"""Studies: one specification for the shape every iterative workflow shares (#48).

The acceptance criterion is :func:`test_a_mesh_convergence_study_tabulates_and_reuses` — "a
mesh-convergence study over one design returns a table of (variation → metric) with per-job
references, reusing cached runs, and its output fits in a screen of text".

The rest fall into three groups. **Specification** tests pin what a study object may say, and
they matter more than they look: a study is a question, and a malformed question produces a
plausible answer rather than an error. **Orchestration** tests cover what running one does to
jobs, quotas and the cache. **Read-out** tests cover the convergence arithmetic, which is the
part that turns a table into a sentence and therefore the part that can quietly lie.
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
    assert provenance.inputs["variation_value"] == 32.0
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
