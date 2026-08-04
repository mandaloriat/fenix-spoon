"""Studies: one specification for the shape every iterative workflow shares (#48, #21).

Two acceptance criteria, one per kind.
:func:`test_a_mesh_convergence_study_tabulates_and_reuses` — "a mesh-convergence study over one
design returns a table of (variation → metric) with per-job references, reusing cached runs,
and its output fits in a screen of text" (#48). And
:func:`test_a_sweep_of_angle_of_attack_produces_a_lift_polar` — one submission, N jobs, and a
response curve that is linear in alpha because that is what potential-flow lift is (#21).

Each kind's tests fall into the same groups, in the same order, and the parallel is the point:
**specification** tests pin what a study object may say, and they matter more than they look —
a study is a question, and a malformed question produces a plausible answer rather than an
error. **Orchestration** tests cover what running one does to jobs, quotas and the cache.
**Read-out** tests cover the arithmetic that turns a table into a sentence — where a ladder
settled, what a sweep's curve looks like — and therefore the part that can quietly lie.

The sweep section also carries the tests for what its arrival *found*: a column list the rows
had never honoured, and a CLI table that dropped every map-valued column.
"""

import asyncio
import json

import pytest
from pydantic import TypeAdapter

from fenixspoon.core import FenixSpoonCore, errors, studies
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import Geometry
from fenixspoon.jobs import JobManager

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
BASE_PARAMS = {"iterations": 400, "write_vtk": False}


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def design_of(core, me, params=None) -> str:
    geometry = core.create_object("geometry", AIRFOIL, me).ref
    return core.create_object(
        "design",
        {
            "solver": "mock.laplace2d",
            "geometry": geometry,
            "params": {**BASE_PARAMS, **(params or {})},
        },
        me,
    ).ref


def study_of(core, me, design, **overrides) -> str:
    body = {
        "kind": "mesh_convergence",
        "design": design,
        "parameter": "resolution",
        "values": [24.0, 32.0, 48.0],
    }
    body.update(overrides)
    return core.create_object("study", body, me).ref


def run_to_completion(core, me, study, timeout: float = 90.0):
    """Run a study and poll its report until every rung is terminal.

    One `asyncio.run` for the whole thing, for the reason `test_rpc` documents: a solve is
    completed by callbacks on the loop that submitted it, so polling from a second loop would
    watch a status that never moves.
    """

    async def go():
        run = await core.run_study(study, me)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            report = core.study_report(study, me)
            if report.complete:
                return run, report
            assert asyncio.get_running_loop().time() < deadline, "study did not finish"
            await asyncio.sleep(0.02)

    return asyncio.run(go())


# ------------------------------------------------------------------------ acceptance


def test_a_mesh_convergence_study_tabulates_and_reuses(core, me):
    """The acceptance criterion, in one test.

    A table of (variation → metric) with per-job references; the second run of the same study
    reuses every rung rather than re-solving; and the whole report fits in a screen of text.
    """
    study = study_of(core, me, design_of(core, me))
    run, report = run_to_completion(core, me, study)

    assert run.submitted == 3 and run.reused == 0 and run.refused == 0
    assert [rung.value for rung in report.rungs] == [24.0, 32.0, 48.0]
    assert all(rung.status == "done" for rung in report.rungs)
    assert all(rung.job_id for rung in report.rungs), "no per-job reference"

    # Every rung reports every declared metric — the table is (variation → metric), not
    # (variation → one number somebody picked).
    declared = {spec.name for spec in core.capability("mock.laplace2d").metrics}
    for rung in report.rungs:
        assert set(rung.metrics) == declared

    # Distinct jobs: three rungs are three different solves, and a cache that collapsed them
    # would make the whole study meaningless.
    assert len({rung.job_id for rung in report.rungs}) == 3

    # …and running it again costs nothing, which is the point of reusing the cache.
    again = asyncio.run(core.run_study(study, me))
    assert again.reused == 3 and again.submitted == 0

    # "Fits in a screen of text": ~80 columns by ~50 rows is about 4 kB.
    rendered = json.dumps(report.model_dump())
    assert len(rendered) < 4096, f"report is {len(rendered)} bytes"
    assert "fields" not in rendered, "a field payload got into a study report"


# --------------------------------------------------------------------- specification


def test_a_study_needs_at_least_two_rungs(core, me):
    """One rung cannot converge to anything, and reporting `settled_at` for a claim never
    tested would be the study equivalent of a fabricated result."""
    with pytest.raises(errors.InvalidObject):
        study_of(core, me, design_of(core, me), values=[32.0])


def test_a_shuffled_or_repeated_ladder_is_refused(core, me):
    """The read-out compares each rung with the previous one, so "settled at" means nothing
    over a shuffled ladder — and a repeated value is the same solve twice."""
    design = design_of(core, me)
    with pytest.raises(errors.InvalidObject):
        study_of(core, me, design, values=[48.0, 24.0, 32.0])
    with pytest.raises(errors.InvalidObject):
        study_of(core, me, design, values=[32.0, 32.0])


def test_a_parameter_the_capability_does_not_have_is_refused(core, me):
    """The failure this guard exists for is invisible without it, which is why it is a guard
    and not a nicety.

    A solver's `Params` model *ignores* unknown fields. So a study varying `resolutoin` would
    submit every rung with identical parameters, the result cache would collapse them onto one
    job, and the report would show the same metric at every rung — a perfectly converged
    answer that is entirely fabricated. A convergence study is precisely the operation where
    nobody would notice.
    """
    study = study_of(core, me, design_of(core, me), parameter="resolutoin")
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_study(study, me))
    assert "resolution" in json.dumps(caught.value.errors), "the message should list the real ones"

    # And the same refusal on the read path, so a study that cannot run cannot be reported on
    # either — a report built from a broken specification is the fabricated answer arriving by
    # a different door.
    with pytest.raises(errors.InvalidObject):
        core.study_report(study, me)


def test_a_study_naming_something_that_is_not_a_design_is_refused(core, me):
    geometry = core.create_object("geometry", AIRFOIL, me).ref
    study = study_of(core, me, geometry)
    with pytest.raises(errors.WrongObjectType):
        asyncio.run(core.run_study(study, me))


def test_a_reference_to_something_that_is_not_a_study_is_refused(core, me):
    """`study.run` on a design reference: the type check is what stops a body that happens to
    parse from being run as something it is not."""
    design = design_of(core, me)
    with pytest.raises(errors.WrongObjectType):
        asyncio.run(core.run_study(design, me))


# -------------------------------------------------------------------- orchestration


def test_the_rungs_are_solved_at_the_values_the_study_names(core, me):
    """The override reaches the solver. Asserted through the *diagnostics* rather than by
    trusting the plumbing: a finer grid is more cells, and that is a number the solve
    reports rather than one the study asserts about itself."""
    study = study_of(core, me, design_of(core, me))
    _, report = run_to_completion(core, me, study)

    cells = [core.result_levels(r.job_id, me).diagnostics.stats["cells"] for r in report.rungs]
    assert cells == sorted(cells), "a finer rung did not produce a bigger mesh"
    assert len(set(cells)) == 3, "two rungs solved the same problem"


def test_a_rung_records_the_study_and_the_design_it_came_from(core, me):
    """#44's rule survives the override: what was solved is still described entirely by
    object revisions, because the parameters are a pure function of (study revision, rung)."""
    design = design_of(core, me)
    study = study_of(core, me, design)
    _, report = run_to_completion(core, me, study)

    provenance = core.provenance(report.rungs[1].job_id, me)
    assert provenance.inputs["study"].startswith(f"{study}@")
    assert provenance.inputs["variation_index"] == 1
    # The whole override map, not the bare number: a grid point sets several parameters, and
    # a value recorded without its parameter was already half a sentence when only the ladder
    # existed.
    assert provenance.inputs["variation"] == {"resolution": 32.0}
    assert provenance.inputs["design"].startswith(f"{design}@")


def test_the_relation_reads_backwards_from_the_study(core, me):
    """The study → job relation is queryable the same way the design → job one is, because
    it is the same mechanism rather than a second one."""
    study = study_of(core, me, design_of(core, me))
    _, report = run_to_completion(core, me, study)

    jobs = core.jobs_for_object(study, me)
    assert {job.id for job in jobs} == {rung.job_id for rung in report.rungs}


def test_a_rung_answered_by_an_earlier_standalone_solve_is_still_found(core, me):
    """The collision that shaped how a rung resolves back to its job.

    Solve one of the ladder's values *before* the study exists. The cache answers that rung
    with the earlier job — which is what reusing the cache means — but that job's `inputs`
    never mentioned the study, so an inputs-only lookup would report the rung as missing. The
    cache key is what finds it, and this is the test that would fail if the report were built
    from `inputs` alone.
    """

    async def solve_standalone():
        # An integer where the study will pass a float, on purpose: the cache hashes
        # *validated* params, so 32 and 32.0 are the same solve. If they were not, this test
        # would pass for the wrong reason and #47's whole normalisation claim would be untested
        # from this direction.
        job = await core.submit(
            "mock.laplace2d",
            TypeAdapter(Geometry).validate_python(AIRFOIL),
            {**BASE_PARAMS, "resolution": 32},
            me,
        )
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        return job.id

    standalone = asyncio.run(solve_standalone())

    study = study_of(core, me, design_of(core, me))
    run, report = run_to_completion(core, me, study)

    assert run.reused == 1, "the pre-existing solve was not reused"
    middle = report.rungs[1]
    assert middle.job_id == standalone, "the rung did not resolve to the job that answered it"
    assert middle.cached is True
    assert middle.metrics, "a reused rung reported no metrics"


def test_a_rung_over_the_cell_budget_does_not_fail_the_study(core, me, tmp_path):
    """Rung 3 exceeding the budget says nothing about rungs 1 and 2, and reporting the whole
    study as failed would throw away work that succeeded."""
    small = FenixSpoonCore(JobManager(data_dir=tmp_path / "budget", max_cells=2000))
    study = study_of(small, me, design_of(small, me), values=[24.0, 32.0, 400.0])

    run = asyncio.run(small.run_study(study, me))
    assert run.refused == 1 and run.submitted == 2

    report = small.study_report(study, me)
    assert report.rungs[2].status == "refused"
    assert report.rungs[2].job_id is None
    assert report.rungs[0].status in ("queued", "running", "done")


def test_a_study_is_not_a_way_around_a_quota(core, me, tmp_path):
    """"Respects quotas per job like any other submission" — a five-rung study is five
    submissions, not one, and a principal allowed two jobs an hour gets two rungs."""
    limited = FenixSpoonCore(JobManager(data_dir=tmp_path / "quota"))
    principal = Principal(id="small", quotas=Quotas(jobs_per_hour=2))
    study = study_of(limited, principal, design_of(limited, principal))

    run = asyncio.run(limited.run_study(study, principal))
    assert run.submitted == 2 and run.refused == 1


# ------------------------------------------------------------------------- read-out


def test_the_report_says_where_a_metric_settled(core, me):
    """The sentence the table is for. Computed here rather than left to the caller, because
    deriving it client-side is what makes a compact answer stop being compact."""
    study = study_of(core, me, design_of(core, me))
    _, report = run_to_completion(core, me, study)

    speed = next(c for c in report.convergence if c.metric == "speed_max")
    assert len(speed.values) == 3
    assert len(speed.relative_change) == 2
    assert all(change is not None for change in speed.relative_change)


@pytest.mark.parametrize(
    ("values", "tolerance", "expected"),
    [
        # Settles at the third rung and stays there.
        ([1.0, 1.5, 1.51, 1.512], 0.05, 32.0),
        # Dips under the tolerance once, then moves again: not settled. This is the case a
        # "first change below tolerance" rule gets wrong, and it is the common one — a metric
        # that happens to cross its own asymptote between two rungs.
        ([1.0, 1.005, 1.4, 1.9], 0.01, None),
        # Never settles at all.
        ([1.0, 2.0, 4.0, 8.0], 0.01, None),
        # An unfinished rung higher up: the claim cannot be made *yet*, which is different
        # from "does not converge" and must not be reported as settled.
        ([1.0, 1.001, None, None], 0.01, None),
    ],
)
def test_settling_needs_every_later_step_to_stay_small(values, tolerance, expected):
    ladder = [16.0, 24.0, 32.0, 48.0]
    assert studies.settled_value(ladder, values, tolerance) == expected


def test_a_relative_change_against_zero_is_undefined_not_infinite():
    """A metric that starts at zero would otherwise look like the least converged thing in
    the study, which is an artefact of the arithmetic rather than a fact about the solve."""
    assert studies.relative_changes([0.0, 1.0, 1.0]) == [None, 0.0]


def test_a_metric_the_capability_does_not_declare_is_refused(core, me):
    """The same rule `capability.describe` follows for sections and `result.get` for levels.

    This test asserted the opposite until the review of #48, and the way it was wrong is worth
    keeping: the docstring on `tabulated_metrics` already *said* unknown names were refused,
    the implementation returned them unchecked, and this test asserted the unchecked behaviour
    while claiming it followed the rule. Three statements, two of them wrong, and they agreed
    with each other well enough that nothing failed.

    A column of nulls is worse than a missing column, which is why "harmless gap" is not the
    right reading. A missing column prompts "did I spell that right"; a column of nulls reads
    as "this capability reports `c_1` and the solve did not produce it" — a specific,
    confident and wrong conclusion.
    """
    study = study_of(core, me, design_of(core, me), metrics=["c_1"])
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_study(study, me))
    assert "c_l" in json.dumps(caught.value.errors), "the message should list the real names"

    # Refused on the read path too, so a study that cannot be tabulated does not spend the
    # compute first and say so afterwards.
    with pytest.raises(errors.InvalidObject):
        core.study_report(study, me)


def test_omitting_the_metric_list_tabulates_every_declared_one(core, me):
    """The counter-case, so the refusal above cannot quietly become "you must list them"."""
    study = study_of(core, me, design_of(core, me))
    _, report = run_to_completion(core, me, study)
    declared = {spec.name for spec in core.capability("mock.laplace2d").metrics}
    assert {column.metric for column in report.convergence} == declared


# ------------------------------------------------------------------------ transport


def test_the_study_operations_are_reachable_over_json_rpc(core, me):
    """The vocabulary the design draft's table describes is now complete — #45 left these two
    out precisely because there was nothing behind them."""
    from fenixspoon.rpc.methods import method_names

    assert {"study.run", "study.get"} <= set(method_names())

    study = study_of(core, me, design_of(core, me))
    run, report = run_to_completion(core, me, study)
    del run

    from fenixspoon.rpc.encoding import jsonable

    wire = jsonable(report)
    assert wire["kind"] == "mesh_convergence"
    assert len(wire["rungs"]) == 3
    assert wire["convergence"][0]["metric"]


# ============================================================== sweeps (roadmap M5, #21)
#
# The second kind, and the one the abstraction was written for. What these tests are really
# checking, beyond the arithmetic, is that adding it cost nothing structural: no new
# operation, no second job path, no change to the ladder above. Every test in this section
# goes through the same `study.run` / `study.get` the convergence tests use.


def sweep_of(core, me, design, **overrides) -> str:
    body = {
        "kind": "sweep",
        "design": design,
        "axes": [{"parameter": "alpha", "values": [-4.0, 0.0, 4.0, 8.0]}],
        "metrics": ["c_l"],
    }
    body.update(overrides)
    return core.create_object("study", body, me).ref


def swept_design(core, me) -> str:
    """A design cheap enough to solve a dozen times in a test, on the airfoil above."""
    return design_of(core, me, {"resolution": 40, "iterations": 250})


# ----------------------------------------------------------------------- acceptance


def test_a_sweep_of_angle_of_attack_produces_a_lift_polar(core, me):
    """The acceptance criterion, restated against what the toolkit can actually answer.

    #21 asked for "a demo sweeping airfoil camber and plotting a lift-**proxy** curve". Both
    halves of that have moved since it was written. The proxy is gone — #68 made `c_l` a real
    number, validated against closed forms — and camber is a property of the *geometry*, which
    the workspace stores as an explicit polygon rather than a parametric shape, so a camber
    sweep is a sweep over geometry references and not over a capability's parameters.

    Angle of attack is the better subject anyway, and not as a substitute: `alpha` rotates the
    free stream rather than the geometry, which the adapter's own parameter description points
    out is what lets a sweep reuse one domain — and the lift polar is the curve this entire
    physics exists to produce.

    The assertion is a **property of the physics**, not a golden number: potential-flow lift is
    linear in alpha, so equal steps in angle must give equal steps in `c_l`. That also catches
    the failure mode a sweep is uniquely exposed to — every point collapsing onto one cached
    job, which would make the "curve" flat and every step equal to zero. Hence both halves:
    the steps agree with each other, *and* they are not zero.
    """
    study = sweep_of(core, me, swept_design(core, me))
    run, report = run_to_completion(core, me, study)

    assert run.submitted == 4 and run.reused == 0 and run.refused == 0
    assert [point.values["alpha"] for point in report.points] == [-4.0, 0.0, 4.0, 8.0]
    assert all(point.status == "done" for point in report.points)
    assert len({point.job_id for point in report.points}) == 4, (
        "the points collapsed onto one solve"
    )

    (curve,) = report.curves
    assert curve.name == "c_l"
    (trace,) = curve.traces
    assert trace.x.name == "alpha" and trace.x.values == [-4.0, 0.0, 4.0, 8.0]

    steps = [b - a for a, b in zip(trace.values, trace.values[1:], strict=False)]
    assert min(steps) > 0.1, f"the polar has no slope: {trace.values}"
    mean = sum(steps) / len(steps)
    assert max(abs(step - mean) for step in steps) < 0.03 * mean, (
        f"lift is linear in alpha and these steps are not: {steps}"
    )

    # The same two properties the ladder's acceptance test pins: it reads in one response, and
    # no field crossed into it.
    rendered = json.dumps(report.model_dump())
    assert len(rendered) < 4096, f"report is {len(rendered)} bytes"
    assert "fields" not in rendered


def test_a_sweep_costs_nothing_the_second_time(core, me):
    """The property that makes a sweep worth running from a workspace rather than a script:
    add two angles to an eleven-angle polar and pay for two solves."""
    design = swept_design(core, me)
    run_to_completion(core, me, sweep_of(core, me, design))

    wider = sweep_of(
        core,
        me,
        design,
        axes=[{"parameter": "alpha", "values": [-4.0, 0.0, 4.0, 6.0, 8.0]}],
    )
    run, report = run_to_completion(core, me, wider)
    assert run.reused == 4 and run.submitted == 1
    assert [point.cached for point in report.points] == [True, True, True, False, True]


# -------------------------------------------------------------------- specification


def test_a_sweep_is_a_grid_or_a_list_of_points_but_not_both(core, me):
    """Given both, neither is the specification — and silently preferring one would make the
    other a decoration that looks load-bearing."""
    design = swept_design(core, me)
    with pytest.raises(errors.InvalidObject):
        sweep_of(core, me, design, points=[{"alpha": 0.0}, {"alpha": 4.0}])
    with pytest.raises(errors.InvalidObject):
        sweep_of(core, me, design, axes=[], points=[])


@pytest.mark.parametrize(
    "axes",
    [
        pytest.param(
            [{"parameter": "alpha", "values": [0.0, 4.0]}, {"parameter": "alpha", "values": [1.0]}],
            id="one parameter on two axes",
        ),
        pytest.param([{"parameter": "alpha", "values": []}], id="an axis with no values"),
        pytest.param([{"parameter": "alpha", "values": [4.0, 0.0]}], id="descending"),
        pytest.param([{"parameter": "alpha", "values": [4.0, 4.0]}], id="a repeated value"),
    ],
)
def test_a_malformed_grid_is_refused(core, me, axes):
    with pytest.raises(errors.InvalidObject):
        sweep_of(core, me, swept_design(core, me), axes=axes)


def test_the_first_axis_needs_two_values_because_it_is_the_abscissa(core, me):
    """A curve needs two points — and this rule is what bounds the trace count.

    With at least two values on the abscissa, the combinations of the remaining axes cannot
    exceed `MAX_SWEEP_POINTS / 2`, which is exactly `MAX_SERIES_TRACES`. Drop it and a sweep
    with one value on the first axis and sixty-four on the second would build a series the
    protocol refuses — a legal study whose report cannot be encoded.
    """
    from fenixspoon.series import MAX_SERIES_TRACES

    assert studies.MAX_SWEEP_POINTS // 2 == MAX_SERIES_TRACES, (
        "the point cap and the trace ceiling are related by this rule; moving one alone "
        "breaks the guarantee that a legal sweep always has an encodable report"
    )
    with pytest.raises(errors.InvalidObject):
        sweep_of(
            core,
            me,
            swept_design(core, me),
            axes=[
                {"parameter": "alpha", "values": [0.0]},
                {"parameter": "resolution", "values": [24.0, 32.0]},
            ],
        )


def test_a_grid_over_the_cap_is_refused_with_its_shape_spelled_out(core, me):
    """The whole failure mode is that a grid's size is not visible in the body asking for it:
    four axes of four values is one short line and two hundred and fifty-six solves."""
    with pytest.raises(errors.InvalidObject) as caught:
        sweep_of(
            core,
            me,
            swept_design(core, me),
            axes=[
                {"parameter": "alpha", "values": [0.0, 2.0, 4.0, 6.0]},
                {"parameter": "u_inf", "values": [1.0, 2.0, 3.0, 4.0]},
                {"parameter": "resolution", "values": [24.0, 32.0, 40.0, 48.0]},
                {"parameter": "iterations", "values": [100.0, 200.0, 300.0, 400.0]},
            ],
        )
    assert "4 x 4 x 4 x 4" in json.dumps(caught.value.errors)


@pytest.mark.parametrize(
    "points",
    [
        pytest.param([{"alpha": 0.0}], id="a sweep of one point is a solve"),
        pytest.param([{"alpha": 0.0}, {"alpha": 0.0}], id="a repeated point"),
        pytest.param([{"alpha": 0.0}, {"u_inf": 2.0}], id="a ragged parameter set"),
        pytest.param([{}, {}], id="points that set nothing"),
    ],
)
def test_a_malformed_point_list_is_refused(core, me, points):
    with pytest.raises(errors.InvalidObject):
        sweep_of(core, me, swept_design(core, me), axes=[], points=points)


def test_the_point_cap_is_counted_and_never_enumerated(core, me, monkeypatch):
    """The cap must not build the thing it refuses.

    Ten axes of a hundred values is a thousand numbers on the wire and 10^20 points, so a
    validator that asked `len(self.variations())` would construct the cartesian product it was
    about to reject — turning the guard against an accidental 256-solve grid into the more
    effective denial of service it was meant to prevent. Counted with `prod` over the axis
    lengths instead, which is one multiplication per axis. Raised in review of #21.

    `product` is monkeypatched to explode rather than left to hang: a regression here would
    otherwise fail by exhausting the machine, which is a true signal and a terrible one.
    """
    monkeypatch.setattr(
        studies,
        "product",
        lambda *args: pytest.fail("the point cap enumerated the grid it was refusing"),
    )
    with pytest.raises(errors.InvalidObject) as caught:
        sweep_of(
            core,
            me,
            swept_design(core, me),
            axes=[
                {"parameter": f"p{i}", "values": [float(v) for v in range(40)]}
                for i in range(8)
            ],
        )
    # 40^8, spelled out both ways, because the point of the message is that neither number is
    # visible in the body that asked for it.
    assert "6553600000000 points (40 x 40 x 40 x 40 x 40 x 40 x 40 x 40)" in json.dumps(
        caught.value.errors
    )


def test_an_axis_over_a_parameter_the_capability_does_not_have_is_refused(core, me):
    """The ladder's guard, which matters more over a grid rather than less: one misspelled
    axis collapses every point that differs only along it, and the table then reads as a
    parameter with no effect — a wrong conclusion, confidently drawn."""
    study = sweep_of(
        core, me, swept_design(core, me), axes=[{"parameter": "alfa", "values": [0.0, 4.0]}]
    )
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_study(study, me))
    assert "alpha" in json.dumps(caught.value.errors), "the message should list the real names"


def test_the_refusal_points_at_the_field_the_bad_name_was_written_in(core, me):
    """A `loc` is an instruction to go and look, so it has to name something the caller sent.

    The first version reported `["parameter"]` whatever the kind, having been written for the
    ladder — which on a sweep sends a reader to a field the body does not have. One error per
    bad name now, each located: the axis that carries it, by index, or `points` where every
    point declares the same keys and singling one out would suggest the rest are fine.
    Raised in review of #21.
    """
    grid = sweep_of(
        core,
        me,
        swept_design(core, me),
        axes=[
            {"parameter": "alpha", "values": [0.0, 4.0]},
            {"parameter": "u_infinity", "values": [1.0, 2.0]},
        ],
    )
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_study(grid, me))
    assert [error["loc"] for error in caught.value.errors] == [["axes", 1, "parameter"]]

    listed = sweep_of(
        core,
        me,
        swept_design(core, me),
        axes=[],
        points=[{"alfa": 0.0, "u_infinity": 1.0}, {"alfa": 4.0, "u_infinity": 1.0}],
    )
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_study(listed, me))
    # Both names are wrong and both are reported: fixing one and resubmitting to discover the
    # other is the round trip a list of errors exists to avoid.
    assert [error["loc"] for error in caught.value.errors] == [["points"], ["points"]]

    ladder = study_of(core, me, design_of(core, me), parameter="resolutoin")
    with pytest.raises(errors.InvalidObject) as caught:
        asyncio.run(core.run_study(ladder, me))
    assert [error["loc"] for error in caught.value.errors] == [["parameter"]]


# -------------------------------------------------------------------- orchestration


def test_a_grid_enumerates_in_odometer_order_with_the_last_axis_fastest(core, me):
    """Consecutive points differ in the axis the curve is drawn along, which is what makes the
    order worth specifying rather than leaving to `product`'s documentation."""
    study = sweep_of(
        core,
        me,
        swept_design(core, me),
        axes=[
            {"parameter": "resolution", "values": [24.0, 32.0]},
            {"parameter": "alpha", "values": [0.0, 4.0, 8.0]},
        ],
    )
    body = studies.STUDY_BODY.validate_python(core.object(study, me).body)
    assert body.variations() == [
        {"resolution": 24.0, "alpha": 0.0},
        {"resolution": 24.0, "alpha": 4.0},
        {"resolution": 24.0, "alpha": 8.0},
        {"resolution": 32.0, "alpha": 0.0},
        {"resolution": 32.0, "alpha": 4.0},
        {"resolution": 32.0, "alpha": 8.0},
    ]


def test_a_point_records_every_parameter_it_varied(core, me):
    """#44's rule under a grid: the job's inputs still describe what was solved, and now they
    have to name *which* parameter each value belonged to."""
    design = swept_design(core, me)
    study = sweep_of(core, me, design)
    _, report = run_to_completion(core, me, study)

    provenance = core.provenance(report.points[2].job_id, me)
    assert provenance.inputs["study"].startswith(f"{study}@")
    assert provenance.inputs["variation_index"] == 2
    assert provenance.inputs["variation"] == {"alpha": 4.0}
    assert provenance.inputs["design"].startswith(f"{design}@")


def test_a_point_the_capability_refuses_does_not_take_the_table_with_it(core, me):
    """`alpha` is bounded at ±30° by the adapter, so a sweep that walks past it has a point
    with no answer and three with one. Per-point, exactly as the ladder is per-rung."""
    study = sweep_of(
        core, me, swept_design(core, me), axes=[{"parameter": "alpha", "values": [0.0, 4.0, 40.0]}]
    )
    _, report = run_to_completion(core, me, study)

    assert [point.status for point in report.points] == ["done", "done", "refused"]
    assert report.complete, "a refused point is terminal; the sweep is not waiting for it"


# ------------------------------------------------------------------------ read-out


def test_a_two_axis_sweep_draws_one_trace_per_line_of_the_grid(core, me):
    """A family of curves, which is how a two-parameter sweep is drawn on paper: `c_l(alpha)`
    at each resolution, rather than one zig-zag through six unrelated points."""
    study = sweep_of(
        core,
        me,
        swept_design(core, me),
        axes=[
            {"parameter": "alpha", "values": [0.0, 4.0, 8.0]},
            {"parameter": "resolution", "values": [24.0, 40.0]},
        ],
    )
    _, report = run_to_completion(core, me, study)

    (curve,) = report.curves
    assert [trace.name for trace in curve.traces] == [
        "c_l @ resolution=24",
        "c_l @ resolution=40",
    ]
    for trace in curve.traces:
        assert trace.x.values == [0.0, 4.0, 8.0]
    # Two resolutions of the same angles disagree — mildly, and they must, or the sweep is
    # reporting one solve under two names.
    assert curve.traces[0].values != curve.traces[1].values


def test_a_missing_point_leaves_its_trace_short_rather_than_misaligned(core, me):
    """Why every trace carries its own abscissa instead of sharing the series-level one.

    With a shared `x`, a point with no answer has no legal encoding: a trace's values must
    line up with the axis, so the missing one would have to become a zero, a null the model
    refuses, or a silent shift of every later value onto the wrong angle. Per-trace abscissae
    make the partial sweep say exactly what it knows.
    """
    study = sweep_of(
        core, me, swept_design(core, me), axes=[{"parameter": "alpha", "values": [0.0, 4.0, 40.0]}]
    )
    _, report = run_to_completion(core, me, study)

    (curve,) = report.curves
    assert curve.x is None, "the series-level abscissa is the encoding this avoids"
    (trace,) = curve.traces
    assert trace.x.values == [0.0, 4.0], "the refused angle is absent, not zero"
    assert len(trace.values) == 2


def test_explicit_points_are_tabulated_and_not_drawn(core, me):
    """The form every design of experiments a grid cannot express arrives in — and the reason
    it comes back without curves: the caller chose points, not an ordering, so there is no
    axis to draw against and inventing one would be drawing against the list index."""
    study = sweep_of(
        core,
        me,
        swept_design(core, me),
        axes=[],
        points=[{"alpha": 0.0, "u_inf": 1.0}, {"alpha": 6.0, "u_inf": 1.5}],
    )
    _, report = run_to_completion(core, me, study)

    assert report.parameters == ["alpha", "u_inf"]
    assert [point.values for point in report.points] == [
        {"alpha": 0.0, "u_inf": 1.0},
        {"alpha": 6.0, "u_inf": 1.5},
    ]
    assert all(point.status == "done" for point in report.points)
    assert report.curves == []


def test_a_metric_nothing_has_answered_yet_is_absent_rather_than_an_empty_frame(core, me):
    """A curve set with no traces is not an empty drawing, it is an invalid one — and "no
    answers yet" is said by the metric's absence, not by a frame around nothing."""
    study = sweep_of(core, me, swept_design(core, me))
    report = core.study_report(study, me)  # never run: nothing has answered anything

    assert all(point.status == "refused" for point in report.points)
    assert report.curves == []
    # `complete` is true here, which reads oddly for a study nobody ran: a refusal is terminal
    # and a point with no job is reported as refused. That is #48's meaning of the field and
    # this pins it rather than leaving it to be discovered — the sentence to read beside it is
    # the per-point `error`, which says the variation was never accepted in the first place.
    assert report.complete and all(point.error for point in report.points)


# ------------------------------------------------------------------------ transport


def test_a_sweep_report_crosses_json_rpc_as_the_same_document(core, me):
    """No new operation and no new binding — `study.get` answers for both kinds, which is the
    whole payoff of #48 having defined a study rather than a mesh ladder."""
    from fenixspoon.rpc.encoding import jsonable
    from fenixspoon.rpc.methods import method_names

    study = sweep_of(core, me, swept_design(core, me))
    _, report = run_to_completion(core, me, study)

    assert {"study.run", "study.get"} <= set(method_names()), "no new method appeared"
    wire = jsonable(report)
    assert wire["kind"] == "sweep"
    assert len(wire["points"]) == 4
    assert wire["curves"][0]["traces"][0]["x"]["name"] == "alpha"


def test_the_table_carries_the_columns_the_study_asked_for(core, me):
    """`metrics` is documented as "which declared metrics to tabulate", and until #21 every
    row carried all of them regardless — the read-out honoured the list and the table ignored
    it. Invisible on a ladder, where the read-out is what a caller reads; obvious the moment a
    sweep of two named metrics rendered six columns of them."""
    study = sweep_of(core, me, swept_design(core, me), metrics=["c_l"])
    _, report = run_to_completion(core, me, study)

    assert all(set(point.metrics) == {"c_l"} for point in report.points)
    assert [curve.name for curve in report.curves] == ["c_l"]

    # The counter-case, so this cannot quietly become "you must ask for what you want".
    every = sweep_of(core, me, swept_design(core, me), metrics=None)
    _, report = run_to_completion(core, me, every)
    declared = {spec.name for spec in core.capability("mock.laplace2d").metrics}
    assert all(set(point.metrics) == declared for point in report.points)
