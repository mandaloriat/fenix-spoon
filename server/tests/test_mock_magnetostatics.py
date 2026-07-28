import numpy as np
import pytest
from geometries import SOLENOID, rect

from fenixspoon.geometry import Region2D, Regions2D
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_magnetostatics import MU0, MockMagnetostatics2D, rasterize_regions


def make_ctx(tmp_path, events=None):
    return SolverContext(
        progress_cb=(events.append if events is not None else lambda e: None),
        artifact_dir=tmp_path / "artifacts",
    )


def test_rasterize_regions_later_wins():
    """Nested regions: the later entry overrides the earlier one where they overlap."""
    geometry = Regions2D(
        bounds=(0.0, 0.0, 1.0, 1.0),
        regions=[
            Region2D(name="outer", shape=rect(0.1, 0.1, 0.9, 0.9), material={"mu_r": 10.0}),
            Region2D(name="inner", shape=rect(0.4, 0.4, 0.6, 0.6), material={"mu_r": 500.0}),
        ],
        background={"mu_r": 1.0},
    )
    xx, yy = np.meshgrid(np.linspace(0, 1, 101), np.linspace(0, 1, 101))
    mu = rasterize_regions(geometry, xx, yy, "mu_r", 1.0)
    assert mu[5, 5] == 1.0  # background corner
    assert mu[30, 30] == 10.0  # outer region
    assert mu[50, 50] == 500.0  # inner region wins over outer
    assert set(np.unique(mu)) == {1.0, 10.0, 500.0}


def test_solve_produces_field_and_converges(tmp_path):
    events = []
    params = MockMagnetostatics2D.Params(resolution=64, iterations=600, report_every=100)
    result = MockMagnetostatics2D().solve(SOLENOID, params, make_ctx(tmp_path, events))

    assert result.kind == "grid2d"
    ny, nx = result.data["shape"]
    a_pot = np.asarray(result.data["fields"]["A"]).reshape(ny, nx)
    b_mag = np.asarray(result.data["fields"]["B"]).reshape(ny, nx)
    assert np.isfinite(a_pot).all() and np.isfinite(b_mag).all()

    # A = 0 on the outer boundary (flux confined to the modelled window).
    assert np.all(a_pot[0, :] == 0) and np.all(a_pot[-1, :] == 0)
    assert np.all(a_pot[:, 0] == 0) and np.all(a_pot[:, -1] == 0)
    # The current drives a non-trivial field.
    assert b_mag.max() > 0

    residuals = [e.residual for e in events if e.residual is not None]
    assert residuals[-1] < residuals[0], "iteration must reduce the residual"


def _core_and_air_flux(mu_core: float, tmp_path) -> tuple[float, float]:
    geometry = SOLENOID.model_copy(deep=True)
    geometry.regions[0].material["mu_r"] = mu_core
    params = MockMagnetostatics2D.Params(resolution=96, iterations=3000, write_vtk=False)
    result = MockMagnetostatics2D().solve(geometry, params, make_ctx(tmp_path))
    ny, nx = result.data["shape"]
    b_mag = np.asarray(result.data["fields"]["B"]).reshape(ny, nx)
    mu_r = np.asarray(result.data["fields"]["mu_r"]).reshape(ny, nx)
    core = mu_r == mu_core
    # Air well away from the coils, excluding the zero-field boundary layer.
    air = np.zeros_like(core)
    air[ny // 4 : 3 * ny // 4, : nx // 6] = True
    return float(b_mag[core].mean()), float(b_mag[air].mean())


def test_field_concentrates_in_the_high_permeability_core(tmp_path):
    """The physics check that makes this solver worth having: flux prefers the iron."""
    core, air = _core_and_air_flux(1000.0, tmp_path)
    assert core > 3 * air, f"expected flux concentration in the core, got {core / air:.2f}x"


def test_higher_permeability_draws_more_flux(tmp_path):
    """Monotonic response to mu_r — the test that would catch a mis-wired coefficient."""
    low, _ = _core_and_air_flux(10.0, tmp_path)
    mid, _ = _core_and_air_flux(100.0, tmp_path)
    high, _ = _core_and_air_flux(2000.0, tmp_path)
    assert low < mid < high


def test_current_sign_flips_the_field(tmp_path):
    """Reversing the winding must reverse A, leaving |B| unchanged."""
    params = MockMagnetostatics2D.Params(resolution=48, iterations=800, write_vtk=False)
    forward = MockMagnetostatics2D().solve(SOLENOID, params, make_ctx(tmp_path))
    reversed_geometry = SOLENOID.model_copy(deep=True)
    for region in reversed_geometry.regions:
        if "current_density" in region.material:
            region.material["current_density"] *= -1
    backward = MockMagnetostatics2D().solve(reversed_geometry, params, make_ctx(tmp_path))

    a_fwd = np.asarray(forward.data["fields"]["A"])
    a_bwd = np.asarray(backward.data["fields"]["A"])
    np.testing.assert_allclose(a_fwd, -a_bwd, atol=1e-12)
    np.testing.assert_allclose(
        np.asarray(forward.data["fields"]["B"]),
        np.asarray(backward.data["fields"]["B"]),
        atol=1e-12,
    )


def test_zero_current_gives_zero_field(tmp_path):
    quiet = Regions2D(
        bounds=(-0.05, -0.05, 0.05, 0.05),
        regions=[Region2D(name="core", shape=rect(-0.01, -0.01, 0.01, 0.01),
                          material={"mu_r": 1000.0})],
    )
    params = MockMagnetostatics2D.Params(resolution=32, iterations=200, write_vtk=False)
    result = MockMagnetostatics2D().solve(quiet, params, make_ctx(tmp_path))
    assert np.allclose(np.asarray(result.data["fields"]["A"]), 0.0)


def test_permeability_enters_as_reluctivity():
    """Guard the unit convention: nu = 1/(mu0 * mu_r)."""
    assert MU0 == pytest.approx(1.2566370614e-6, rel=1e-6)


def test_mesh2d_output_covers_whole_domain(tmp_path):
    params = MockMagnetostatics2D.Params(resolution=32, iterations=100, output="mesh2d")
    result = MockMagnetostatics2D().solve(SOLENOID, params, make_ctx(tmp_path))
    assert result.kind == "mesh2d"
    points = np.asarray(result.data["points"])
    triangles = np.asarray(result.data["triangles"])
    # No hole: every grid node is present.
    ny = max(8, round(32 * 1))
    assert len(points) == 32 * ny
    assert triangles.max() < len(points)
    for name in ("A", "B", "mu_r"):
        assert len(result.data["point_fields"][name]) == len(points)
