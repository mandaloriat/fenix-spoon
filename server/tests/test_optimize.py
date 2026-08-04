"""Optimization: choosing the next point, which is what a study may not do (#22).

The acceptance criterion is :func:`test_the_search_finds_the_zero_lift_angle`, and it is
checked against the *other* M5 item rather than against a number somebody wrote down: a sweep
tabulates the polar, a search finds where it crosses zero, and the crossing must fall inside
the bracket the search reports. Two things that compute the same answer by different means.

Three groups, and the first one needs no solver at all — which is the argument the module
makes about itself. The **method** is a pure function from the answers so far to the next
point, so it is tested against arithmetic. **Orchestration** covers what running one does to
jobs and the cache. **Specification** covers what an optimization object may say, and matters
for the reason a study's does: a malformed question produces a plausible answer.
"""

import asyncio
import json

import pytest

from fenixspoon.core import FenixSpoonCore, errors, optimize
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.jobs import JobManager

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [
            [1.0, 0.0], [0.7, 0.055], [0.35, 0.09], [0.1, 0.062],
            [0.0, 0.0], [0.1, -0.03], [0.35, -0.045], [0.7, -0.03],
        ],
    },
}


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def design_of(core, me) -> str:
    """A cambered section, cheap enough to solve a dozen times in a test."""
    geometry = core.create_object("geometry", AIRFOIL, me).ref
    return core.create_object(
        "design",
        {
            "solver": "mock.laplace2d",
            "geometry": geometry,
            "params": {"resolution": 48, "iterations": 300, "write_vtk": False},
        },
        me,
    ).ref


def optimization_of(core, me, design, **overrides) -> str:
    body = {
        "design": design,
        "parameter": "alpha",
        "bounds": [-10.0, 10.0],
        "objective": {"metric": "c_l", "sense": "target", "target": 0.0},
        "max_evaluations": 12,
        "tolerance": 0.02,
    }
    body.update(overrides)
    return core.create_object("optimization", body, me).ref


# --------------------------------------------------------------------------- the method


def minimise(objective, bounds=(0.0, 1.0), tolerance=1e-3, budget=60):
    """Drive `next_point` over a plain function — no core, no jobs, no workspace.

    This is what "the method is a pure function of the answers" buys, and running the loop
    here rather than mocking a solver is the point: if this needed a fixture, the claim in the
    module docstring would be false.
    """
    answered: list[float] = []
    points: list[float] = []
    for _ in range(budget):
        point = optimize.next_point(list(bounds), answered, tolerance)
        if point is None:
            break
        points.append(point)
        answered.append(objective(point))
    return points, answered


def test_the_method_finds_a_minimum_of_a_function_it_has_never_seen():
    """Golden section on a parabola: the bracket closes on the vertex, and the best point
    seen lands there. Arithmetic, no solver — which is the whole claim of the ask–tell
    shape."""
    points, answered = minimise(lambda x: (x - 0.3) ** 2)
    lower, upper = optimize.bracket_of([0.0, 1.0], answered)

    assert lower <= 0.3 <= upper
    assert upper - lower <= 1e-3, "the tolerance is a fraction of the span, which is 1 here"
    assert min(points, key=lambda x: (x - 0.3) ** 2) == pytest.approx(0.3, abs=1e-3)


def test_each_evaluation_after_the_first_shrinks_the_bracket_by_the_golden_ratio():
    """What the ratio is *for*: every step but the first reuses an interior point, so the
    bracket shrinks once per solve rather than once per pair of them. On an adapter where an
    evaluation is a finite-element solve, that factor is the running time.

    Stated as the arithmetic rather than as a count of points, because a count is a fencepost
    argument about when the tolerance is checked and this is the claim actually being made.
    """
    objective = lambda x: (x - 0.3) ** 2  # noqa: E731 - the subject of the test, not a helper
    points, answered = minimise(objective, tolerance=1e-6)
    for spent in range(2, 9):
        lower, upper = optimize.bracket_of([0.0, 1.0], answered[:spent])
        assert upper - lower == pytest.approx(optimize.INVPHI ** (spent - 1), rel=1e-9), (
            f"after {spent} evaluations the bracket is not φ⁻{spent - 1} of the span"
        )
    assert min(points, key=objective) == pytest.approx(0.3, abs=1e-5)


def test_the_method_never_learns_which_way_the_caller_meant():
    """`next_point` sees the minimised objective and nothing else, which is why one method
    serves minimise, maximise and target — and why it cannot develop an opinion about any of
    them. Maximising a parabola is minimising its negation, and the same sequence results."""
    up, _ = minimise(lambda x: -((x - 0.7) ** 2))
    down, _ = minimise(lambda x: (x - 0.7) ** 2)
    assert up[:2] == down[:2], "the opening points cannot depend on the objective's shape"
    assert min(down, key=lambda x: (x - 0.7) ** 2) == pytest.approx(0.7, abs=1e-3)


def test_the_bracket_is_where_the_minimum_is_known_to_be_not_where_it_was_seen():
    """Two different claims, and reporting only the first would present a budget-stopped
    search as a located answer."""
    points, answered = minimise(lambda x: (x - 0.3) ** 2, budget=4)
    lower, upper = optimize.bracket_of([0.0, 1.0], answered)
    assert lower <= 0.3 <= upper, "the minimum left the bracket"
    assert upper - lower > 0.02, "four evaluations cannot have located it to 2%"
    assert len(points) == 4


@pytest.mark.parametrize(
    ("sense", "target", "metric", "expected"),
    [
        ("minimize", None, 2.0, 2.0),
        ("maximize", None, 2.0, -2.0),
        ("target", 1.5, 2.0, 0.25),
    ],
)
def test_every_sense_becomes_the_same_minimisation(sense, target, metric, expected):
    objective = optimize.Objective(metric="c_l", sense=sense, target=target)
    assert objective.as_minimum(metric) == pytest.approx(expected)


def test_a_target_is_required_by_one_sense_and_refused_by_the_others():
    """The 1.6 refusal in miniature: a target on a `minimize` is a misunderstanding or a
    leftover, and ignoring it silently would optimise something other than what the body
    appears to ask for."""
    with pytest.raises(ValueError):
        optimize.Objective(metric="c_l", sense="target")
    with pytest.raises(ValueError):
        optimize.Objective(metric="c_l", sense="minimize", target=0.0)


def _swept(core, me, study, timeout: float = 90.0):
    """Run a study and poll until every point is terminal — the study suite's helper, and the
    same reason for one `asyncio.run`: a solve is completed by callbacks on the loop that
    submitted it."""

    async def go():
        await core.run_study(study, me)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            report = core.study_report(study, me)
            if report.complete:
                return report
            assert asyncio.get_running_loop().time() < deadline, "the sweep did not finish"
            await asyncio.sleep(0.02)

    return asyncio.run(go())


# ------------------------------------------------------------------------- acceptance


def test_the_search_finds_the_zero_lift_angle(core, me):
    """The acceptance criterion, checked against the sweep rather than against a constant.

    #22 asked for "optimizes airfoil camber for a lift proxy". Both halves moved for the
    reasons #21's acceptance did: camber is a property of the geometry, which the workspace
    stores as an explicit polygon, and the proxy became a real `c_l` with #68. The angle of
    attack is the parameter, and *zero-lift angle* is the classic question about a cambered
    section — the angle at which it carries no lift.

    The check is the interesting part. A golden number would pin this test to one mock at one
    resolution; instead the same design is swept and searched, and the sweep's polar must
    change sign **inside the bracket the search reports**. Two computations of one answer, one
    of them tabulating and one of them choosing, agreeing where they overlap.
    """
    design = design_of(core, me)
    report = asyncio.run(core.run_optimization(optimization_of(core, me, design), me))

    assert report.stopped == "converged"
    assert report.best is not None
    assert abs(report.best.metric) < 0.01, f"c_l is not near zero: {report.best.metric}"

    lower, upper = report.bracket
    assert upper - lower <= 0.02 * 20.0, "converged means the bracket reached the tolerance"

    # The sweep's own answer to the same question, at the same resolution: the polar either
    # side of the bracket must straddle zero.
    sweep = core.create_object(
        "study",
        {
            "kind": "sweep",
            "design": design,
            "axes": [{"parameter": "alpha", "values": [round(lower, 6), round(upper, 6)]}],
            "metrics": ["c_l"],
        },
        me,
    ).ref
    below, above = (point.metrics["c_l"] for point in _swept(core, me, sweep).points)
    assert below < 0.0 < above, (
        f"the polar does not cross zero inside the bracket the search reported: "
        f"c_l({lower:.4f}) = {below}, c_l({upper:.4f}) = {above}"
    )

    # Compact by the same rule as a study report: scalars, job references and one curve.
    rendered = json.dumps(report.model_dump())
    assert len(rendered) < 8192, f"report is {len(rendered)} bytes"
    assert "fields" not in rendered


def test_the_trajectory_comes_back_as_a_drawable_curve(core, me):
    """`series1d` was introduced for "a sweep, a convergence history" in as many words. This
    is the second of those, so it is the model rather than a shape beside it."""
    report = asyncio.run(core.run_optimization(optimization_of(core, me, design_of(core, me)), me))

    history = report.history
    assert history is not None and history.x is None
    assert [trace.name for trace in history.traces] == ["objective", "c_l"]
    for trace in history.traces:
        assert trace.x.name == "evaluation"
        assert len(trace.values) == len(trace.x.values) == report.evaluations_spent


# ----------------------------------------------------------------------- orchestration


def test_running_it_again_costs_nothing_and_walks_the_same_path(core, me):
    """Why there is no run record. An optimizer is not *predictable* — nothing can enumerate
    its points in advance — but it is **reproducible**, and reproducibility is the property
    storage would have been for. The second run replays the identical sequence and every
    evaluation is a content-addressed cache hit."""
    optimization = optimization_of(core, me, design_of(core, me))
    first = asyncio.run(core.run_optimization(optimization, me))
    again = asyncio.run(core.run_optimization(optimization, me))

    assert [e.value for e in again.evaluations] == [e.value for e in first.evaluations]
    assert all(e.cached for e in again.evaluations), "a replay should solve nothing"
    assert again.best.job_id == first.best.job_id


def test_the_report_replays_the_search_without_running_it(core, me):
    """`optimize.get` resolves each point by content address, exactly as a study report
    resolves a rung — so the trajectory is recovered rather than recorded."""
    optimization = optimization_of(core, me, design_of(core, me))
    ran = asyncio.run(core.run_optimization(optimization, me))

    read = core.optimization_report(optimization, me)
    assert read.evaluations_spent == ran.evaluations_spent
    assert [e.job_id for e in read.evaluations] == [e.job_id for e in ran.evaluations]
    assert read.best.value == ran.best.value and read.stopped == ran.stopped


def test_a_search_nobody_has_run_reports_nothing_rather_than_failing(core, me):
    """An optimization object is a question. Asking what it has found before anyone runs it
    has a true answer, and it is not an error."""
    report = core.optimization_report(optimization_of(core, me, design_of(core, me)), me)

    assert report.stopped == "not_run"
    assert report.evaluations == [] and report.best is None
    assert report.bracket is None and report.history is None


def test_an_evaluation_records_the_optimization_it_came_from(core, me):
    """#44's rule again: what was solved is described by object revisions, and `iteration` is
    this sequence's `variation_index` so a reader of provenance learns one vocabulary."""
    design = design_of(core, me)
    optimization = optimization_of(core, me, design)
    report = asyncio.run(core.run_optimization(optimization, me))

    provenance = core.provenance(report.evaluations[1].job_id, me)
    assert provenance.inputs["optimization"].startswith(f"{optimization}@")
    assert provenance.inputs["variation_index"] == 1
    assert provenance.inputs["variation"] == {"alpha": report.evaluations[1].value}
    assert provenance.inputs["design"].startswith(f"{design}@")


def test_the_budget_stops_the_search_and_the_bracket_says_how_far_it_got(core, me):
    """A search that runs out of evaluations has not failed and has not converged. Reporting
    only its best point would present a wide bracket as a located answer."""
    report = asyncio.run(
        core.run_optimization(
            optimization_of(core, me, design_of(core, me), max_evaluations=3), me
        )
    )

    assert report.stopped == "budget" and report.evaluations_spent == 3
    lower, upper = report.bracket
    assert upper - lower > 0.02 * 20.0, "three evaluations cannot have reached the tolerance"
    assert report.best is not None, "it still found the best of what it tried"


def test_an_evaluation_with_no_answer_stops_the_search(core, me):
    """The sharpest difference from a study, and it follows from what a search *is*.

    `alpha` is bounded at ±30° by the adapter, so a bracket reaching past it has a point the
    server refuses. A study would tabulate the rest and mark that rung refused; here the next
    point is a function of the missing value, so there is nowhere to continue from — and
    reporting a "best" out of the points before it would present a truncated search as a
    finished one.
    """
    report = asyncio.run(
        core.run_optimization(
            optimization_of(core, me, design_of(core, me), bounds=[20.0, 40.0]), me
        )
    )

    assert report.stopped == "stalled"
    assert report.evaluations[-1].status == "refused"
    assert report.evaluations[-1].error


def test_an_evaluation_is_an_ordinary_submission(core, me):
    """So the cell budget and the quota apply per job rather than per search — the property
    that keeps a study from being a way around them, applied to the thing that spends more."""
    quota = Principal(id="tester", quotas=Quotas(jobs_per_hour=2))
    design = design_of(core, quota)
    report = asyncio.run(
        core.run_optimization(optimization_of(core, quota, design), quota)
    )

    assert report.stopped == "stalled", "the quota should stop the search like any refusal"
    assert report.evaluations_spent == 3, "two solves, then the refusal that stopped it"
    assert "limit of 2" in report.evaluations[-1].error, report.evaluations[-1].error


# ------------------------------------------------------------------------ specification


def test_a_parameter_the_capability_does_not_have_is_refused(core, me):
    """The study's guard, and the failure it prevents is worse here: a search over a parameter
    the solver ignores evaluates the same point every time, watches the objective never move,
    and reports the middle of the bracket as the answer."""
    optimization = optimization_of(core, me, design_of(core, me), parameter="alfa")
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_optimization(optimization, me))
    assert "alpha" in json.dumps(caught.value.errors)


def test_an_objective_the_capability_does_not_declare_is_refused(core, me):
    """Same rule as a study's metric list, and the same reason: a caller who asks for `c_1`
    must not be handed something that looks like an answer."""
    optimization = optimization_of(
        core, me, design_of(core, me), objective={"metric": "lift", "sense": "maximize"}
    )
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_optimization(optimization, me))
    assert ["objective", "metric"] == caught.value.errors[0]["loc"]
    assert "c_l" in json.dumps(caught.value.errors)

    # Refused on the read path too, so a search that cannot run cannot be reported on either.
    with pytest.raises(errors.InvalidObject):
        core.optimization_report(optimization, me)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"bounds": [10.0, -10.0]}, id="a bracket that does not increase"),
        pytest.param({"bounds": [1.0, 1.0]}, id="a bracket of no width"),
        pytest.param({"bounds": [1.0]}, id="half a bracket"),
        pytest.param({"max_evaluations": 1}, id="a budget the method cannot open with"),
        pytest.param({"max_evaluations": 999}, id="a budget past the cap"),
        pytest.param({"tolerance": 0.0}, id="a tolerance of zero"),
        pytest.param({"tolerance": 1.5}, id="a tolerance wider than the bracket"),
    ],
)
def test_a_malformed_optimization_is_refused(core, me, overrides):
    with pytest.raises(errors.InvalidObject):
        optimization_of(core, me, design_of(core, me), **overrides)


def test_a_reference_to_something_that_is_not_an_optimization_is_refused(core, me):
    design = design_of(core, me)
    with pytest.raises(errors.WrongObjectType):
        asyncio.run(core.run_optimization(design, me))


# ---------------------------------------------------------------------------- transport


def test_the_optimization_operations_are_reachable_over_json_rpc(core, me):
    """Two new methods rather than a third study kind, and the table should say which is
    which: a study enumerates, an optimizer chooses."""
    from fenixspoon.rpc.encoding import jsonable
    from fenixspoon.rpc.methods import method_names

    assert {"optimize.run", "optimize.get"} <= set(method_names())

    optimization = optimization_of(core, me, design_of(core, me))
    report = asyncio.run(core.run_optimization(optimization, me))
    wire = jsonable(report)
    assert wire["stopped"] == "converged"
    assert wire["best"]["metric"] == report.best.metric
    assert wire["history"]["traces"][0]["x"]["name"] == "evaluation"
