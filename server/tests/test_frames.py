"""The time index (#86): what a transient can hand back, and what it deliberately cannot.

The design decision these tests pin down is that **the index is derived, not stored**. An
artifact carries the instant it holds; the result's `frames` is the ordered projection of the
ones that have one. So the two cannot disagree, and a frame naming a file the result does not
serve is unrepresentable rather than merely tested for — which is why there is no test for
that case here, and why its absence is deliberate.

The accepted limit is asserted too, because a limit nobody wrote down becomes a bug report:
there is no reduction at a chosen instant. A scalar-against-time question is answered by the
curve; a field question means fetching a frame.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest
from geometries import rect

from fenixspoon.core import FenixSpoonCore
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.core.results import FieldQuery
from fenixspoon.frames import MAX_FRAMES, frames_of
from fenixspoon.geometry import Region2D, Regions2D
from fenixspoon.jobs import JobManager
from fenixspoon.protocol import ArtifactRef, ResultEnvelope
from fenixspoon.solvers.base import ArtifactSpec, SolverContext
from fenixspoon.solvers.mock_transient_heat import MockTransientHeat2D

SINK = Regions2D(
    bounds=(-0.05, -0.02, 0.05, 0.06),
    regions=[
        Region2D(
            name="base",
            shape=rect(-0.03, 0.0, 0.03, 0.006),
            material={"k": 205.0, "rho_cp": 2.42e6, "q": 5.0e6},
        )
    ],
    background={},
)


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def run_to_completion(core, me, solver, **params):
    """One loop for the whole solve. Two `asyncio.run` calls would watch a status the other
    loop's callbacks set — the trap this suite has fallen into three times."""

    async def go():
        job = await core.submit(solver, SINK, params, me)
        deadline = asyncio.get_running_loop().time() + 120
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return job.id

    return asyncio.run(go())


def solve(core, me, **params):
    job_id = run_to_completion(core, me, "mock.transient_heat2d", **params)
    return core.result(job_id, me)


# ------------------------------------------------------------------- the index itself


def test_a_transient_asked_to_save_returns_an_index_over_its_artifacts(core, me):
    """Every frame is a file this result serves, at a stated instant, in time order."""
    result = solve(core, me, resolution=32, duration=100.0, steps=10, save_every=5)

    frames = frames_of(result.artifacts)
    assert [frame.t for frame in frames] == [50.0, 100.0]
    served = {artifact.name for artifact in result.artifacts}
    assert {frame.artifact for frame in frames} <= served
    # The final-instant VTK is not a frame: it carries no `t`, because it is the answer
    # rather than a sample of the history.
    assert "solution.vtk" in served
    assert "solution.vtk" not in {frame.artifact for frame in frames}


def test_saving_nothing_is_the_default_and_leaves_no_index(core, me):
    """A steady reading of a transient is the common case, and it should cost nothing.

    Writing a field per step is the expensive half of a transient, so `save_every` defaults
    to zero and the answer stays the curves. An empty index is also how a caller tells "this
    solve has no time axis to walk" from "you did not ask for one".
    """
    result = solve(core, me, resolution=32, duration=100.0, steps=10)
    assert frames_of(result.artifacts) == []


def test_the_index_travels_on_the_compact_level_without_the_fields(core, me):
    """`frames` rides with `artifacts` — references, not arrays — so the default answer
    carries the whole time axis and none of the history.

    This is the property that makes the design worth having: a caller learns which instants
    exist, and how large each one is, before deciding to fetch any of them.
    """
    result = solve(core, me, resolution=32, duration=100.0, steps=10, save_every=2)
    payload = core.result_levels(result.job_id, me, ["artifacts"]).wire()

    assert payload.get("fields") is None, "the compact answer grew a field payload"
    assert [frame["t"] for frame in payload["frames"]] == [20.0, 40.0, 60.0, 80.0, 100.0]
    assert len(json.dumps(payload)) < 4096, "the index stopped being compact"


def test_a_steady_result_carries_no_frames_key_at_all(core, me):
    """Absent rather than empty, so a caller can tell "no time axis" from "not requested".

    `Selective` drops nulls on the wire, which is what makes the distinction free — a steady
    capability pays nothing for a field it has no use for.
    """
    job_id = run_to_completion(core, me, "mock.heat2d", resolution=32, iterations=60)
    payload = core.result_levels(job_id, me, ["artifacts"]).wire()
    assert "frames" not in payload


# ------------------------------------------------- the invariants, and the accepted limit


def test_the_index_cannot_name_a_file_the_result_does_not_serve():
    """Not a validation rule — a consequence of where the instant lives.

    The index is derived from the artifacts, so there is no way to express a frame pointing
    at something absent. This test exists to record that, because the obvious alternative
    design (a `frames` list beside `artifacts`) needs a cross-check that this one makes
    meaningless.
    """
    envelope = ResultEnvelope(
        job_id="job_1",
        solver="mock.transient_heat2d",
        kind="grid2d",
        data={"bounds": [0.0, 0.0, 1.0, 1.0], "shape": [2, 2],
              "fields": {"T": [0.0, 1.0, 2.0, 3.0]}, "mask": [0, 0, 0, 0]},
        artifacts=[
            ArtifactRef(name="frame_0001.vtk", content_type="model/vnd.vtk", size=1,
                        url="/a", t=10.0),
            ArtifactRef(name="solution.vtk", content_type="model/vnd.vtk", size=1, url="/b"),
        ],
    )
    assert [frame.artifact for frame in envelope.frames] == ["frame_0001.vtk"]
    assert set(envelope.model_dump()["frames"][0]) == {"t", "artifact"}


def test_the_index_is_capped_so_it_cannot_become_the_payload():
    """A list of references is cheap right up until it is used to smuggle the history."""
    many = [
        ArtifactRef(name=f"frame_{index:05d}.vtk", content_type="model/vnd.vtk", size=1,
                    url=f"/a/{index}", t=float(index))
        for index in range(MAX_FRAMES + 1)
    ]
    with pytest.raises(ValueError, match="exceeds the"):
        ResultEnvelope(
            job_id="job_1", solver="mock.transient_heat2d", kind="grid2d",
            data={"bounds": [0.0, 0.0, 1.0, 1.0], "shape": [2, 2],
                  "fields": {"T": [0.0, 1.0, 2.0, 3.0]}, "mask": [0, 0, 0, 0]},
            artifacts=many,
        )


def test_a_frame_file_is_declared_by_pattern_rather_than_going_undeclared():
    """The count is a parameter, so the name has to be one too.

    Without `{index}` a transient would have to either leave its frames undeclared — losing
    the check that written files are a subset of declared ones — or declare a fixed number it
    cannot know. The matcher is deliberately narrow: digits only, so `frame_x.vtk` is still an
    undeclared file rather than something the pattern quietly absorbs.
    """
    spec = next(
        spec for spec in MockTransientHeat2D.artifacts if "{index}" in spec.name
    )
    assert spec.matches("frame_0001.vtk")
    assert not spec.matches("frame_x.vtk")
    assert not spec.matches("frame_.vtk")
    assert not spec.matches("solution.vtk")
    assert ArtifactSpec(
        name="solution.vtk", content_type="model/vnd.vtk", description="."
    ).matches("solution.vtk")


def test_there_is_no_reduction_at_a_chosen_instant(core, me):
    """The limit agreed when the shape was settled, asserted so it stays a decision.

    `result.query` reduces *the payload*, and the payload is the final instant — asking it
    about a frame is not a supported operation and must not silently answer about the wrong
    time. A caller wanting a scalar against time reads the curve; a caller wanting a field
    fetches the frame.
    """
    result = solve(core, me, resolution=32, duration=100.0, steps=10, save_every=5)
    answer = core.query_result(
        result.job_id, FieldQuery(field="T", op="max"), me
    )

    # The peak of the final instant, which is what the payload holds — and the curve, which
    # is where the history lives, agrees with it at its last sample. A query that had somehow
    # reached a frame would disagree with both.
    levels = core.result_levels(result.job_id, me, ["series"])
    history = next(
        curve for curve in levels.series if curve.name == "temperature_history"
    )
    peak = next(trace for trace in history.traces if trace.name == "t_max")
    assert answer.result["value"] == pytest.approx(peak.values[-1], rel=1e-6)
    assert "t" not in FieldQuery.model_fields, (
        "a field query grew a time argument; #86 settled that there is no reduction at a "
        "chosen instant, and adding one silently would answer about the wrong time"
    )


def test_the_frame_files_are_readable_on_their_own(core, me):
    """Each frame stands alone: a caller fetches one instant without needing the others."""
    result = solve(core, me, resolution=32, duration=100.0, steps=4, save_every=2)
    for frame in frames_of(result.artifacts):
        handle = next(item for item in result.artifacts if item.name == frame.artifact)
        text = Path(handle.path).read_text(encoding="utf-8", errors="replace")
        assert text.startswith("# vtk DataFile"), frame.artifact
        assert handle.size > 0


def test_an_artifact_records_the_instant_it_holds():
    """The context API, at the level an adapter author uses it."""
    ctx = SolverContext(progress_cb=lambda event: None, artifact_dir=Path(tempfile.mkdtemp()))
    ctx.artifact("frame_0001.vtk", t=25.0).write_text("x")
    ctx.artifact("solution.vtk").write_text("y")

    entries = {entry["name"]: entry for entry in ctx.artifacts}
    assert entries["frame_0001.vtk"]["t"] == 25.0
    assert "t" not in entries["solution.vtk"]


# ------------------------------------------------------ the transports, and the SDK types


def test_the_result_route_carries_every_field_the_envelope_declares(tmp_path, monkeypatch):
    """The gap this PR's review found: `/jobs/{id}/result` builds its payload by hand.

    A hand-built dict beside a declared model is a place the two can disagree, and they did —
    protocol 1.7 was bumped with `t` reaching the compact levels and not this route, so the
    version advertised an addition the full result did not carry. Comparing key sets is the
    check that catches the *next* one too, rather than only this field.
    """
    from fastapi.testclient import TestClient

    from fenixspoon.main import create_app

    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "http"))
    with TestClient(create_app()) as client:
        submitted = client.post(
            "/api/v1/jobs",
            json={
                "solver": "mock.transient_heat2d",
                "geometry": SINK.model_dump(),
                "params": {"resolution": 32, "duration": 100.0, "steps": 4, "save_every": 2},
            },
        )
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        deadline = time.monotonic() + 60
        while client.get(f"/api/v1/jobs/{job_id}").json()["status"] not in (
            "done", "failed", "cancelled"
        ):
            assert time.monotonic() < deadline, "solve did not finish"
            time.sleep(0.05)

        payload = client.get(f"/api/v1/jobs/{job_id}/result").json()

    declared = set(ResultEnvelope.model_json_schema()["properties"])
    assert declared <= set(payload), (
        f"the result route omits envelope fields: {sorted(declared - set(payload))}"
    )
    assert [frame["t"] for frame in payload["frames"]] == [50.0, 100.0]
    framed = {item["name"]: item.get("t") for item in payload["artifacts"]}
    assert framed["frame_0002.vtk"] == 50.0
    assert framed["solution.vtk"] is None, "the final answer is not one of the instants"
    # And the payload still validates as the thing it claims to be.
    ResultEnvelope.model_validate(payload)


def test_the_frame_cap_is_enforced_where_artifacts_are_registered():
    """On every transport, not only where a `ResultEnvelope` happens to be built.

    The compact levels and the in-process API never construct one, so a cap living only in
    that model would be a cap half the callers walk past. Raised at registration it also
    fails the solve with a message naming the parameter to change, rather than producing a
    result that cannot be represented.
    """
    ctx = SolverContext(progress_cb=lambda event: None, artifact_dir=Path(tempfile.mkdtemp()))
    for index in range(MAX_FRAMES):
        ctx.artifact(f"frame_{index:05d}.vtk", t=float(index))
    with pytest.raises(ValueError, match="at most"):
        ctx.artifact("frame_99999.vtk", t=1.0e9)
    # Unframed files are unaffected: the cap is on the index, not on the output.
    assert ctx.artifact("solution.vtk").name == "solution.vtk"
