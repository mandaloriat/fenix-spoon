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


def _searched(core, me, optimization, timeout: float = 120.0):
    """Start a search and wait for it — what the CLI and the Python API do.

    Two calls rather than one because `optimize.run` returns a receipt now (ADR 0002
    decision 4) and the search keeps going as a task on this loop. Both have to happen inside
    one `asyncio.run`: the loop that submitted a solve is the loop that completes it, and a
    loop closing takes the search with it.

    Returns the *replayed* report, not the search's own. That is the one a caller can
    actually get at now, so it is the one the tests should be asserting against — and if the
    two ever disagreed, the replay is the one telling the truth.
    """

    async def go():
        started = await core.run_optimization(optimization, me)
        assert started.started is True
        deadline = asyncio.get_running_loop().time() + timeout
        while core.optimization_report(optimization, me).running:
            assert asyncio.get_running_loop().time() < deadline, "the search did not finish"
            await asyncio.sleep(0.02)

    asyncio.run(go())
    return core.optimization_report(optimization, me)


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
    report = _searched(core, me, optimization_of(core, me, design))

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
    report = _searched(core, me, optimization_of(core, me, design_of(core, me)))

    history = report.history
    assert history is not None and history.x is None
    # The objective and the metric it came from — *not* the parameter, which is the `value`
    # column of every row above. The docstrings said "where it looked" while the code emitted
    # the metric, and a self-documenting schema that documents the wrong thing is worse than
    # one that says nothing. Raised in review of #22.
    assert [trace.name for trace in history.traces] == ["objective", "c_l"]
    for trace in history.traces:
        assert trace.x.name == "evaluation"
        assert len(trace.values) == len(trace.x.values) == report.evaluations_spent

    # And both carry the unit the capability declares, transformed where the sense transforms
    # it: `c_l` is dimensionless, and a squared distance from a dimensionless target is too.
    assert [trace.unit for trace in history.traces] == ["1", "1"]


@pytest.mark.parametrize(
    ("sense", "target", "unit", "expected"),
    [
        ("minimize", None, "Pa", "Pa"),
        ("maximize", None, "m/s", "m/s"),
        ("target", 1.0, "degC", "(degC)^2"),
        ("target", 1.0, "1", "1"),
    ],
)
def test_the_objective_carries_the_unit_its_sense_implies(sense, target, unit, expected):
    """Negating leaves a unit alone; squaring a distance does not. Both traces went out
    unitless in the first version, on a capability that declares one."""
    objective = optimize.Objective(metric="x", sense=sense, target=target)
    assert objective.unit_of(unit) == expected


# ----------------------------------------------------------------------- orchestration


def test_running_it_again_costs_nothing_and_walks_the_same_path(core, me):
    """Why there is no run record. An optimizer is not *predictable* — nothing can enumerate
    its points in advance — but it is **reproducible**, and reproducibility is the property
    storage would have been for. The second run replays the identical sequence and every
    evaluation is a content-addressed cache hit."""
    optimization = optimization_of(core, me, design_of(core, me))
    first = _searched(core, me, optimization)
    again = _searched(core, me, optimization)

    assert [e.value for e in again.evaluations] == [e.value for e in first.evaluations]
    assert all(e.cached for e in again.evaluations), "a replay should solve nothing"
    assert again.best.job_id == first.best.job_id


def test_the_report_replays_the_search_without_running_it(core, me):
    """`optimize.get` resolves each point by content address, exactly as a study report
    resolves a rung — so the trajectory is recovered rather than recorded."""
    optimization = optimization_of(core, me, design_of(core, me))
    ran = _searched(core, me, optimization)

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


def test_a_replay_says_the_trajectory_stops_rather_than_guessing_why(core, me, tmp_path):
    """The one place the absence of a run record costs something, reported rather than
    guessed at — and the process-local memory that keeps it reportable at all.

    A submission the server refused leaves **no job**, so a replay cannot tell "stalled
    there" from "nobody ran it that far", and the first version called both `budget`: a
    search that stalled on a quota came back from `optimize.get` as one that had spent its
    evaluations. Raised in review of #22, fixed with `incomplete`.

    **Decision 4 nearly deleted the answer.** While `optimize.run` returned the finished
    report it could say `stalled` itself; once it returned a receipt, nobody read that report
    and the reason had nowhere to go. So the core remembers it — beside `running`, on the
    same terms — and this test pins both halves: the process that ran the search says
    `stalled`, and **a process that did not still says `incomplete`**, which is the claim
    that has to keep being true or the memory is pretending to be a run record.
    """
    quota = Principal(id="tester", quotas=Quotas(jobs_per_hour=2))
    optimization = optimization_of(core, quota, design_of(core, quota))
    replayed = _searched(core, quota, optimization)

    assert replayed.stopped == "stalled", "this process was there and remembers why"
    assert replayed.evaluations_spent == 3, "two solves, then the refusal that stopped it"
    assert replayed.evaluations[-1].status == "refused"
    assert "hour" in (replayed.evaluations[-1].error or ""), (
        "the refusal's own message is the actionable half and must survive the run"
    )
    assert replayed.best is not None and replayed.bracket is not None

    elsewhere = FenixSpoonCore(JobManager(data_dir=core.jobs.data_dir))
    blind = elsewhere.optimization_report(optimization, quota)
    assert blind.stopped == "incomplete", (
        "a replay with no memory of the run cannot know why, and must not say"
    )
    assert blind.evaluations_spent == 2, "only the two solves that left a job behind"


def test_starting_a_search_forgets_what_the_last_one_knew(core, me):
    """The remembered tail describes a search that *finished*, and a new run drops it.

    Without that rule the memory outlives its subject. A poll during a running search reaches
    the same "no job here" branch at the same index as the previous run's stall, matches the
    remembered tail, and reports a search that is progressing as stalled — with an error
    message from a refusal that is no longer happening. Raised in review of #22.

    Asserted on the structure rather than through a poll, and the reason is worth stating:
    reproducing the false report behaviourally needs a second search that gets *further* than
    the first, which means relieving the quota that stopped it, which means controlling the
    clock. That machinery would test the fixture more than the rule. The rule is that
    starting clears, and this is that rule.
    """
    quota = Principal(id="tester", quotas=Quotas(jobs_per_hour=2))
    optimization = optimization_of(core, quota, design_of(core, quota))
    assert _searched(core, quota, optimization).stopped == "stalled"
    assert core._stops, "a stalled search leaves the reason behind for optimize.get"

    async def start_only():
        await core.run_optimization(optimization, quota)
        # Read before the search can finish and write a fresh tail: what a poll would see
        # while one is in flight is `incomplete`, never a reason from the run before it.
        remembered = dict(core._stops)
        await core._searches[(f"{optimization}@1", quota.id)]
        return remembered

    assert asyncio.run(start_only()) == {}


def test_an_evaluation_records_the_optimization_it_came_from(core, me):
    """#44's rule again: what was solved is described by object revisions, and `iteration` is
    this sequence's `variation_index` so a reader of provenance learns one vocabulary."""
    design = design_of(core, me)
    optimization = optimization_of(core, me, design)
    report = _searched(core, me, optimization)

    provenance = core.provenance(report.evaluations[1].job_id, me)
    assert provenance.inputs["optimization"].startswith(f"{optimization}@")
    assert provenance.inputs["variation_index"] == 1
    assert provenance.inputs["variation"] == {"alpha": report.evaluations[1].value}
    assert provenance.inputs["design"].startswith(f"{design}@")


def test_the_budget_stops_the_search_and_the_bracket_says_how_far_it_got(core, me):
    """A search that runs out of evaluations has not failed and has not converged. Reporting
    only its best point would present a wide bracket as a located answer."""
    report = _searched(core, me, optimization_of(core, me, design_of(core, me), max_evaluations=3))

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
    report = _searched(
        core, me, optimization_of(core, me, design_of(core, me), bounds=[20.0, 40.0])
    )

    assert report.stopped == "stalled"
    assert report.evaluations[-1].status == "refused"
    assert report.evaluations[-1].error


def test_an_evaluation_is_an_ordinary_submission(core, me):
    """So the cell budget and the quota apply per job rather than per search — the property
    that keeps a study from being a way around them, applied to the thing that spends more."""
    quota = Principal(id="tester", quotas=Quotas(jobs_per_hour=2))
    design = design_of(core, quota)
    report = _searched(core, quota, optimization_of(core, quota, design))

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
    report = _searched(core, me, optimization)
    wire = jsonable(report)
    assert wire["stopped"] == "converged"
    assert wire["best"]["metric"] == report.best.metric
    assert wire["history"]["traces"][0]["x"]["name"] == "evaluation"


def test_run_answers_with_a_receipt_and_a_second_call_joins_the_first(core, me):
    """ADR 0002 decision 4 on the wire: `optimize.run` returns *that the search started*.

    The shape it replaced was the finished trajectory, which is minutes of solving held in
    one response — fine on a pipe a child process owns, impossible through the proxies and
    browser timeouts between a page and a server. And a second call while one is in flight
    joins it rather than starting a rival: two searches over one revision would agree, since
    they replay the same sequence and every point is a cache hit, but they would be two loops
    doing one loop's work.
    """
    optimization = optimization_of(core, me, design_of(core, me))

    async def go():
        first = await core.run_optimization(optimization, me)
        second = await core.run_optimization(optimization, me)
        assert core.optimization_report(optimization, me).running is True
        while core.optimization_report(optimization, me).running:
            await asyncio.sleep(0.02)
        return first, second

    first, second = asyncio.run(go())

    assert first.started is True and second.started is False
    assert first.optimization == second.optimization
    assert first.optimization.startswith(f"{optimization}@"), "the receipt pins the revision"
    # And once it is over, nothing claims to be running any more.
    assert core.optimization_report(optimization, me).running is False
