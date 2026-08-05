"""Tests for the FEniCSx axisymmetric electrostatics adapter (``pytest -m fenics``).

Skipped automatically where dolfinx is not installed.

The load-bearing test is :func:`test_the_coaxial_capacitance_matches_the_closed_form`, and
it is the same one the mock half is held to: a coaxial section has a capacitance with a
closed form, and the form does not survive the `r` weight being dropped. Two independent
discretisations against one external answer is a stronger statement than either alone —
which is the argument the gallery makes for shipping every physics twice.
"""

import threading

import numpy as np
import pytest

pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")

from geometries import (  # noqa: E402
    CAPACITIVE_SENSOR,
    COAX,
    ROD_ON_AXIS,
    SENSOR_GAP,
    SENSOR_R1,
    SENSOR_R2,
)

from fenixspoon.solvers import get_solver  # noqa: E402
from fenixspoon.solvers.base import JobCancelled, SolverContext  # noqa: E402
from fenixspoon.solvers.dolfinx_electrostatics import DolfinxElectrostaticsAxi2D  # noqa: E402
from fenixspoon.solvers.mock_electrostatics import (  # noqa: E402
    EPS0,
    MockElectrostaticsAxi2D,
)

pytestmark = pytest.mark.fenics

COAX_CONDITIONS = {"inner": {"voltage": 1.0}, "outer": {"voltage": 0.0}}


def make_ctx(tmp_path, conditions=None, events=None, cancel_event=None):
    return SolverContext(
        progress_cb=(events.append if events is not None else lambda e: None),
        cancel_event=cancel_event,
        artifact_dir=tmp_path / "artifacts",
        conditions=conditions,
    )


def solve(geometry, tmp_path, conditions=None, **params):
    return DolfinxElectrostaticsAxi2D().solve(
        geometry, DolfinxElectrostaticsAxi2D.Params(**params), make_ctx(tmp_path, conditions)
    )


def test_adapter_is_registered():
    assert get_solver("dolfinx.electrostatics_axi2d") is DolfinxElectrostaticsAxi2D
    assert DolfinxElectrostaticsAxi2D.geometry_types == ["axisymmetric2d"]


def test_the_coaxial_capacitance_matches_the_closed_form(tmp_path):
    """`C = 2*pi*eps*L/ln(b/a)`, and nothing in this section approximates it away.

    The z truncation is not an approximation here: the exact solution does not vary with z,
    so a zero-flux plane is a symmetry. What is left is the mesh, and an unstructured one
    resolving a 2 mm gap at 50 µm should be well inside a percent.
    """
    a, b, length = 0.001, 0.003, 0.002
    result = solve(COAX, tmp_path, COAX_CONDITIONS, mesh_size=5.0e-5, write_vtk=False)

    exact = 2.0 * np.pi * EPS0 * length / np.log(b / a)
    assert result.metrics["capacitance"] == pytest.approx(exact, rel=0.01)
    assert result.metrics["energy"] == pytest.approx(0.5 * exact, rel=0.01)


def test_the_potential_profile_is_logarithmic(tmp_path):
    """The field, not only its integral: `ln(b/r)/ln(b/a)` is what the cylindrical operator
    produces, and a plane one would give the straight line this test rejects."""
    result = solve(COAX, tmp_path, COAX_CONDITIONS, mesh_size=5.0e-5, write_vtk=False)
    points = np.asarray(result.data["points"])
    potential = np.asarray(result.data["point_fields"]["V"])

    r = points[:, 0]
    expected = np.log(0.003 / r) / np.log(3.0)
    assert np.max(np.abs(potential - expected)) < 0.01
    assert np.max(np.abs(potential - (0.003 - r) / 0.002)) > 0.05


def test_agrees_with_the_mock_solver(tmp_path):
    """Two discretisations — Cartesian finite volume and unstructured FEM — on the section
    that has no closed form, which is the one where agreement is worth something."""
    fem = solve(CAPACITIVE_SENSOR, tmp_path, mesh_size=2.0e-5, write_vtk=False)
    mock = MockElectrostaticsAxi2D().solve(
        CAPACITIVE_SENSOR,
        MockElectrostaticsAxi2D.Params(resolution=256, iterations=20000, write_vtk=False),
        make_ctx(tmp_path),
    )
    assert fem.metrics["capacitance"] == pytest.approx(
        mock.metrics["capacitance"], rel=0.15
    ), (
        f"FEM {fem.metrics['capacitance']:.5g} F vs mock "
        f"{mock.metrics['capacitance']:.5g} F"
    )


def test_the_sensor_reads_above_its_parallel_plate_estimate(tmp_path):
    """The motivating case. The published sensor sits about 15.7% above the parallel-plate
    value over its annulus, and that excess is fringe and chamfer; this section is the same
    shape rather than the same dimensions, so what is asserted is the sign and a band.

    The mesh matters here in a way it does not for the coax: the fringe is an edge effect, so
    it is the elements *at the chamfer* that decide how much of it the answer contains.
    """
    parallel_plate = EPS0 * np.pi * (SENSOR_R2**2 - SENSOR_R1**2) / SENSOR_GAP
    result = solve(CAPACITIVE_SENSOR, tmp_path, mesh_size=2.0e-5, write_vtk=False)

    capacitance = result.metrics["capacitance"]
    assert capacitance > parallel_plate
    assert capacitance < 1.5 * parallel_plate


def test_a_region_on_the_axis_meshes_and_solves(tmp_path):
    """A region with an edge exactly on r = 0, which the schema allows and Gmsh fragments
    against the rectangle's own edge. Nothing about the axis is special-cased anywhere: the
    natural condition is what the `r` in the integrand produces."""
    result = solve(ROD_ON_AXIS, tmp_path, mesh_size=5.0e-4, write_vtk=False)
    points = np.asarray(result.data["points"])
    potential = np.asarray(result.data["point_fields"]["V"])

    on_axis_in_rod = (points[:, 0] < 1e-9) & (np.abs(points[:, 1]) < 0.004)
    assert on_axis_in_rod.any()
    assert np.allclose(potential[on_axis_in_rod], 1.0)
    # Between the two electrodes and nowhere outside them: the maximum principle, which a
    # stray condition on the axis would break.
    assert potential.min() == pytest.approx(0.0, abs=1e-12)
    assert potential.max() == pytest.approx(1.0)
    assert ((potential > 0.1) & (potential < 0.9)).any()


def test_a_model_with_nothing_held_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no electrode"):
        solve(COAX, tmp_path, mesh_size=2.0e-4, write_vtk=False)


def test_mesh2d_payload_and_vtk_artifact(tmp_path):
    ctx = make_ctx(tmp_path, COAX_CONDITIONS)
    result = DolfinxElectrostaticsAxi2D().solve(
        COAX, DolfinxElectrostaticsAxi2D.Params(mesh_size=1.0e-4), ctx
    )
    assert result.kind == "mesh2d"
    points = np.asarray(result.data["points"])
    triangles = np.asarray(result.data["triangles"])
    assert triangles.min() >= 0 and triangles.max() < len(points)
    for name in ("V", "E", "eps_r"):
        assert len(result.data["point_fields"][name]) == len(points)

    assert [a["name"] for a in ctx.artifacts] == ["solution.vtk"]
    content = (tmp_path / "artifacts" / "solution.vtk").read_text()
    assert "DATASET UNSTRUCTURED_GRID" in content
    assert "SCALARS V" in content and "SCALARS E" in content


def test_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        DolfinxElectrostaticsAxi2D().solve(
            COAX,
            DolfinxElectrostaticsAxi2D.Params(mesh_size=2.0e-4),
            make_ctx(tmp_path, COAX_CONDITIONS, cancel_event=cancel),
        )
