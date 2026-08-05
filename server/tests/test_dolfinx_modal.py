"""Tests for the FEniCSx eigensolver (``pytest -m fenics``).

Skipped automatically where dolfinx or slepc4py is not installed — and the CI job that runs
these imports both first, so a skip here cannot look like a pass.

The load-bearing tests are the two closed forms the mock is held to as well. Cross-validating
the pair is the *second* check rather than the first, and it is worth having precisely because
the two discretisations err in opposite directions: this one uses the consistent mass matrix
and over-predicts, the mock lumps and under-predicts, so agreement between them is evidence
rather than a coincidence of shared code.
"""

import math
import threading

import numpy as np
import pytest

pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")
pytest.importorskip("slepc4py", reason="requires slepc4py (SLEPc)")

from geometries import UNIFORM_BEAM  # noqa: E402

from fenixspoon.solvers import get_solver  # noqa: E402
from fenixspoon.solvers.base import JobCancelled, SolverContext  # noqa: E402
from fenixspoon.solvers.dolfinx_modal import DolfinxModal2D  # noqa: E402
from fenixspoon.solvers.mock_modal import MockModal2D  # noqa: E402

pytestmark = pytest.mark.fenics

LENGTH, DEPTH = 1.0, 0.1
YOUNGS, DENSITY = 2.1e11, 7850.0


def beam_frequency(root: float) -> float:
    """The Euler-Bernoulli frequency for a tabulated root; see `test_modal.py`."""
    inertia = DEPTH**3 / 12.0
    return (root**2 / (2.0 * math.pi)) * math.sqrt(
        YOUNGS * inertia / (DENSITY * DEPTH * LENGTH**4)
    )


def make_ctx(tmp_path, conditions=None, cancel_event=None):
    return SolverContext(
        progress_cb=lambda event: None,
        cancel_event=cancel_event,
        artifact_dir=tmp_path / "artifacts",
        conditions=conditions,
    )


def solve(tmp_path, geometry=UNIFORM_BEAM, **params):
    params.setdefault("write_vtk", False)
    return DolfinxModal2D().solve(geometry, DolfinxModal2D.Params(**params), make_ctx(tmp_path))


def test_adapter_is_registered():
    assert get_solver("dolfinx.modal2d") is DolfinxModal2D
    assert DolfinxModal2D.physics == "elasticity-modal"


def test_the_cantilever_matches_the_euler_bernoulli_frequency(tmp_path):
    """The tabulated 1.875 root, on a quadratic element that resolves bending properly."""
    result = solve(tmp_path, mesh_size=0.02, degree=2, fixed_edge="xmin", n_modes=4)
    assert result.metrics["rigid_body_modes"] == 0
    assert result.metrics["f_1"] == pytest.approx(beam_frequency(1.875104), rel=0.05)


def test_the_free_free_bar_matches_its_own_closed_form(tmp_path):
    """The 4.730 root, and the case the negative shift exists for.

    An unrestrained structure has a singular stiffness matrix, so this test is also what says
    the shift-and-invert transform is set up correctly: at a shift of exactly zero the
    factorisation would fail rather than return the wrong number.
    """
    result = solve(tmp_path, mesh_size=0.02, degree=2, n_modes=6, mode=4)
    assert result.metrics["rigid_body_modes"] == 3
    assert result.metrics["f_1"] == pytest.approx(beam_frequency(4.730041), rel=0.05)


def test_the_zero_modes_are_zero(tmp_path):
    """Not merely small: many orders below the first elastic mode, which is what makes the
    count a check rather than a threshold."""
    result = solve(tmp_path, mesh_size=0.03, n_modes=6, mode=4)
    frequencies = result.series[0].traces[0].values
    assert max(frequencies[:3]) < 1e-4 * frequencies[3]
    assert result.series[0].traces[1].values[:4] == [1.0, 1.0, 1.0, 0.0]


def test_agrees_with_the_mock_solver(tmp_path):
    """Two discretisations that err in opposite directions, on one structure.

    The band is wide because that is the honest width: a lumped mass matrix on constant-strain
    triangles and a consistent one on quadratic elements are not expected to agree closely,
    and a tight tolerance here would be a tolerance tuned until it passed.
    """
    fem = solve(tmp_path, mesh_size=0.02, degree=2, fixed_edge="xmin", n_modes=4)
    mock = MockModal2D().solve(
        UNIFORM_BEAM,
        MockModal2D.Params(resolution=64, n_modes=4, fixed_edge="xmin", write_vtk=False),
        make_ctx(tmp_path),
    )
    assert fem.metrics["f_1"] == pytest.approx(mock.metrics["f_1"], rel=0.10), (
        f"FEniCSx {fem.metrics['f_1']:.4g} Hz vs mock {mock.metrics['f_1']:.4g} Hz"
    )


def test_every_mode_shape_is_an_indexed_artifact(tmp_path):
    ctx = make_ctx(tmp_path)
    result = DolfinxModal2D().solve(
        UNIFORM_BEAM, DolfinxModal2D.Params(mesh_size=0.05, n_modes=4, mode=4), ctx
    )
    registered = ctx.artifacts
    assert [item["name"] for item in registered] == [f"mode_{i}.vtk" for i in (1, 2, 3, 4)]
    assert [item["mode"] for item in registered] == [1, 2, 3, 4]
    assert all(item.get("t") is None for item in registered)

    points = np.asarray(result.data["points"])
    assert len(result.data["point_fields"]["u_mag"]) == len(points)
    content = (tmp_path / "artifacts" / "mode_4.vtk").read_text()
    assert "DATASET UNSTRUCTURED_GRID" in content and "SCALARS u_mag" in content


def test_a_load_case_restrains_the_structure(tmp_path):
    """The same load case the elasticity pair reads, doing the half that has meaning here."""
    from fenixspoon.geometry import BoundarySpec, NearSelector

    beam = UNIFORM_BEAM.model_copy(
        update={"boundaries": [BoundarySpec(name="root", select=NearSelector(axis="x", value=0.0))]}
    )
    result = DolfinxModal2D().solve(
        beam,
        DolfinxModal2D.Params(mesh_size=0.02, degree=2, n_modes=4, write_vtk=False),
        make_ctx(tmp_path, conditions={"root": {"fixed": 1.0}}),
    )
    assert result.metrics["rigid_body_modes"] == 0
    assert result.metrics["f_1"] == pytest.approx(beam_frequency(1.875104), rel=0.05)


def test_the_closed_sets_are_typed_as_closed_sets():
    """`plane` and `fixed_edge` are enums, so a wrong value is a 422 rather than a solve.

    Raised in review of #101, and worth the test rather than only the fix: as bare strings,
    an unknown `plane` reached `lame()` and fell through to the plane-strain branch — the
    wrong physics with no symptom — while an unknown `fixed_edge` raised a `KeyError` from
    inside `edge_locator`. Typed, both are refused at submit with the choices listed, and
    `capability.describe` reports them as enums so a form offers a picker.
    """
    from pydantic import ValidationError

    for bad in ({"plane": "planar"}, {"fixed_edge": "left"}):
        with pytest.raises(ValidationError):
            DolfinxModal2D.Params(**bad)
    # And the schema says so, which is what a form generator and `capability.describe` read.
    properties = DolfinxModal2D.Params.model_json_schema()["properties"]
    assert properties["plane"]["enum"] == ["stress", "strain"]
    assert "none" in properties["fixed_edge"]["enum"]


def test_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        DolfinxModal2D().solve(
            UNIFORM_BEAM,
            DolfinxModal2D.Params(mesh_size=0.05, n_modes=4),
            make_ctx(tmp_path, cancel_event=cancel),
        )
