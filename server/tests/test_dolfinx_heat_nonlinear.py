"""Newton on a temperature-dependent conductivity (``pytest -m fenics``), #107.

Skipped automatically where dolfinx is not installed — and the CI job that runs these imports
it first, so a skip here cannot look like a pass.

The load-bearing test is the closed form, not the cross-validation, and that ordering is
different from the other pairs here. Picard and Newton converge to the *same* fixed point
rather than erring in opposite directions, so their agreement is much weaker evidence than
the modal pair's bracketing: two routes to one answer can both be wrong in the same way if
the shared thing — the material law, the boundary treatment — is wrong. The Kirchhoff
transform is outside both.
"""

import numpy as np
import pytest

pytest.importorskip("dolfinx", reason="requires dolfinx (FEniCSx)")
pytest.importorskip("gmsh", reason="requires the gmsh Python API")

from geometries import slab_of  # noqa: E402

from fenixspoon.solvers import get_solver  # noqa: E402
from fenixspoon.solvers.base import JobCancelled, SolverContext  # noqa: E402
from fenixspoon.solvers.dolfinx_heat_nonlinear import DolfinxNonlinearHeat2D  # noqa: E402
from fenixspoon.solvers.mock_heat_nonlinear import (  # noqa: E402
    MockNonlinearHeat2D,
    slab_temperature,
)

pytestmark = pytest.mark.fenics

LENGTH, K0, T_REF = 1.0, 50.0, 20.0
T_HOT, T_COLD = 300.0, 20.0


def make_ctx(tmp_path, cancel_event=None):
    return SolverContext(
        progress_cb=lambda event: None,
        cancel_event=cancel_event,
        artifact_dir=tmp_path / "artifacts",
        conditions=None,
    )


def solve(tmp_path, beta=-4.0e-4, **params):
    params.setdefault("write_vtk", False)
    params.setdefault("t_hot", T_HOT)
    params.setdefault("t_cold", T_COLD)
    return DolfinxNonlinearHeat2D().solve(
        slab_of(beta), DolfinxNonlinearHeat2D.Params(**params), make_ctx(tmp_path)
    )


def sampled_on_the_centreline(result):
    """Nodal values near mid-height, sorted by x. A mesh has no rows, so this picks a band."""
    points = np.asarray(result.data["points"])
    temperature = np.asarray(result.data["point_fields"]["T"])
    band = np.abs(points[:, 1] - 0.1) < 0.02
    order = np.argsort(points[band, 0])
    return points[band, 0][order], temperature[band][order]


def test_adapter_is_registered():
    assert get_solver("dolfinx.heat_nonlinear2d") is DolfinxNonlinearHeat2D
    assert DolfinxNonlinearHeat2D.physics == "heat-conduction-nonlinear"


@pytest.mark.parametrize("beta", [0.0, -4.0e-4, 1.5e-3])
def test_the_slab_matches_the_kirchhoff_closed_form(tmp_path, beta):
    result = solve(tmp_path, beta=beta, mesh_size=0.02)
    x, temperature = sampled_on_the_centreline(result)
    exact = slab_temperature(x, LENGTH, K0, beta, T_REF, T_HOT, T_COLD)

    assert result.converged is True
    assert np.max(np.abs(temperature - exact)) < 0.05, "over a 280 K span"


def test_a_linear_problem_takes_one_newton_step(tmp_path):
    """At `beta = 0` the operator does not depend on the solution, so Newton is exact at once.

    The cheapest possible check on the step, and the one that would have caught the sign error
    CI found: with the correction added instead of subtracted, a linear problem's residual
    *doubles* every iteration — the history read 1, 2, 4, 8, 16 rather than falling to zero.
    A wrong-signed Newton still looks plausible on a nonlinear case, where slow divergence
    can be mistaken for a hard problem; on this one it cannot.
    """
    result = solve(tmp_path, beta=0.0, mesh_size=0.04)

    assert result.converged is True
    assert result.iterations <= 2
    assert result.series[0].traces[0].values[-1] < 1e-10


def test_newton_converges_quadratically(tmp_path):
    """What distinguishes this half from the Picard twin, and the reason for the residual curve.

    Quadratic convergence squares the residual each step near the root, so the log falls
    faster than linearly. Asserted as a ratio of successive logs rather than an iteration
    count, because a count is a property of the tolerance and this is a property of the method.
    """
    (curve,) = solve(tmp_path, beta=1.5e-3, mesh_size=0.03, tolerance=1e-10).series
    residuals = [r for r in curve.traces[0].values if r > 0]

    assert len(residuals) >= 3
    logs = np.log10(residuals[-3:])
    assert (logs[1] - logs[2]) > 1.5 * (logs[0] - logs[1])


def test_agrees_with_the_picard_twin(tmp_path):
    """The second check, and deliberately the second.

    Both halves are already anchored to the closed form, so this is asking a narrower
    question: that the two discretisations of one problem land in the same place. A
    disagreement here after both pass above would mean one of them is right for the slab and
    wrong for a mesh, which is exactly the failure a uniform-material benchmark cannot see.
    """
    fem = solve(tmp_path, mesh_size=0.02)
    mock = MockNonlinearHeat2D().solve(
        slab_of(-4.0e-4),
        MockNonlinearHeat2D.Params(
            resolution=120, t_hot=T_HOT, t_cold=T_COLD, write_vtk=False
        ),
        make_ctx(tmp_path),
    )

    assert fem.metrics["k_ratio"] == pytest.approx(mock.metrics["k_ratio"], rel=0.01)
    # The peak is read out of the payloads rather than out of `metrics`. `t_max` is a
    # *declared* reduction, so it is filled by `fill_declared_metrics` after the runtime gets
    # the result — calling `solve` directly, as this test does, returns only what the adapter
    # supplies. The discovery suite is where the declared metrics are checked against a real
    # submission; here the field is the honest source.
    peak_fem = max(fem.data["point_fields"]["T"])
    peak_mock = max(mock.data["fields"]["T"])
    assert peak_fem == pytest.approx(peak_mock, rel=0.001)


def test_the_two_halves_report_different_residuals_and_say_so(tmp_path):
    """The whole reason 1.16 makes a capability declare what its residual measures.

    Picard's is a temperature step between iterates; Newton's is the norm of the heat balance
    relative to the first iteration. Both are called `residual` on the wire and comparing them
    would be comparing a temperature to a heat flux — which is why `measures` exists and why
    this test asserts the declarations differ rather than that the numbers agree.
    """
    assert DolfinxNonlinearHeat2D.convergence.method == "newton"
    assert MockNonlinearHeat2D.convergence.method == "picard"
    assert (
        DolfinxNonlinearHeat2D.convergence.measures != MockNonlinearHeat2D.convergence.measures
    )
    # What they *do* agree on is the policy, because the reason for it is the physics rather
    # than the scheme: an iterate short of the fixed point solves the wrong problem either way.
    assert DolfinxNonlinearHeat2D.convergence.on_failure == "fail"
    assert MockNonlinearHeat2D.convergence.on_failure == "fail"


def test_a_solve_capped_short_reports_that_it_did_not_converge(tmp_path):
    """The case the declaration exists for, provoked on purpose rather than waited for.

    One Newton iteration cannot solve a nonlinear problem, so the result comes back
    `converged: false`. The adapter returns it — the *runtime* is what fails the job, from the
    declaration, which is what keeps the policy in one place instead of in fourteen adapters.
    """
    from fenixspoon.solvers.base import ConvergenceNotReached, check_declared_convergence

    result = solve(tmp_path, beta=1.5e-3, mesh_size=0.05, max_iterations=1, tolerance=1e-12)
    assert result.converged is False
    assert result.iterations == 1

    with pytest.raises(ConvergenceNotReached, match="did not reach its tolerance"):
        check_declared_convergence(DolfinxNonlinearHeat2D, result)


def test_cancellation(tmp_path):
    import threading

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        DolfinxNonlinearHeat2D().solve(
            slab_of(-4.0e-4),
            DolfinxNonlinearHeat2D.Params(mesh_size=0.05),
            make_ctx(tmp_path, cancel_event=cancel),
        )
