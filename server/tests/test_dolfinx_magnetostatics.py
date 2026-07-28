"""Tests for the FEniCSx magnetostatics adapter (``pytest -m fenics``).

Skipped automatically where dolfinx is not installed.
"""

import threading

import numpy as np
import pytest

pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")

from geometries import SOLENOID, rect  # noqa: E402

from fenixspoon.geometry import Region2D, Regions2D  # noqa: E402
from fenixspoon.solvers import get_solver  # noqa: E402
from fenixspoon.solvers.base import JobCancelled, SolverContext  # noqa: E402
from fenixspoon.solvers.dolfinx_magnetostatics import DolfinxMagnetostatics2D  # noqa: E402
from fenixspoon.solvers.mock_magnetostatics import MockMagnetostatics2D  # noqa: E402

pytestmark = pytest.mark.fenics


def make_ctx(tmp_path, events=None, cancel_event=None):
    return SolverContext(
        progress_cb=(events.append if events is not None else lambda e: None),
        cancel_event=cancel_event,
        artifact_dir=tmp_path / "artifacts",
    )


def solve(geometry, tmp_path, **params):
    return DolfinxMagnetostatics2D().solve(
        geometry, DolfinxMagnetostatics2D.Params(**params), make_ctx(tmp_path)
    )


def test_adapter_is_registered():
    assert get_solver("dolfinx.magnetostatics2d") is DolfinxMagnetostatics2D
    assert DolfinxMagnetostatics2D.geometry_types == ["regions2d"]


def test_region_tags_cover_the_right_cells(tmp_path):
    """Regression: attributing mesh fragments by centre of mass tagged the whole
    background as core, because the rectangle-minus-regions fragment is centred on the
    origin exactly like the core. Fragment attribution must come from gmsh's own map."""
    result = solve(SOLENOID, tmp_path, mesh_size=0.004)
    points = np.asarray(result.data["points"])
    mu_r = np.asarray(result.data["point_fields"]["mu_r"])

    assert set(np.round(np.unique(mu_r), 6)) == {1.0, 1000.0}
    core_fraction = (mu_r > 100).mean()
    # The core is 0.02 x 0.06 of a 0.12 x 0.12 domain = 8.3% by area; nodes on the
    # interface are counted as core by the display convention, so allow some slack.
    assert 0.05 < core_fraction < 0.20, f"core covers {core_fraction:.1%} of nodes"

    # Nodes tagged as core really are in the core rectangle.
    core_pts = points[mu_r > 100]
    assert np.all(np.abs(core_pts[:, 0]) <= 0.0101)
    assert np.all(np.abs(core_pts[:, 1]) <= 0.0301)


def test_flux_concentrates_in_the_core(tmp_path):
    result = solve(SOLENOID, tmp_path, mesh_size=0.004, write_vtk=False)
    points = np.asarray(result.data["points"])
    mu_r = np.asarray(result.data["point_fields"]["mu_r"])
    b_mag = np.asarray(result.data["point_fields"]["B"])

    core = mu_r > 100
    far_air = (np.abs(points[:, 0]) > 0.04) & (np.abs(points[:, 1]) < 0.03)
    assert b_mag[core].mean() > 3 * b_mag[far_air].mean()


def test_agrees_with_the_mock_solver(tmp_path):
    """Two independent discretizations — Cartesian finite volume and unstructured FEM —
    must agree on the peak vector potential."""
    fem = solve(SOLENOID, tmp_path, mesh_size=0.004, write_vtk=False)
    mock = MockMagnetostatics2D().solve(
        SOLENOID,
        MockMagnetostatics2D.Params(resolution=128, iterations=8000, write_vtk=False),
        make_ctx(tmp_path),
    )
    peak_fem = np.abs(np.asarray(fem.data["point_fields"]["A"])).max()
    peak_mock = np.abs(np.asarray(mock.data["fields"]["A"])).max()
    assert peak_fem == pytest.approx(peak_mock, rel=0.15), (
        f"FEM peak |A| {peak_fem:.4g} vs mock {peak_mock:.4g}"
    )


def test_nested_regions_use_painter_order(tmp_path):
    """A high-permeability core nested inside a coil must win over the coil's material."""
    nested = Regions2D(
        bounds=(-0.06, -0.06, 0.06, 0.06),
        regions=[
            Region2D(
                name="coil", shape=rect(-0.03, -0.03, 0.03, 0.03),
                material={"mu_r": 1.0, "current_density": 2.0e6},
            ),
            Region2D(name="core", shape=rect(-0.01, -0.01, 0.01, 0.01),
                      material={"mu_r": 800.0}),
        ],
        background={"mu_r": 1.0},
    )
    result = solve(nested, tmp_path, mesh_size=0.004, write_vtk=False)
    points = np.asarray(result.data["points"])
    mu_r = np.asarray(result.data["point_fields"]["mu_r"])
    assert set(np.round(np.unique(mu_r), 6)) == {1.0, 800.0}
    core_pts = points[mu_r > 100]
    assert len(core_pts) > 0
    assert np.all(np.abs(core_pts) <= 0.0101 + 1e-9)


def test_mesh2d_payload_and_vtk_artifact(tmp_path):
    ctx = make_ctx(tmp_path)
    result = DolfinxMagnetostatics2D().solve(
        SOLENOID, DolfinxMagnetostatics2D.Params(mesh_size=0.005), ctx
    )
    assert result.kind == "mesh2d"
    points = np.asarray(result.data["points"])
    triangles = np.asarray(result.data["triangles"])
    assert triangles.min() >= 0 and triangles.max() < len(points)
    for name in ("A", "B", "mu_r"):
        assert len(result.data["point_fields"][name]) == len(points)

    assert [a["name"] for a in ctx.artifacts] == ["solution.vtk"]
    content = (tmp_path / "artifacts" / "solution.vtk").read_text()
    assert "DATASET UNSTRUCTURED_GRID" in content
    assert "SCALARS A" in content and "SCALARS B" in content


def test_concurrent_meshing_is_serialized(tmp_path):
    """Gmsh keeps global process state and the job manager runs solvers on a thread pool,
    so concurrent meshing must be serialized or it corrupts meshes / crashes the process.
    Runs both dolfinx adapters at once, which is the case a per-adapter lock would miss."""
    from concurrent.futures import ThreadPoolExecutor

    from fenixspoon.geometry import Domain2D, Polygon2D
    from fenixspoon.solvers.dolfinx_poisson import DolfinxPotentialFlow2D

    airfoil = Domain2D(
        bounds=(-1.0, -1.0, 2.0, 1.0),
        obstacle=Polygon2D(points=[(0.0, 0.0), (0.35, 0.09), (1.0, 0.0), (0.35, -0.06)]),
    )

    def magnetostatics(_):
        return solve(SOLENOID, tmp_path, mesh_size=0.008, write_vtk=False)

    def potential_flow(_):
        return DolfinxPotentialFlow2D().solve(
            airfoil,
            DolfinxPotentialFlow2D.Params(mesh_size=0.1, resolution=48, write_vtk=False),
            make_ctx(tmp_path),
        )

    jobs = [magnetostatics, potential_flow] * 3
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda fn: fn(None), jobs))

    assert all(r.kind in ("mesh2d", "grid2d") for r in results)
    for r in results:
        values = (
            r.data["point_fields"] if r.kind == "mesh2d" else r.data["fields"]
        )
        for name, arr in values.items():
            assert np.isfinite(np.asarray(arr)).all(), f"{name} has non-finite values"


def test_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        DolfinxMagnetostatics2D().solve(
            SOLENOID,
            DolfinxMagnetostatics2D.Params(mesh_size=0.01),
            make_ctx(tmp_path, cancel_event=cancel),
        )
