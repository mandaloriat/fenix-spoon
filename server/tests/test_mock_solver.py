import numpy as np

from fenixspoon.geometry import Domain2D, Polygon2D
from fenixspoon.solvers.mock_laplace import MockLaplace2D, polygon_mask

AIRFOIL = Polygon2D(
    points=[(0.0, 0.0), (0.35, 0.09), (0.8, 0.05), (1.0, 0.0), (0.8, -0.03), (0.35, -0.06)]
)
GEOMETRY = Domain2D(bounds=(-1.0, -1.0, 2.0, 1.0), obstacle=AIRFOIL)


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


def test_solve_produces_grid2d_and_converges():
    events = []
    params = MockLaplace2D.Params(resolution=64, iterations=400, report_every=50)
    result = MockLaplace2D().solve(GEOMETRY, params, events.append)

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
    residuals = [e.residual for e in events if e.residual is not None]
    assert residuals[-1] < residuals[0], "Jacobi residual must decrease"


def test_speed_zero_inside_obstacle():
    params = MockLaplace2D.Params(resolution=64, iterations=200)
    result = MockLaplace2D().solve(GEOMETRY, params, lambda e: None)
    ny, nx = result.data["shape"]
    speed = np.asarray(result.data["fields"]["speed"])
    mask = np.asarray(result.data["mask"], dtype=bool)
    assert mask.any()
    assert np.all(speed[mask] == 0.0)
