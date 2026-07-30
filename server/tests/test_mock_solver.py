import threading

import numpy as np
import pytest

from fenixspoon.geometry import Domain2D, Polygon2D
from fenixspoon.solvers.base import JobCancelled, SolverContext
from fenixspoon.solvers.mock_laplace import MockLaplace2D, polygon_mask

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


def test_polygon_mask_contains_centroid():
    pts = np.asarray(AIRFOIL.points, dtype=float)
    cx, cy = pts.mean(axis=0)
    xx, yy = np.meshgrid(np.linspace(-1, 2, 61), np.linspace(-1, 1, 41))
    mask = polygon_mask(pts, xx, yy)
    i = np.argmin(np.abs(np.linspace(-1, 1, 41) - cy))
    j = np.argmin(np.abs(np.linspace(-1, 2, 61) - cx))
    assert mask[i, j]
    assert not mask[0, 0]  # domain corner is outside the airfoil
    assert 0 < mask.sum() < mask.size / 4


def test_solve_produces_grid2d_and_converges(tmp_path):
    events = []
    params = MockLaplace2D.Params(resolution=64, iterations=400, report_every=50)
    result = MockLaplace2D().solve(GEOMETRY, params, make_ctx(tmp_path, events))

    assert result.kind == "grid2d"
    ny, nx = result.data["shape"]
    for name in ("psi", "speed"):
        assert len(result.data["fields"][name]) == ny * nx
    assert len(result.data["mask"]) == ny * nx

    psi = np.asarray(result.data["fields"]["psi"]).reshape(ny, nx)
    assert np.isfinite(psi).all()
    # Far-field Dirichlet: psi = u_inf * y on the domain edges.
    y = np.linspace(GEOMETRY.bounds[1], GEOMETRY.bounds[3], ny)
    np.testing.assert_allclose(psi[:, 0], y, atol=1e-12)

    assert events, "solver must report progress"
    # Grouped by stage, because the Kutta path is two relaxations and the residual is monotone
    # *within* one: the second starts from its own initial guess, so a stream read as one
    # sequence appears to go backwards halfway through. Every event carries the stage that
    # produced it, which is what makes that legible to a subscriber as well as to this test.
    by_stage: dict[str, list[float]] = {}
    for event in events:
        if event.residual is not None:
            by_stage.setdefault(event.message or "", []).append(event.residual)
    assert set(by_stage) == {
        "relaxing the free-stream mode",
        "relaxing the circulatory mode",
    }, sorted(by_stage)
    for stage, residuals in by_stage.items():
        assert residuals[-1] < residuals[0], f"Jacobi residual must decrease in {stage}"


def test_speed_zero_inside_obstacle(tmp_path):
    params = MockLaplace2D.Params(resolution=64, iterations=200, write_vtk=False)
    result = MockLaplace2D().solve(GEOMETRY, params, make_ctx(tmp_path))
    ny, nx = result.data["shape"]
    speed = np.asarray(result.data["fields"]["speed"])
    mask = np.asarray(result.data["mask"], dtype=bool)
    assert mask.any()
    assert np.all(speed[mask] == 0.0)


def test_mesh2d_output(tmp_path):
    params = MockLaplace2D.Params(resolution=48, iterations=100, output="mesh2d")
    result = MockLaplace2D().solve(GEOMETRY, params, make_ctx(tmp_path))

    assert result.kind == "mesh2d"
    points = np.asarray(result.data["points"])
    triangles = np.asarray(result.data["triangles"])
    assert points.ndim == 2 and points.shape[1] == 2
    assert triangles.ndim == 2 and triangles.shape[1] == 3
    # All triangle indices reference existing, compacted nodes.
    assert triangles.min() >= 0 and triangles.max() < len(points)
    for name in ("psi", "speed"):
        assert len(result.data["point_fields"][name]) == len(points)
    # The obstacle leaves a hole: fewer nodes than the full grid.
    pts_full = np.asarray(GEOMETRY.obstacle.points)
    assert len(points) < 48 * 48
    # No mesh node falls inside the obstacle polygon.
    xx, yy = points[:, 0][None, :], points[:, 1][None, :]
    inside = polygon_mask(pts_full, xx, yy)
    assert not inside.any()


def test_vtk_artifact_written(tmp_path):
    ctx = make_ctx(tmp_path)
    params = MockLaplace2D.Params(resolution=32, iterations=50)
    MockLaplace2D().solve(GEOMETRY, params, ctx)
    arts = ctx.artifacts
    assert [a["name"] for a in arts] == ["solution.vtk"]
    assert arts[0]["size"] > 0
    content = (tmp_path / "artifacts" / "solution.vtk").read_text()
    assert content.startswith("# vtk DataFile")
    assert "SCALARS psi" in content and "SCALARS speed" in content


def test_cancellation_raises(tmp_path):
    cancel = threading.Event()
    cancel.set()
    params = MockLaplace2D.Params(resolution=32, iterations=50)
    with pytest.raises(JobCancelled):
        MockLaplace2D().solve(GEOMETRY, params, make_ctx(tmp_path, cancel_event=cancel))


def test_artifact_name_validation(tmp_path):
    ctx = make_ctx(tmp_path)
    for bad in ("../evil", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            ctx.artifact(bad)
