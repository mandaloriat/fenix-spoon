"""The M2.5 vertical slice — the milestone's exit criterion (#51).

> From a local agent process with no HTTP server started: discover the potential-flow
> capability, create an airfoil design in a workspace, submit a mock or FEniCSx solve, receive
> progress, query compact field metrics, retrieve the VTK artifact by reference, patch one
> geometry parameter, and run a second solve that reuses the unchanged objects and reports
> what came from cache.

That is :func:`test_the_vertical_slice`, parametrised over the mock solver and the FEniCSx
one, so the same loop is the acceptance test in a plain virtualenv and in the dolfinx CI
image. The issue is explicit that the milestone does not close on "JSON-RPC answers" but on
this passing end to end, which is why the assertions are about the *loop* — did the second
solve reuse the cache, did the artifact arrive by reference, did progress actually stream —
rather than about any one call's payload.

"No HTTP server started" is checked rather than assumed:
:func:`test_the_slice_needs_no_web_framework` runs the whole thing in a subprocess and fails
if FastAPI was ever imported.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fenixspoon import local

REPO_SERVER = Path(__file__).resolve().parents[1]

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}

#: Per-capability parameters for a solve small enough to be a test and real enough to mean
#: something. The FEniCSx adapter meshes, so its knob is `mesh_size` rather than `resolution`
#: — which is exactly why a study names its parameter instead of guessing (#48).
SUBJECTS = {
    "mock.laplace2d": {"resolution": 48, "iterations": 400, "report_every": 50},
    "dolfinx.potential_flow2d": {"mesh_size": 0.12, "resolution": 48},
}


def capability_available(name: str) -> bool:
    from fenixspoon.solvers import get_solver

    return get_solver(name) is not None


@pytest.mark.parametrize(
    "solver",
    [
        "mock.laplace2d",
        pytest.param("dolfinx.potential_flow2d", marks=pytest.mark.fenics),
    ],
)
def test_the_vertical_slice(tmp_path, solver):
    """Discover, design, solve, read, patch, re-solve — with the cache reporting itself.

    One session for the whole loop, which is also the shape an agent has: the workspace
    outlives any single call, and object references are what carry state between them.
    """
    if not capability_available(solver):
        pytest.skip(f"{solver} is not installed")
    params = SUBJECTS[solver]

    with local.open_workspace(tmp_path / "workspace") as fs:
        # 1. Discover the capability rather than assuming it. An agent starts here because it
        #    cannot know what a given installation has.
        potential_flow = [
            item for item in fs.capabilities() if item.physics == "potential-flow"
        ]
        assert any(item.name == solver for item in potential_flow), (
            f"{solver} did not describe itself as potential flow"
        )
        described = fs.describe(solver, ["params", "metrics"])
        declared = {metric.name for metric in described.metrics}
        assert "speed_max" in declared

        # 2. An airfoil design in the workspace, under references rather than in messages.
        geometry = fs.create("geometry", AIRFOIL, label="airfoil")
        design = fs.create(
            "design",
            {"solver": solver, "geometry": geometry.ref, "params": params},
            label="baseline",
        )

        # 3. Submit and wait.
        first = fs.submit(design=design.ref).wait(timeout=300)
        assert first.status().status == "done", first.status().error

        # 4. Progress actually streamed. Read from the stored log rather than a live
        #    subscription, which is what an agent does — it does not want twenty ticks in its
        #    context, it wants to know the solve reported.
        events = first.events(limit=500)
        assert events.final, "the log never reached a terminal event"
        assert any(event["type"] == "progress" for event in events.events), (
            "no progress was reported; the loop's third requirement is unmet"
        )

        # 5. Compact metrics — scalars, not fields.
        metrics = first.metrics()
        assert declared <= set(metrics), "a declared metric was not reported"
        assert isinstance(metrics["speed_max"], float)
        peak = first.query("psi", "max")
        assert isinstance(peak.result["value"], int | float)
        assert len(json.dumps(metrics)) < 1024, "metrics stopped being compact"

        # 6. The artifact by reference: a path on this machine, not bytes in the answer.
        vtk = first.artifact("solution.vtk")
        assert Path(vtk.path).is_file()
        assert Path(vtk.path).read_text(encoding="utf-8", errors="replace").startswith(
            "# vtk DataFile"
        )
        assert vtk.size > 0

        # 7. Patch *one* geometry parameter — one control point, which is the edit the whole
        #    workspace design exists to make cheap.
        moved = fs.patch(
            geometry.ref,
            [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.14]}],
        )
        assert moved.revision == 2

        # 8. The second solve. The design is unchanged and its geometry reference is
        #    unpinned, so it resolves to the new revision — a different shape, a different
        #    answer, and *not* a cache hit. That is the interesting assertion: a cache that
        #    hit here would be serving the old airfoil's numbers for the new one.
        second = fs.submit(design=design.ref).wait(timeout=300)
        assert second.id != first.id, "the patched geometry was answered from cache"
        assert second.provenance().cached is False
        assert second.metrics()["speed_max"] != metrics["speed_max"]

        # …and the unchanged objects were reused rather than re-created: one design, one
        # geometry object with two revisions, and both solves name the same design.
        assert fs.get(design.ref).revision == 1, "the design was rewritten to change a point"
        assert [1, 2] == [
            fs.get(f"{geometry.ref}@{n}").revision for n in (1, 2)
        ]
        assert first.provenance().inputs["design"] == second.provenance().inputs["design"]

        # 9. …and re-solving something already computed *does* report the cache. Submitted by
        #    pinning the first revision, which is the same content the first solve ran on.
        pinned = fs.create(
            "design",
            {"solver": solver, "geometry": f"{geometry.ref}@1", "params": params},
            label="baseline-pinned",
        )
        third = fs.submit(design=pinned.ref)
        assert third.id == first.id, "an identical solve was not reused"
        assert third.provenance().cached is True

        # The whole point of the milestone, stated as a size: everything above crossed the
        # boundary as scalars and references. Only the artifact is large, and it stayed a
        # path.
        answer = second.result().model_dump(exclude_none=True)
        assert "fields" not in answer
        assert len(json.dumps(answer)) < 2048


def test_the_slice_needs_no_web_framework(tmp_path):
    """"With no HTTP server started" as a property of the code, not of this test's imports.

    A subprocess for the reason every other purity test here uses one: this process has
    FastAPI loaded from the API suite, so an in-process `sys.modules` check would pass no
    matter what the slice imports. Running the loop itself rather than importing the module
    is what makes it meaningful — a lazy import inside `submit` would slip past the weaker
    check.
    """
    script = f"""
import json, sys
from fenixspoon import local

with local.open_workspace({str(tmp_path / "pure")!r}) as fs:
    geometry = fs.create("geometry", {AIRFOIL!r})
    design = fs.create("design", {{
        "solver": "mock.laplace2d", "geometry": geometry.ref,
        "params": {{"resolution": 32, "iterations": 200}},
    }})
    job = fs.submit(design=design.ref).wait(timeout=300)
    assert job.status().status == "done"
    assert job.metrics()["speed_max"] > 0
    fs.patch(geometry.ref, [{{"op": "replace", "path": "/obstacle/points/1",
                             "value": [0.35, 0.14]}}])
    assert fs.submit(design=design.ref).wait(timeout=300).status().status == "done"

sys.exit(1 if "fastapi" in sys.modules else 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_SERVER, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"the slice imported a web framework, or failed\n{result.stderr}"
    )


@pytest.mark.parametrize(
    "solver",
    [
        "mock.laplace2d",
        pytest.param("dolfinx.potential_flow2d", marks=pytest.mark.fenics),
    ],
)
def test_both_solvers_honour_the_same_envelopes(tmp_path, solver):
    """"Mock and FEniCSx honor the same envelopes for the same operation" (#51).

    The two adapters mesh differently and produce different numbers — that is the point of
    having both — so what is compared is the *shape*: the same result kind, the same declared
    metrics actually present, the same diagnostics keys, the same artifact. A caller written
    against one must not have to branch for the other.
    """
    if not capability_available(solver):
        pytest.skip(f"{solver} is not installed")

    with local.open_workspace(tmp_path / "envelope") as fs:
        geometry = fs.create("geometry", AIRFOIL)
        design = fs.create(
            "design", {"solver": solver, "geometry": geometry.ref, "params": SUBJECTS[solver]}
        )
        job = fs.submit(design=design.ref).wait(timeout=300)

        result = job.result(["status", "metrics", "diagnostics", "provenance", "artifacts"])
        assert result.status.status == "done"
        declared = {metric.name for metric in fs.describe(solver, ["metrics"]).metrics}
        assert declared <= set(result.metrics)
        assert {"cells", "seconds"} <= set(result.diagnostics.stats)
        assert [artifact.name for artifact in result.artifacts] == ["solution.vtk"]
        assert result.provenance.solver == solver
        assert result.provenance.solver_version
