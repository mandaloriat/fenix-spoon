"""Tests for the FEniCSx adapter. Skipped automatically where dolfinx is not installed;
run them with the dolfinx Docker image or a conda env with fenics-dolfinx + python-gmsh:

    pytest -m fenics
"""

import threading

import numpy as np
import pytest

dolfinx = pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")

from geometries import naca4  # noqa: E402

from fenixspoon.geometry import Domain2D, Polygon2D  # noqa: E402
from fenixspoon.solvers import get_solver  # noqa: E402
from fenixspoon.solvers.base import JobCancelled, SolverContext  # noqa: E402
from fenixspoon.solvers.dolfinx_poisson import DolfinxPotentialFlow2D  # noqa: E402
from fenixspoon.solvers.mock_laplace import MockLaplace2D  # noqa: E402

pytestmark = pytest.mark.fenics

AIRFOIL = Polygon2D(
    points=[(0.0, 0.0), (0.35, 0.09), (0.8, 0.05), (1.0, 0.0), (0.8, -0.03), (0.35, -0.06)]
)
GEOMETRY = Domain2D(bounds=(-1.0, -1.0, 2.0, 1.0), obstacle=AIRFOIL)


def make_ctx(tmp_path, events=None, cancel_event=None):
    return SolverContext(
        progress_cb=(events.append if events is not None else lambda e: None),
        cancel_event=cancel_event,
        artifact_dir=tmp_path / "artifacts",
    )


def run(tmp_path, geometry=GEOMETRY, events=None, **params):
    """Solve once. Saves repeating the three-line ceremony for every parameter set."""
    return DolfinxPotentialFlow2D().solve(
        geometry,
        DolfinxPotentialFlow2D.Params(**params),
        make_ctx(tmp_path, events),
    )


def test_adapter_is_registered():
    assert get_solver("dolfinx.potential_flow2d") is DolfinxPotentialFlow2D


def test_solve_produces_sane_grid2d(tmp_path):
    events = []
    params = DolfinxPotentialFlow2D.Params(mesh_size=0.06, resolution=96)
    result = DolfinxPotentialFlow2D().solve(GEOMETRY, params, make_ctx(tmp_path, events))

    assert result.kind == "grid2d"
    ny, nx = result.data["shape"]
    psi = np.asarray(result.data["fields"]["psi"]).reshape(ny, nx)
    mask = np.asarray(result.data["mask"], dtype=bool).reshape(ny, nx)
    assert np.isfinite(psi).all()
    assert mask.any() and not mask.all()
    # Far-field Dirichlet: psi ≈ u_inf * y along the outer boundary of the sampling grid.
    y = np.linspace(GEOMETRY.bounds[1], GEOMETRY.bounds[3], ny)
    np.testing.assert_allclose(psi[:, 0], y, atol=5e-2)
    assert any(e.message for e in events), "adapter must report stage progress"


def test_agrees_with_mock_solver(tmp_path):
    """Same physics, two discretizations: fields must agree away from the obstacle."""
    fem_params = DolfinxPotentialFlow2D.Params(mesh_size=0.05, resolution=96)
    fem = DolfinxPotentialFlow2D().solve(GEOMETRY, fem_params, make_ctx(tmp_path))
    mock_params = MockLaplace2D.Params(resolution=96, iterations=4000, write_vtk=False)
    mock = MockLaplace2D().solve(GEOMETRY, mock_params, make_ctx(tmp_path))

    ny, nx = fem.data["shape"]
    assert mock.data["shape"] == [ny, nx]
    psi_fem = np.asarray(fem.data["fields"]["psi"]).reshape(ny, nx)
    psi_mock = np.asarray(mock.data["fields"]["psi"]).reshape(ny, nx)
    mask = np.asarray(fem.data["mask"], dtype=bool).reshape(ny, nx) | np.asarray(
        mock.data["mask"], dtype=bool
    ).reshape(ny, nx)

    span = psi_mock.max() - psi_mock.min()
    rms = np.sqrt(np.mean((psi_fem[~mask] - psi_mock[~mask]) ** 2)) / span
    assert rms < 0.05, f"normalized RMS disagreement too large: {rms:.3f}"


def test_mesh2d_output_and_vtk_artifact(tmp_path):
    """The adapter can emit its own FEM triangulation, not just a resampled grid."""
    ctx = make_ctx(tmp_path)
    params = DolfinxPotentialFlow2D.Params(mesh_size=0.08, resolution=64, output="mesh2d")
    result = DolfinxPotentialFlow2D().solve(GEOMETRY, params, ctx)

    assert result.kind == "mesh2d"
    points = np.asarray(result.data["points"])
    triangles = np.asarray(result.data["triangles"])
    assert points.ndim == 2 and points.shape[1] == 2
    assert triangles.ndim == 2 and triangles.shape[1] == 3
    assert triangles.min() >= 0 and triangles.max() < len(points)
    for name in ("psi", "speed"):
        assert len(result.data["point_fields"][name]) == len(points)

    # The mesh is genuinely unstructured: a Cartesian grid would have uniform node
    # spacing along x, a triangulation does not.
    gaps = np.diff(np.unique(np.round(points[:, 0], 9)))
    assert gaps.std() / gaps.mean() > 0.1, "node spacing is uniform — this looks like a grid"

    # The obstacle is a hole in the mesh: no node lies in its interior. Test against the
    # polygon shrunk toward its centroid, since nodes *on* the boundary are classified
    # arbitrarily by the even-odd rule.
    from fenixspoon.solvers.mock_laplace import polygon_mask

    obstacle = np.asarray(GEOMETRY.obstacle.points, dtype=float)
    shrunk = obstacle.mean(axis=0) + 0.9 * (obstacle - obstacle.mean(axis=0))
    inside = polygon_mask(shrunk, points[:, 0][None, :], points[:, 1][None, :])
    assert not inside.any()

    arts = ctx.artifacts
    assert [a["name"] for a in arts] == ["solution.vtk"]
    content = (tmp_path / "artifacts" / "solution.vtk").read_text()
    assert "DATASET UNSTRUCTURED_GRID" in content
    assert f"POINTS {len(points)} double" in content
    assert "SCALARS psi" in content and "SCALARS speed" in content


def test_mesh2d_matches_grid2d_sampling(tmp_path):
    """Both output kinds must describe the same solution."""
    common = dict(mesh_size=0.06, resolution=64, write_vtk=False)
    grid = DolfinxPotentialFlow2D().solve(
        GEOMETRY, DolfinxPotentialFlow2D.Params(output="grid2d", **common), make_ctx(tmp_path)
    )
    mesh = DolfinxPotentialFlow2D().solve(
        GEOMETRY, DolfinxPotentialFlow2D.Params(output="mesh2d", **common), make_ctx(tmp_path)
    )
    psi_mesh = np.asarray(mesh.data["point_fields"]["psi"])
    ny, nx = grid.data["shape"]
    psi_grid = np.asarray(grid.data["fields"]["psi"]).reshape(ny, nx)
    grid_mask = np.asarray(grid.data["mask"], dtype=bool).reshape(ny, nx)
    assert psi_mesh.min() == pytest.approx(psi_grid[~grid_mask].min(), abs=0.05)
    assert psi_mesh.max() == pytest.approx(psi_grid[~grid_mask].max(), abs=0.05)


def test_cancellation_before_solve(tmp_path):
    cancel = threading.Event()
    cancel.set()
    params = DolfinxPotentialFlow2D.Params(mesh_size=0.1, resolution=48)
    with pytest.raises(JobCancelled):
        DolfinxPotentialFlow2D().solve(
            GEOMETRY, params, make_ctx(tmp_path, cancel_event=cancel)
        )


# Degenerate geometry (self-intersecting polygons, zero-length edges) never reaches this
# adapter: the geometry schema rejects it at validation time — Gmsh hangs on such input
# (found the hard way; see Polygon2D._check_simple). The conformance corpus in
# protocol/fixtures/geometries.json covers those cases; the job-level wall-clock timeout
# remains the last line of defense for anything else that stalls the mesher.


def test_solve_through_job_api(tmp_path, monkeypatch):
    """End-to-end through the job manager: solvers run on a worker thread there, which
    catches thread-hostile library behavior (e.g. gmsh signal handlers) that direct
    ``solve()`` calls on the main thread cannot see."""
    import time

    from fastapi.testclient import TestClient

    from fenixspoon.main import create_app

    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    with TestClient(create_app()) as client:
        job_id = client.post(
            "/api/v1/jobs",
            json={
                "solver": "dolfinx.potential_flow2d",
                "geometry": GEOMETRY.model_dump(),
                "params": {"mesh_size": 0.08, "resolution": 64},
            },
        ).json()["job_id"]
        deadline = time.monotonic() + 60
        while True:
            status = client.get(f"/api/v1/jobs/{job_id}").json()
            if status["status"] in ("done", "failed", "cancelled"):
                break
            assert time.monotonic() < deadline, "dolfinx job did not finish"
            time.sleep(0.1)
        assert status["status"] == "done", status["error"]
        payload = client.get(f"/api/v1/jobs/{job_id}/result").json()
        assert payload["kind"] == "grid2d"
        stats = payload["stats"]
        assert stats["cells"] > 0 and stats["dofs"] > 0 and stats["seconds"] > 0


@pytest.mark.parametrize("mesh_size", [0.06, 0.1])
def test_cell_estimate_stays_above_the_real_mesh(tmp_path, mesh_size):
    """The budget check is only useful if its estimate errs high.

    ``2A/h²`` alone is the equilateral-tiling *floor* and Gmsh comes in ~35% above it,
    so the estimate carries a safety factor. This test is what stops that factor from
    being quietly tuned back below the real mesh.
    """
    params = DolfinxPotentialFlow2D.Params(mesh_size=mesh_size, resolution=48, write_vtk=False)
    estimate = DolfinxPotentialFlow2D.estimate_cells(GEOMETRY, params)
    result = DolfinxPotentialFlow2D().solve(GEOMETRY, params, make_ctx(tmp_path))
    assert estimate >= result.stats["cells"]
    # ...but not so high that the cap becomes theatre.
    assert estimate <= 5 * result.stats["cells"]


# ---------------------------------------------------- circulation and lift (#68)

#: The symmetric section from `geometries`, in a domain with room for a circulation contour.
#: `GEOMETRY` above is a 4-vertex cambered diamond — fine for plumbing, useless for checking
#: that lift is antisymmetric in incidence, which is a property of the *body*.
SYMMETRIC = Domain2D(bounds=(-1.5, -1.5, 2.5, 1.5), obstacle=Polygon2D(points=naca4()))


def test_the_kutta_condition_produces_lift_and_the_pair_agrees_on_it(tmp_path):
    """The FEniCSx half of #68, and the cross-validation the pair exists for.

    `mock.laplace2d` and this adapter solve the same equations two ways and share one
    :mod:`fenixspoon.solvers.aerodynamics` implementation of the loads, so their lift
    coefficients have to agree — not to the last digit, since one relaxes on a raster and the
    other factorises a P1 system on a body-conforming mesh, but well inside the band that
    separates "the same physics" from "one of these is wrong".
    """
    fem = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.04, resolution=160, alpha=5.0)
    mock = MockLaplace2D().solve(
        SYMMETRIC,
        MockLaplace2D.Params(resolution=160, iterations=8000, alpha=5.0, write_vtk=False),
        make_ctx(tmp_path),
    )

    for metric in ("circulation", "c_l", "c_m_c4", "x_cp"):
        assert metric in fem.metrics, f"{metric} missing from the FEniCSx result"
    assert fem.metrics["c_l"] > 0.3, "five degrees on this section must lift"
    assert fem.metrics["circulation"] < 0, "lift is clockwise circulation"
    assert fem.metrics["c_l"] == pytest.approx(mock.metrics["c_l"], rel=0.25)


def test_lift_is_antisymmetric_in_incidence_for_a_symmetric_section(tmp_path):
    """A property of the *problem*, not of the discretisation, so it holds however coarse the
    mesh — which makes it the one lift assertion a mesh cannot fake. It needs a symmetric
    section, which is why `naca4` lives in `geometries` rather than in one test module."""
    up = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.05, resolution=120, alpha=6.0)
    down = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.05, resolution=120, alpha=-6.0)
    assert up.metrics["c_l"] > 0 > down.metrics["c_l"]
    assert up.metrics["c_l"] == pytest.approx(-down.metrics["c_l"], rel=0.02)


def test_zero_incidence_on_a_symmetric_section_is_zero_lift(tmp_path):
    result = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.05, resolution=120, alpha=0.0)
    assert result.metrics["c_l"] == pytest.approx(0.0, abs=0.02)


def test_the_kutta_value_comes_off_the_mesh_so_the_grid_does_not_cap_it(tmp_path):
    """`resolution` is the *reporting* grid; `mesh_size` is what resolves the flow.

    The body's streamfunction value is a Dirichlet value of the returned field, so it is probed
    on the triangulation. If it were read off the sampling grid instead, halving `resolution`
    while holding `mesh_size` would move `c_l` — which is the regression this guards, and the
    reason the surface-pressure integral (which genuinely does use the grid) is checked
    separately with a looser band.
    """
    coarse = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.04, resolution=64, alpha=5.0)
    fine = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.04, resolution=256, alpha=5.0)
    assert coarse.metrics["circulation"] == pytest.approx(
        fine.metrics["circulation"], rel=0.02
    ), "the Kutta value moved with the sampling grid, so it is being read off it"


def test_the_surface_pressure_comes_back_as_a_series(tmp_path):
    """Protocol 1.4's `series`, from this adapter as well as the mock — the pair has to agree
    about the *shape* of an answer, not only about its numbers."""
    result = run(tmp_path, geometry=SYMMETRIC, mesh_size=0.05, resolution=128, alpha=4.0)
    assert [entry.name for entry in result.series] == ["surface_cp"]
    assert {trace.name for trace in result.series[0].traces} == {"cp_upper", "cp_lower"}
    for trace in result.series[0].traces:
        assert trace.x is not None and len(trace.values) == len(trace.x.values)


def test_a_mesh2d_result_still_carries_the_aerodynamic_metrics(tmp_path):
    """The surface integral samples a grid regardless of `output`, which is why `resolution`
    affects `c_m_c4` even when the payload is the triangulation. Worth a test because the
    obvious reading of `resolution` — "only for a grid2d result" — is no longer true."""
    result = run(
        tmp_path, geometry=SYMMETRIC, mesh_size=0.05, resolution=128, alpha=4.0,
        output="mesh2d", write_vtk=False,
    )
    assert result.kind == "mesh2d"
    assert result.metrics["c_l"] > 0
    assert result.series, "and the curve comes with it"


def test_without_the_kutta_condition_the_lift_metrics_are_absent(tmp_path):
    result = run(tmp_path, mesh_size=0.06, resolution=96, kutta=False)
    for absent in ("circulation", "c_l", "c_m_c4", "x_cp"):
        assert absent not in result.metrics
    assert result.metrics["cp_min"] < 0, "the flow field is still usable"
    assert any("no Kutta condition" in warning for warning in result.warnings)


def test_a_mesh2d_result_without_circulation_builds_no_sampling_grid(tmp_path):
    """The one path that needs no grid at all: a triangulation payload with no loads to
    integrate. Asserted through the progress stream, which is the only thing that observes it —
    a `mesh2d` + `kutta=False` run reports four stages, not six."""
    events = []
    result = run(
        tmp_path, mesh_size=0.06, resolution=512, output="mesh2d",
        kutta=False, write_vtk=False, events=events,
    )
    assert result.kind == "mesh2d"
    assert max(event.iteration for event in events) == 4
