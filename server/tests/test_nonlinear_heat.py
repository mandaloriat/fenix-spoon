"""Temperature-dependent conduction, and the convergence vocabulary it put under load (#107).

Two things are being tested and they are worth keeping apart in the reading.

**The physics** is held to a closed form rather than to the other adapter. With
`k(T) = k0(1 + beta(T - T_ref))` across a slab, the Kirchhoff transform makes the transformed
variable linear in `x`, so `T(x)` is the root of a quadratic — an answer that exists
independently of anything in this repository. That matters more here than it did for the
modal pair: Picard and Newton converge to the *same* fixed point rather than bracketing it
from opposite sides, so their agreement is weaker evidence than usual and something outside
has to anchor it.

**The vocabulary** is the part #107 was actually filed about. `converged`, `residual` and a
warning have been on a result since 1.3, and nothing declared them — so the tests below are
as much about what a capability *says* it will do when it does not converge as about whether
it converges.
"""

import numpy as np
import pytest
from geometries import SLAB, slab_of

from fenixspoon.solvers import get_solver
from fenixspoon.solvers.base import (
    ConvergenceNotReached,
    JobCancelled,
    SolverContext,
    SolverResult,
    check_declared_convergence,
)
from fenixspoon.solvers.mock_heat_nonlinear import MockNonlinearHeat2D, slab_temperature

LENGTH = 1.0
K0, T_REF = 50.0, 20.0
T_HOT, T_COLD = 300.0, 20.0


def make_ctx(tmp_path, cancel_event=None):
    return SolverContext(
        progress_cb=lambda event: None,
        cancel_event=cancel_event,
        artifact_dir=tmp_path / "artifacts",
        conditions=None,
    )


def solve(tmp_path, geometry=None, **params):
    params.setdefault("write_vtk", False)
    return MockNonlinearHeat2D().solve(
        geometry if geometry is not None else SLAB,
        MockNonlinearHeat2D.Params(**params),
        make_ctx(tmp_path),
    )


def centreline(result) -> tuple[np.ndarray, np.ndarray]:
    ny, nx = result.data["shape"]
    field = np.asarray(result.data["fields"]["T"]).reshape(ny, nx)
    return np.linspace(0.0, LENGTH, nx), field[ny // 2]


# ------------------------------------------------------------------------- the physics


def test_adapter_is_registered():
    assert get_solver("mock.heat_nonlinear2d") is MockNonlinearHeat2D
    assert MockNonlinearHeat2D.physics == "heat-conduction-nonlinear"


@pytest.mark.parametrize("beta", [0.0, -4.0e-4, 1.5e-3])
def test_the_slab_matches_the_kirchhoff_closed_form(tmp_path, beta):
    """The check that is not this repository marking its own homework.

    `beta = 0` is in the list on purpose: it is the linear problem, whose answer is a
    straight line, and a nonlinear solver that cannot reproduce it has nothing else worth
    checking.
    """
    result = solve(tmp_path, geometry=slab_of(beta), resolution=120, t_hot=T_HOT, t_cold=T_COLD)
    x, temperature = centreline(result)
    exact = slab_temperature(x, LENGTH, K0, beta, T_REF, T_HOT, T_COLD)

    assert np.max(np.abs(temperature - exact)) < 1e-3, "over a 280 K span"


def test_the_nonlinear_answer_is_not_the_linear_one(tmp_path):
    """Otherwise the test above would pass for a solver that ignored `beta` entirely.

    A 12% swing in conductivity bends the profile about 4 K away from the straight line, which
    is a hundred times the tolerance the closed-form test uses — so the two tests together
    say the adapter is right *and* that being right required the nonlinearity.
    """
    x, temperature = centreline(
        solve(tmp_path, geometry=slab_of(-4.0e-4), resolution=120, t_hot=T_HOT, t_cold=T_COLD)
    )
    straight_line = T_HOT + (T_COLD - T_HOT) * x / LENGTH

    assert np.max(np.abs(temperature - straight_line)) > 3.0


def test_a_linear_material_converges_immediately(tmp_path):
    """With `beta = 0` the operator does not depend on the solution, so the first Picard step
    is already the fixed point and the second confirms it."""
    result = solve(tmp_path, geometry=slab_of(0.0), resolution=64)

    assert result.iterations <= 2
    assert result.metrics["k_ratio"] == pytest.approx(1.0)


def test_k_ratio_reports_how_nonlinear_the_answer_was(tmp_path):
    """The metric that can tell a caller it did not need this capability.

    `beta = -4e-4` over a 280 K span is a 11% spread in `k`, so the ratio lands near 1.12 —
    and at `beta = 0` it is exactly 1, which is the adapter saying "a linear solve would have
    answered this".
    """
    result = solve(tmp_path, geometry=slab_of(-4.0e-4), resolution=80, t_hot=T_HOT, t_cold=T_COLD)

    assert result.metrics["k_ratio"] == pytest.approx(1.0 / (1.0 - 4.0e-4 * 280.0), rel=0.02)


def test_the_convergence_history_comes_back_as_a_curve(tmp_path):
    """*How* it converged is the caller's likeliest next question, so it is in the result."""
    result = solve(tmp_path, geometry=slab_of(1.5e-3), resolution=64)
    (curve,) = result.series

    assert curve.name == "convergence"
    assert len(curve.traces[0].values) == result.iterations
    assert curve.traces[0].values[-1] == pytest.approx(result.residual)


def test_one_face_cannot_be_held_at_two_temperatures(tmp_path):
    with pytest.raises(ValueError, match="both"):
        solve(tmp_path, hot_edge="xmin", cold_edge="xmin")


def test_cancellation(tmp_path):
    import threading

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        MockNonlinearHeat2D().solve(
            SLAB,
            MockNonlinearHeat2D.Params(resolution=32, write_vtk=False),
            make_ctx(tmp_path, cancel_event=cancel),
        )


# ---------------------------------------------------------------------- the vocabulary


def test_the_capability_declares_what_its_residual_measures():
    """The gap #107 was filed about: `residual` was a bare float on every adapter.

    Picard's is a step size and Newton's is a heat balance, so without the declaration the
    two halves of this pair report numbers a caller would reasonably — and wrongly — compare.
    """
    spec = MockNonlinearHeat2D.convergence

    assert spec is not None
    assert spec.method == "picard"
    assert "temperature change" in spec.measures
    assert spec.on_failure == "fail"


def test_a_capability_that_promises_to_fail_is_held_to_it():
    """The refusal that makes the declaration a contract rather than a comment.

    An adapter declaring `on_failure="fail"` and returning `converged: false` has stated a
    policy and not implemented it — and a caller who read the declaration reasonably stopped
    checking the flag, which is why this raises rather than warns.
    """
    unconverged = SolverResult(
        kind="grid2d", data={}, converged=False, residual=1e-2, iterations=40
    )

    with pytest.raises(ConvergenceNotReached, match="did not reach its tolerance"):
        check_declared_convergence(MockNonlinearHeat2D, unconverged)


def test_a_relaxation_preview_is_allowed_to_come_back_unconverged():
    """The other half of the same rule, and the reason `on_failure` is a choice at all.

    `mock.heat2d`'s own "fast preview" example says the temperature is not converged and the
    shape is. That is a feature; the same result from a Newton solve would be a field that
    solves nothing.
    """
    heat = get_solver("mock.heat2d")
    result = SolverResult(kind="grid2d", data={}, converged=False, residual=3.0)

    assert heat.convergence.on_failure == "return"
    check_declared_convergence(heat, result)  # does not raise


def test_saying_no_without_declaring_anything_is_refused():
    """A `converged: false` from a capability with no spec is unreadable by construction.

    There is no tolerance to compare the residual against, no meaning for the residual, and
    no statement of whether the iterate is usable — so the flag tells a caller only that
    something is wrong, which is the shape of a plausible answer rather than a verifiable one.
    """

    class Undeclared(MockNonlinearHeat2D):
        name = "test.undeclared"
        convergence = None

    unreadable = SolverResult(kind="grid2d", data={}, converged=False)
    with pytest.raises(ConvergenceNotReached, match="declares no"):
        check_declared_convergence(Undeclared, unreadable)


def test_saying_yes_needs_no_declaration():
    """The asymmetry, stated as a test because it is the part that looks like an oversight.

    Every FEniCSx adapter here reports `converged: true` off a direct LU factorisation, which
    is trivially true and needs nothing to read it against. Requiring a spec for that would
    have made seven adapters declare an iteration none of them performs.
    """

    class Direct(MockNonlinearHeat2D):
        name = "test.direct"
        convergence = None

    check_declared_convergence(Direct, SolverResult(kind="grid2d", data={}, converged=True))


def test_a_declaring_adapter_that_says_nothing_is_taken_to_have_converged():
    """Silence means success, because it returned normally.

    The opposite reading — silence means failure — would fail every solve that worked, which
    is the sort of default that gets a check deleted rather than fixed.
    """
    result = SolverResult(kind="grid2d", data={})
    check_declared_convergence(MockNonlinearHeat2D, result)

    assert result.converged is True


def test_the_iterating_mocks_all_declare_a_spec():
    """Retrofitted with #107, and worth a test rather than a note.

    Six adapters could already return `converged: false` and none of them said what that
    meant. Any new one that can must declare too, or the check above will refuse its results
    at runtime — which is a worse place to find out than here.
    """
    from fenixspoon.solvers import registered_solvers

    iterating = [cls for cls in registered_solvers() if cls.convergence is not None]

    assert len(iterating) >= 7
    assert all(spec.default_tolerance > 0 for spec in (cls.convergence for cls in iterating))
    assert {cls.convergence.on_failure for cls in iterating} == {"return", "fail"}


def test_no_two_capabilities_share_a_convergence_declaration():
    """The guard for the mistake this feature made on its own first draft.

    Metric sets *are* shared, deliberately: two halves of a pair must report the same
    quantities or they are interchangeable only in principle. Convergence is the opposite —
    it describes the *implementation*, and this pair is the proof, since Picard measures a
    temperature step and Newton a heat balance for one problem.

    The first version of #107 missed that and gave six relaxation mocks one shared spec. Four
    of them then advertised a tolerance they do not use: magnetostatics stops at 1e-14, the
    axisymmetric electrostatics scales its tolerance by the domain span, and elasticity is
    conjugate gradients reporting a *force* residual rather than a sweep change. Caught in
    review of #108 — a declaration that lies, shipped in the change whose argument is that
    undeclared numbers are unreadable.

    Sharing the object is what made one edit wrong in four places, so that is what this
    forbids.
    """
    from fenixspoon.solvers import registered_solvers

    declared = [(cls.name, cls.convergence) for cls in registered_solvers() if cls.convergence]
    by_identity: dict[int, list[str]] = {}
    for name, spec in declared:
        by_identity.setdefault(id(spec), []).append(name)

    shared = {names[0]: names for names in by_identity.values() if len(names) > 1}
    assert not shared, f"one ConvergenceSpec object serves several capabilities: {shared}"


def test_a_declared_tolerance_is_the_one_the_solve_uses(tmp_path):
    """The declaration checked against the implementation, on the case where it is checkable.

    A capability with a `tolerance` parameter cannot be pinned this way — the run's tolerance
    is the caller's. But this one's default *is* the declaration, so a solve that reports
    convergence must have a residual under it, and one that does not must not. That is the
    narrow version of the claim `measures` and `default_tolerance` make.
    """
    spec = MockNonlinearHeat2D.convergence
    converged = solve(tmp_path, geometry=slab_of(-4.0e-4), resolution=64)

    assert converged.converged is True
    assert converged.residual < spec.default_tolerance

    # And the other direction, forced: one Picard step cannot solve a nonlinear problem.
    capped = solve(tmp_path, geometry=slab_of(1.5e-3), resolution=64, outer_iterations=1)
    assert capped.converged is False
    assert capped.residual > spec.default_tolerance
