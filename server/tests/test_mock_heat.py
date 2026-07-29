"""The heat-sink solver (#18).

Two of these are physics assertions rather than smoke tests, and they are the ones worth
having. **Energy has to balance**: at convergence, the power the sources put in must
equal the power convected out, and any bug in the face coefficients or the boundary
treatment shows up there before it shows up in a picture. And **fins have to cool**: the
example exists to show that more surface means a cooler chip, so if that ordering ever
inverts the example is not just wrong, it is teaching the opposite of its lesson.
"""

import numpy as np
import pytest

from fenixspoon.geometry import Polygon2D, Region2D, Regions2D
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_heat import MockHeat2D, solid_mask

K_ALUMINIUM = 205.0
K_AIR = 0.026


def rect(x0: float, y0: float, x1: float, y1: float) -> Polygon2D:
    return Polygon2D(points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def heat_sink(fins: int) -> Regions2D:
    """An aluminium base with `fins` fins, heated from below by a chip."""
    spacing = 0.064 / max(fins, 1)
    return Regions2D(
        bounds=(-0.05, -0.03, 0.05, 0.05),
        regions=[
            Region2D(name="base", shape=rect(-0.04, -0.012, 0.04, -0.005),
                     material={"k": K_ALUMINIUM}),
            *[
                Region2D(
                    name=f"fin{i}",
                    shape=rect(-0.032 + i * spacing, -0.005, -0.026 + i * spacing, 0.035),
                    material={"k": K_ALUMINIUM},
                )
                for i in range(fins)
            ],
            Region2D(name="chip", shape=rect(-0.012, -0.02, 0.012, -0.012),
                     material={"k": 150.0, "q": 2.0e6}),
        ],
        background={"k": K_AIR},
    )


def solve(geometry: Regions2D, tmp_path, **overrides):
    settings = {"resolution": 120, "iterations": 1500, "write_vtk": False, **overrides}
    params = MockHeat2D.Params(**settings)
    ctx = SolverContext(progress_cb=lambda event: None, artifact_dir=tmp_path)
    return MockHeat2D().solve(geometry, params, ctx), params


def test_energy_balances_at_convergence(tmp_path):
    """Power in must equal power out, or a coefficient somewhere is wrong.

    Computed from the returned field rather than from the solver's internals, so this
    checks the answer a client receives, not the arithmetic that produced it.
    """
    result, params = solve(heat_sink(5), tmp_path, iterations=4000)
    ny, nx = result.data["shape"]
    temperature = np.asarray(result.data["fields"]["T"]).reshape(ny, nx)
    fluid = np.asarray(result.data["mask"], dtype=bool).reshape(ny, nx)
    solid = ~fluid

    xmin, ymin, xmax, ymax = result.data["bounds"]
    dx, dy = (xmax - xmin) / (nx - 1), (ymax - ymin) / (ny - 1)

    geometry = heat_sink(5)
    x, y = np.linspace(xmin, xmax, nx), np.linspace(ymin, ymax, ny)
    xx, yy = np.meshgrid(x, y)
    from fenixspoon.solvers.mock_magnetostatics import rasterize_regions

    source = rasterize_regions(geometry, xx, yy, "q", 0.0)
    power_in = float(np.sum(np.where(solid, source, 0.0)) * dx * dy)

    # Every solid face touching fluid or the domain edge sheds h*(T - T_ambient)*length.
    power_out = 0.0
    for rows, cols, face in ((0, -1, dy), (0, 1, dy), (-1, 0, dx), (1, 0, dx)):
        exposed = solid & ~_neighbour(solid, rows, cols)
        power_out += float(np.sum(np.where(exposed, temperature - params.t_ambient, 0.0))) * (
            params.h * face
        )

    assert power_in > 0
    assert power_out == pytest.approx(power_in, rel=0.05), (
        f"power in {power_in:.1f} W/m, convected out {power_out:.1f} W/m"
    )


def _neighbour(solid: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Neighbour solidity with off-grid treated as fluid — mirrors the solver."""
    from fenixspoon.solvers.mock_heat import _neighbour_is_solid

    return _neighbour_is_solid(solid, rows, cols)


def test_more_fins_cool_the_chip(tmp_path):
    """The whole point of the example, as an assertion."""
    rises = [solve(heat_sink(n), tmp_path)[0].stats["t_rise"] for n in (0, 2, 5, 9)]
    assert rises == sorted(rises, reverse=True), rises
    # And the effect is worth showing, not a rounding difference.
    assert rises[0] > 1.5 * rises[-1], rises


def test_a_hotter_ambient_shifts_the_whole_solution(tmp_path):
    """Conduction is linear in temperature: only the offset moves."""
    cool, _ = solve(heat_sink(5), tmp_path, t_ambient=20.0)
    warm, _ = solve(heat_sink(5), tmp_path, t_ambient=60.0)
    assert warm.stats["t_max"] == pytest.approx(cool.stats["t_max"] + 40.0, abs=0.2)
    assert warm.stats["t_rise"] == pytest.approx(cool.stats["t_rise"], rel=1e-3)


def test_stronger_convection_cools(tmp_path):
    gentle, _ = solve(heat_sink(5), tmp_path, h=10.0)
    forced, _ = solve(heat_sink(5), tmp_path, h=200.0)
    assert forced.stats["t_rise"] < gentle.stats["t_rise"] / 5


def test_no_source_means_everything_sits_at_ambient(tmp_path):
    """A sanity floor: with nothing driving it, the solution is the ambient field."""
    geometry = Regions2D(
        bounds=(-0.05, -0.03, 0.05, 0.05),
        regions=[Region2D(name="slab", shape=rect(-0.02, -0.01, 0.02, 0.01),
                          material={"k": K_ALUMINIUM})],
        background={"k": K_AIR},
    )
    result, _ = solve(geometry, tmp_path, t_ambient=20.0)
    assert result.stats["t_rise"] == pytest.approx(0.0, abs=1e-6)


def test_fluid_is_marked_in_the_mask_not_solved(tmp_path):
    """`mask` marks what was not solved, so a viewer can grey it out."""
    result, _ = solve(heat_sink(5), tmp_path)
    ny, nx = result.data["shape"]
    fluid = np.asarray(result.data["mask"], dtype=bool).reshape(ny, nx)
    temperature = np.asarray(result.data["fields"]["T"]).reshape(ny, nx)
    assert fluid.any() and not fluid.all()
    assert np.allclose(temperature[fluid], 20.0)
    assert temperature[~fluid].max() > 20.0


def test_solid_mask_matches_the_regions(tmp_path):
    del tmp_path
    geometry = heat_sink(3)
    x = np.linspace(-0.05, 0.05, 120)
    y = np.linspace(-0.03, 0.05, 96)
    xx, yy = np.meshgrid(x, y)
    mask = solid_mask(geometry, xx, yy)
    assert mask.any() and not mask.all()
    # A point inside the base is solid; one well above the fins is not.
    assert mask[np.argmin(abs(y - (-0.008))), np.argmin(abs(x - 0.0))]
    assert not mask[np.argmin(abs(y - 0.045)), np.argmin(abs(x - 0.0))]


def test_geometry_that_misses_the_grid_fails_loudly(tmp_path):
    """A region thinner than a cell would otherwise solve an empty problem."""
    sliver = Regions2D(
        bounds=(-0.05, -0.03, 0.05, 0.05),
        regions=[Region2D(name="sliver", shape=rect(0.0001, 0.0001, 0.0002, 0.0002),
                          material={"k": K_ALUMINIUM, "q": 1e6})],
        background={"k": K_AIR},
    )
    with pytest.raises(ValueError, match="no solid cells"):
        solve(sliver, tmp_path, resolution=16)


def test_progress_and_stats_are_reported(tmp_path):
    events = []
    ctx = SolverContext(progress_cb=events.append, artifact_dir=tmp_path)
    params = MockHeat2D.Params(resolution=64, iterations=300, report_every=50, write_vtk=False)
    result = MockHeat2D().solve(heat_sink(3), params, ctx)
    assert [e.iteration for e in events] == sorted(e.iteration for e in events)
    assert all(e.residual is not None for e in events)
    assert result.stats["solid_cells"] > 0
    assert result.stats["t_max"] > result.stats["t_rise"]  # t_max includes ambient
