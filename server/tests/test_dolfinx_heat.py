"""Tests for the FEniCSx heat adapter (``pytest -m fenics``).

Skipped automatically where dolfinx is not installed.

The test that justifies this adapter existing is
:func:`test_agrees_with_the_mock_solver`. `mock.heat2d` already proves it balances energy
*internally*, but a discretisation can satisfy its own conservation law and still solve
the wrong problem. Two independent schemes — a Cartesian finite-volume relaxation on a
raster and P1 finite elements on an unstructured mesh — landing on the same chip
temperature is the first real evidence either of them is right.
"""

import numpy as np
import pytest

pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")

from geometries import rect  # noqa: E402

from fenixspoon.geometry import Region2D, Regions2D  # noqa: E402
from fenixspoon.solvers import get_solver  # noqa: E402
from fenixspoon.solvers.base import SolverContext  # noqa: E402
from fenixspoon.solvers.dolfinx_heat import DolfinxHeat2D  # noqa: E402
from fenixspoon.solvers.mock_heat import MockHeat2D  # noqa: E402

pytestmark = pytest.mark.fenics

K_ALUMINIUM = 205.0


def make_ctx(tmp_path):
    return SolverContext(progress_cb=lambda e: None, artifact_dir=tmp_path / "artifacts")


def heat_sink(fins: int) -> Regions2D:
    """The same geometry the mock solver's tests use, so the two are comparable."""
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
        background={"k": 0.026},
    )


def solve(geometry, tmp_path, **params):
    return DolfinxHeat2D().solve(geometry, DolfinxHeat2D.Params(**params), make_ctx(tmp_path))


def test_adapter_is_registered():
    assert get_solver("dolfinx.heat2d") is DolfinxHeat2D
    assert DolfinxHeat2D.geometry_types == ["regions2d"]


def test_agrees_with_the_mock_solver(tmp_path):
    """The cross-validation, and the reason this adapter exists.

    Two unrelated discretisations of the same problem. They will not agree exactly — the
    raster staircases the fin surfaces, which changes the exposed area the convective
    condition sees, and that area *is* the answer here. 15% is the band where agreement
    still means something and geometric discretisation error does not swamp it.
    """
    geometry = heat_sink(5)
    fem_result = solve(geometry, tmp_path, mesh_size=0.0012)
    mock_result = MockHeat2D().solve(
        geometry,
        MockHeat2D.Params(resolution=320, iterations=8000, write_vtk=False),
        make_ctx(tmp_path),
    )

    fem_rise = fem_result.stats["t_rise"]
    mock_rise = mock_result.stats["t_rise"]
    assert fem_rise == pytest.approx(mock_rise, rel=0.15), (
        f"FEM {fem_rise:.2f} K vs mock {mock_rise:.2f} K"
    )
    # Both must be solving a problem that is actually driven, not converging to ambient.
    assert fem_rise > 1.0


def test_energy_balances_at_convergence(tmp_path):
    """Power in equals power convected out — the same physics assertion as the mock's.

    Computed from the returned nodal field and the mesh, not from anything the solver
    kept, so this checks the answer a client receives.
    """
    geometry = heat_sink(5)
    result = solve(geometry, tmp_path, mesh_size=0.0012, h=25.0, t_ambient=20.0)

    points = np.asarray(result.data["points"])
    triangles = np.asarray(result.data["triangles"])
    temperature = np.asarray(result.data["point_fields"]["T"])

    # Power in: the chip's source density over the chip's area.
    chip = next(r for r in geometry.regions if r.name == "chip")
    xs = [p[0] for p in chip.shape.points]
    ys = [p[1] for p in chip.shape.points]
    chip_area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    power_in = float(chip.material["q"]) * chip_area

    # Power out: h * (T - T_ambient) integrated over the boundary. Boundary edges are the
    # ones belonging to exactly one triangle.
    edges: dict[tuple[int, int], int] = {}
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edges[(min(a, b), max(a, b))] = edges.get((min(a, b), max(a, b)), 0) + 1
    power_out = 0.0
    for (a, b), count in edges.items():
        if count != 1:
            continue
        length = float(np.linalg.norm(points[a] - points[b]))
        mean_excess = 0.5 * (temperature[a] + temperature[b]) - 20.0
        power_out += 25.0 * mean_excess * length

    assert power_in > 0
    assert power_out == pytest.approx(power_in, rel=0.05), (
        f"in {power_in:.1f} W/m, out {power_out:.1f} W/m"
    )


def test_more_fins_cool_the_chip(tmp_path):
    """The example's whole lesson, asserted on the FEM path too."""
    rises = [solve(heat_sink(n), tmp_path, mesh_size=0.002).stats["t_rise"] for n in (0, 2, 5)]
    assert rises == sorted(rises, reverse=True), rises
    assert rises[0] > 1.5 * rises[-1], rises


def test_stronger_convection_cools(tmp_path):
    gentle = solve(heat_sink(5), tmp_path, mesh_size=0.002, h=10.0)
    forced = solve(heat_sink(5), tmp_path, mesh_size=0.002, h=200.0)
    assert forced.stats["t_rise"] < gentle.stats["t_rise"] / 5


def test_a_hotter_ambient_shifts_the_whole_solution(tmp_path):
    """Conduction is linear: only the offset moves, the rise is unchanged."""
    cool = solve(heat_sink(3), tmp_path, mesh_size=0.002, t_ambient=20.0)
    warm = solve(heat_sink(3), tmp_path, mesh_size=0.002, t_ambient=60.0)
    assert warm.stats["t_max"] == pytest.approx(cool.stats["t_max"] + 40.0, abs=0.2)
    assert warm.stats["t_rise"] == pytest.approx(cool.stats["t_rise"], rel=1e-3)


def test_only_the_regions_are_meshed(tmp_path):
    """The modelling decision, as an assertion: the fluid must not be in the mesh.

    If the background were meshed, nodes would appear well above the fins where there is
    nothing but air, and the fins would stop working.
    """
    result = solve(heat_sink(5), tmp_path, mesh_size=0.002)
    points = np.asarray(result.data["points"])
    # The tallest solid is a fin topping out at y = 0.035; the domain runs to y = 0.05.
    assert points[:, 1].max() <= 0.0351, "mesh extends above the solid — the fluid got meshed"
    assert points[:, 1].min() >= -0.0201, "mesh extends below the chip"


def test_a_geometry_with_no_regions_never_reaches_the_solver():
    """"Nothing to mesh" is refused by the protocol, before any adapter sees it.

    `regions` is declared `min_length=1`, so this fails at validation with a 422 rather
    than inside a solver. The adapter keeps its own guard for the same condition, but that
    branch is unreachable through the API — asserting it here would be testing the guard,
    not the behaviour.
    """
    with pytest.raises(ValueError):
        Regions2D(bounds=(-0.05, -0.03, 0.05, 0.05), regions=[], background={"k": 0.026})
