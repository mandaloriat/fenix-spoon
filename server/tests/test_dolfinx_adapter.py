"""Tests for the FEniCSx adapter. Skipped automatically where dolfinx is not installed;
run them with the dolfinx Docker image or a conda env with fenics-dolfinx + python-gmsh:

    pytest -m fenics
"""

import threading

import numpy as np
import pytest

dolfinx = pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")

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

    # The unstructured mesh is genuinely unstructured: node spacing is not uniform.
    xs = np.unique(np.round(points[:, 0], 6))
    assert len(xs) > len(points) ** 0.5, "grid-like output would have few distinct x values"

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
