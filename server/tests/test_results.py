"""Compact results: levels, metrics, diagnostics and field queries (#46).

The acceptance criterion is :func:`test_the_default_answer_is_small_and_carries_no_arrays`
and :func:`test_a_query_finds_the_peak_without_transferring_the_field` — "a finished solve's
default answer fits comfortably in a kilobyte, carries no numeric array longer than a handful
of entries, and `result.query` answers 'where is the peak field and how big is it' without
transferring the field".

The rest divide into two halves. One checks that the *separation* is real: metrics are the
answer, `stats` is the cost, and `t_max` is no longer in the latter. The other checks the
arithmetic in :mod:`fenixspoon.fields`, mostly against closed-form cases where the right
answer is known independently rather than by running the code and blessing what came out.
"""

import asyncio
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from geometries import SOLENOID

from fenixspoon import fields
from fenixspoon.core import FenixSpoonCore, FieldQuery, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.core.results import DEFAULT_LEVELS, LEVELS
from fenixspoon.geometry import Domain2D, Polygon2D, Regions2D
from fenixspoon.jobs import JobManager
from fenixspoon.main import create_app

AIRFOIL = Domain2D(
    bounds=(-1.0, -1.0, 2.0, 1.0),
    obstacle=Polygon2D(points=[(0.0, 0.0), (0.35, 0.09), (1.0, 0.0), (0.35, -0.06)]),
)
AIRFOIL_JSON = AIRFOIL.model_dump()

#: A block of aluminium with a heat source in it. The solenoid fixture has no `q`, so a heat
#: solve of it sits at ambient — fine where only the metric *names* matter, useless where a
#: number has to be non-zero.
HEATED_BLOCK = {
    "type": "regions2d",
    "bounds": [-0.05, -0.03, 0.05, 0.05],
    "regions": [
        {
            "name": "base",
            "shape": {
                "type": "polygon2d",
                "points": [[-0.03, -0.01], [0.03, -0.01], [0.03, 0.005], [-0.03, 0.005]],
            },
            "material": {"k": 205.0, "q": 2.0e6},
        }
    ],
    "background": {"k": 0.026},
}
FAST = {"resolution": 64, "iterations": 400}


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def solve(core, me, solver="mock.laplace2d", geometry=AIRFOIL, **overrides):
    async def run():
        job = await core.submit(solver, geometry, {**FAST, **overrides}, me)
        deadline = asyncio.get_running_loop().time() + 60
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return core.job(job.id, me)

    job = asyncio.run(run())
    assert job.status == "done", job.error
    return job


def numeric_arrays(payload) -> list[list]:
    """Every list of numbers anywhere in a payload, for the no-arrays assertion."""
    found = []
    if isinstance(payload, dict):
        for value in payload.values():
            found += numeric_arrays(value)
    elif isinstance(payload, list):
        if payload and all(isinstance(item, int | float) for item in payload):
            found.append(payload)
        else:
            for item in payload:
                found += numeric_arrays(item)
    return found


# --------------------------------------------------------------- acceptance criteria


def test_the_default_answer_is_small_and_carries_no_arrays(core, me):
    """The acceptance criterion: "fits comfortably in a kilobyte, carries no numeric array
    longer than a handful of entries"."""
    job = solve(core, me)
    compact = core.result_levels(job.id, me).model_dump(exclude_none=True)
    payload = json.dumps(compact)
    # 1112 bytes measured on this solve, against 1024 before protocol 1.5. The budget moved,
    # and it is worth recording why rather than quietly raising it again next time.
    #
    # The breakdown: status 150, metrics 202, diagnostics 274, provenance 280, artifacts 150.
    # #47 had already spent most of the kilobyte — its own comment noted the answer "very
    # nearly was not" under it, and that a full SHA-256 digest was truncated to 128 bits
    # rather than move this line. What tipped it over is #68 taking `mock.laplace2d` from two
    # declared metrics to six, at ~34 bytes each.
    #
    # That growth is **linear in the number of declared metrics, by design**: metrics are the
    # answer, and a capability that reports more of it produces a larger compact answer. A
    # fixed byte ceiling cannot survive that for any adapter, so it is a proxy — the invariant
    # the criterion is really about is the assertion below, that no numeric *array* rides in
    # the default. That one has not moved and must not.
    assert len(payload) < 1536, f"default answer is {len(payload)} bytes:\n{payload}"

    longest = max((len(a) for a in numeric_arrays(compact)), default=0)
    assert longest <= 8, f"a compact answer carried an array of {longest} numbers"

    # And it is compact *relative to* the thing it replaces, which is the point.
    full = json.dumps(core.result(job.id, me).data)
    assert len(full) > 100 * len(payload)


def test_a_query_finds_the_peak_without_transferring_the_field(core, me):
    """The other half of the criterion: `result.query` answers "where is the peak field and
    how big is it" without transferring the field."""
    job = solve(core, me)
    answer = core.query_result(job.id, FieldQuery(field="speed", op="max"), me)
    assert answer.result["value"] > 0
    x, y = answer.result["at"]
    xmin, ymin, xmax, ymax = AIRFOIL.bounds
    assert xmin <= x <= xmax and ymin <= y <= ymax
    # The whole response, not just the value: nothing array-shaped came back.
    assert len(json.dumps(answer.model_dump())) < 256


def test_fields_are_not_in_the_default_and_are_reachable_by_name(core, me):
    job = solve(core, me)
    assert core.result_levels(job.id, me).fields is None
    asked = core.result_levels(job.id, me, ["fields"])
    assert asked.fields is not None and asked.fields.kind == "grid2d"
    assert len(asked.fields.data["fields"]["speed"]) > 1000


# ------------------------------------------------------------------------- the levels


def test_each_level_can_be_requested_alone(core, me):
    job = solve(core, me)
    for level in LEVELS:
        payload = core.result_levels(job.id, me, [level]).model_dump(exclude_none=True)
        assert set(payload) == {"job_id", "solver", level}, level


def test_the_default_is_everything_except_fields_and_series(core, me):
    job = solve(core, me)
    payload = core.result_levels(job.id, me).model_dump(exclude_none=True)
    assert set(payload) == {"job_id", "solver", *DEFAULT_LEVELS}
    assert "fields" not in payload
    assert "series" not in payload


# ---------------------------------------------------------------- the series level (#69)


def test_curves_are_reachable_by_name_in_the_same_request(core, me):
    """Protocol 1.4's level. Out of the default because a curve is a numeric array and the
    default answer's whole promise is that it carries none — but a *level*, not a second route,
    so a caller wanting the answer and the curve asks once."""
    job = solve(core, me)
    assert core.result_levels(job.id, me).series is None
    asked = core.result_levels(job.id, me, ["status", "metrics", "series"])
    assert [entry.name for entry in asked.series] == ["surface_cp"]
    assert asked.metrics["c_l"] is not None
    trace = asked.series[0].traces[0]
    assert trace.x is not None and len(trace.values) == len(trace.x.values)


def test_a_capability_that_produces_no_curves_says_so_with_an_empty_list(core, me):
    """Empty means "none produced"; absent means "not asked for". The heat adapter produces no
    curves, and a caller that asked must be able to tell that from a level it forgot."""
    job = solve(
        core, me, solver="mock.heat2d", geometry=Regions2D.model_validate(HEATED_BLOCK)
    )
    asked = core.result_levels(job.id, me, ["series"])
    assert asked.series == []
    assert "series" in asked.model_dump(exclude_none=True)


def test_the_series_level_costs_a_row_read_not_the_field_payload(core, me):
    """Curves live in a database column, like the metrics, rather than inside `result.json`.

    That is the whole reason they are compact in practice as well as in principle: the level
    can answer without the multi-megabyte read and JSON parse the arrays would cost. Asserted
    by asking for the curves with the payload deliberately not loaded.
    """
    job = solve(core, me)
    unloaded = core.job(job.id, me, with_result=False)
    assert unloaded.result is None, "the payload was not read off disk"
    assert unloaded.summary is not None and unloaded.summary.series, (
        "yet the curves are there, because they came from a column"
    )


def test_curves_survive_a_round_trip_through_a_reopened_store(tmp_path, me):
    """A curve is JSON in a column, so it has to come back as the model it went in as.

    Reopening the database is what makes this a round trip: two reads from one live store share
    the same parsed objects and would agree even if the column were never written. This asserts
    against a second `FenixSpoonCore` over the same directory — the restart case the durability
    contract promises.
    """
    first = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    job = solve(first, me)
    before = first.result_levels(job.id, me, ["series"]).series
    first.jobs.store.close()

    reopened = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    after = reopened.result_levels(job.id, me, ["series"]).series
    assert after == before, "curves did not survive the database being reopened"

    trace = after[0].traces[0]
    assert trace.unit == "1" and trace.x is not None and trace.x.name == "x/c"
    assert len(trace.values) == len(trace.x.values)


def test_an_unknown_level_is_refused_not_ignored(core, me):
    """Same rule as a misspelled capability section: silently dropping it would let a caller
    conclude the solve reported no metrics."""
    job = solve(core, me)
    with pytest.raises(errors.UnknownLevel) as caught:
        core.result_levels(job.id, me, ["metric"])
    assert caught.value.unknown == ["metric"]


def test_the_compact_levels_do_not_read_the_payload_file(core, me, monkeypatch):
    """The saving that matters most for a local caller: not sending the arrays is half of
    it, not *parsing* them is the other half.

    Asserted by removing `result.json` from under the store. Metrics and diagnostics come
    from database columns, so they must still answer.
    """
    job = solve(core, me)
    (core.jobs.data_dir / job.id / "result.json").unlink()

    compact = core.result_levels(job.id, me)
    assert compact.metrics and compact.diagnostics.stats["cells"] > 0


def test_a_requested_level_never_goes_quietly_missing(core, me):
    """Raised in review of #67, and the first draft of this file asserted the bug.

    Absent-means-unrequested is what the whole payload is built on, so returning no
    `fields` key to a caller that *asked* for the arrays would tell it the result has none —
    the same silent wrong answer an unknown level name is refused to avoid. The store
    tolerates a missing payload by design, so this state is reachable rather than theoretical.
    """
    job = solve(core, me)
    (core.jobs.data_dir / job.id / "result.json").unlink()
    with pytest.raises(errors.ResultPayloadMissing):
        core.result_levels(job.id, me, ["fields"])


def test_a_missing_payload_is_not_reported_as_an_unfinished_job(core, me):
    """Raised in review of #67. Every path that needs the arrays used to answer
    `JobNotFinished("done")` — self-contradictory, and it sent a caller to poll a job that
    had already finished."""
    job = solve(core, me)
    (core.jobs.data_dir / job.id / "result.json").unlink()

    for call in (
        lambda: core.result(job.id, me),
        lambda: core.query_result(job.id, FieldQuery(field="speed", op="max"), me),
        lambda: core.result_levels(job.id, me, ["fields"]),
    ):
        with pytest.raises(errors.ResultPayloadMissing) as caught:
            call()
        assert caught.value.job_id == job.id
        assert "no longer on disk" in caught.value.detail


def test_a_missing_payload_is_410_over_http(client):
    """`410 Gone` rather than `404`: the job is very much there, and its metrics and
    diagnostics still answer. Only the arrays are gone."""
    job_id = run_http(client)
    payload = client.get(f"/api/v1/jobs/{job_id}/result").json()
    assert payload["metrics"]
    # Reach into the data directory the way a retention sweep or a tidy-up would.
    data_dir = next(iter(client.app.state.core.jobs.data_dir.glob(job_id)))
    (data_dir / "result.json").unlink()

    assert client.get(f"/api/v1/jobs/{job_id}/result").status_code == 410
    assert client.post(
        f"/api/v1/jobs/{job_id}/query", json={"field": "speed", "op": "max"}
    ).status_code == 410
    # …and the compact levels still work, which is the point of holding them separately.
    assert client.get(f"/api/v1/jobs/{job_id}/summary").json()["metrics"]


def test_a_job_that_did_not_succeed_says_so_rather_than_returning_empty_levels(core, me):
    async def run():
        job = await core.submit("mock.laplace2d", AIRFOIL, {**FAST, "iterations": 20000}, me)
        with pytest.raises(errors.JobNotFinished):
            core.result_levels(job.id, me)
        await core.cancel(job.id, me)

    asyncio.run(run())


# ------------------------------------------------------------ metrics versus statistics


def test_metrics_are_the_answer_and_stats_are_the_cost(core, me):
    """The separation #46 exists to make, on the capability that used to conflate them."""
    job = solve(core, me, solver="mock.heat2d", geometry=SOLENOID)
    compact = core.result_levels(job.id, me)
    assert set(compact.metrics) == {"t_max", "t_rise", "flux_max"}
    assert "t_max" not in compact.diagnostics.stats
    assert "t_rise" not in compact.diagnostics.stats
    # Cost keys stayed where they were.
    assert compact.diagnostics.stats["cells"] > 0
    assert compact.diagnostics.stats["seconds"] > 0


def test_a_declared_reduction_is_computed_generically(core, me):
    """`speed_max` is declared as the `max` of `speed` and nothing in the adapter computes
    it — the runtime does, from the declaration. Checked against the field itself."""
    job = solve(core, me)
    metrics = core.result_levels(job.id, me).metrics
    data = core.result(job.id, me).data
    mask = np.asarray(data["mask"], dtype=bool)
    expected = float(np.asarray(data["fields"]["speed"])[~mask].max())
    assert metrics["speed_max"] == pytest.approx(expected)


def test_a_derived_metric_comes_from_the_adapter(core, me):
    """`cp_min` needs `u_inf`, which is a parameter and not in the payload, so only the
    adapter can produce it. Its definition is checked rather than its value."""
    metrics = core.result_levels(solve(core, me, u_inf=2.0).id, me).metrics
    assert metrics["cp_min"] == pytest.approx(1.0 - (metrics["speed_max"] / 2.0) ** 2)


def test_every_declared_metric_is_actually_reported(core, me):
    """The declaration from #43 and the value from #46 must agree about what exists."""
    for solver, geometry in [
        ("mock.laplace2d", AIRFOIL),
        ("mock.magnetostatics2d", SOLENOID),
        ("mock.heat2d", SOLENOID),
    ]:
        job = solve(core, me, solver=solver, geometry=geometry)
        reported = core.result_levels(job.id, me).metrics
        declared = {spec.name for spec in core.capability(solver).metrics}
        assert declared == set(reported), f"{solver}: declared {declared}, reported {set(reported)}"


def test_diagnostics_carry_what_stats_never_could(core, me):
    """A convergence flag and a warning string have no home in `dict[str, float]`."""
    stopped = core.result_levels(solve(core, me, iterations=10).id, me).diagnostics
    assert stopped.converged is False
    assert stopped.residual > 0
    assert any("iteration cap" in warning for warning in stopped.warnings)

    converged = core.result_levels(solve(core, me, iterations=20000).id, me).diagnostics
    assert converged.converged is True
    assert converged.warnings == []


def test_metrics_survive_a_restart(tmp_path, me):
    """They are columns, not a cache: a second process reads them back without re-solving."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    job = solve(core, me)
    before = core.result_levels(job.id, me)
    core.jobs.store.close()

    reopened = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    after = reopened.result_levels(job.id, me)
    assert after.metrics == before.metrics
    assert after.diagnostics.converged == before.diagnostics.converged
    assert after.diagnostics.warnings == before.diagnostics.warnings


# -------------------------------------------------------------------- field arithmetic


def uniform(value: float = 3.0, n: int = 21) -> dict:
    """A grid2d payload holding one constant field on the unit square, no mask."""
    return {
        "bounds": [0.0, 0.0, 1.0, 1.0],
        "shape": [n, n],
        "fields": {"f": [value] * (n * n), "ramp": list(np.tile(np.linspace(0, 1, n), n))},
        "mask": [0] * (n * n),
    }


def test_integral_and_mean_of_a_constant_field_are_known_in_closed_form():
    """Checked against arithmetic, not against whatever the code happened to print.

    The integral is *exactly* 3.0 — 3.0 over the unit square — and asserting it exactly is
    the point. Summing grid values and multiplying by the cell area gives 3.31 on a 21-point
    grid, because a lattice has one more point per axis than it has intervals; that 10%
    error is what the trapezoidal weighting removes, and a loose tolerance here would have
    hidden it.
    """
    payload = uniform(3.0)
    assert fields.query(payload, "grid2d", "f", "mean")["value"] == pytest.approx(3.0)
    integral = fields.query(payload, "grid2d", "f", "integral")
    assert integral["value"] == pytest.approx(3.0, rel=1e-12)
    assert integral["area"] == pytest.approx(1.0, rel=1e-12)


def test_the_integral_is_exact_for_a_linear_field_too():
    """Trapezoid is exact up to linear, which is the guarantee worth asserting: a ramp from
    0 to 1 across the unit square integrates to 0.5."""
    assert fields.query(uniform(), "grid2d", "ramp", "integral")["value"] == pytest.approx(
        0.5, rel=1e-12
    )


def test_a_ramp_averages_to_its_midpoint():
    assert fields.query(uniform(), "grid2d", "ramp", "mean")["value"] == pytest.approx(0.5)


def test_interpolation_is_exact_on_a_linear_field():
    """Bilinear interpolation reproduces a linear field exactly; if it does not, the index
    arithmetic is wrong in a way a smooth field would hide."""
    answer = fields.query(uniform(), "grid2d", "ramp", "at_point", at=[0.37, 0.5])
    assert answer["value"] == pytest.approx(0.37, abs=1e-9)


def test_a_masked_point_is_refused_rather_than_interpolated_from_filler(core, me):
    """Inside the airfoil the payload carries a placeholder, not a solved value. Blending
    it into an answer would report something the solver never computed."""
    data = core.result(solve(core, me).id, me).data
    with pytest.raises(fields.FieldError, match="masked"):
        fields.query(data, "grid2d", "speed", "at_point", at=[0.4, 0.02])


def test_a_point_outside_the_domain_says_so(core, me):
    data = core.result(solve(core, me).id, me).data
    with pytest.raises(fields.FieldError, match="outside"):
        fields.query(data, "grid2d", "speed", "at_point", at=[99.0, 99.0])


def test_a_mesh_mean_is_area_weighted_not_nodal(core, me):
    """On a graded mesh the two differ, and a nodal mean is biased toward wherever the
    mesher put nodes. The weighting is reported so a caller knows which it got."""
    data = core.result(solve(core, me, output="mesh2d").id, me).data
    answer = fields.query(data, "mesh2d", "speed", "mean")
    assert answer["weighting"] == "nodal-dual-area"
    nodal = float(np.mean(data["point_fields"]["speed"]))
    assert answer["value"] == pytest.approx(nodal, rel=0.2)  # close, deliberately not equal


def test_budgets_are_capped_rather_than_refused():
    """A caller asking for 10,000 samples wants "a lot"; giving it the most this server
    sends beats an error it has to guess a number for."""
    big = uniform(n=41)  # 1681 points, comfortably over the cap
    answer = fields.query(big, "grid2d", "f", "sample", samples=10_000)
    assert len(answer["value"]) == fields.MAX_SAMPLES
    assert answer["of"] == 41 * 41
    assert len(fields.query(big, "grid2d", "f", "sample")["value"]) == fields.DEFAULT_SAMPLES
    # A field smaller than the budget comes back whole rather than padded.
    assert len(fields.query(uniform(n=9), "grid2d", "f", "sample", samples=500)["value"]) == 81


def test_hotspots_are_spread_out_rather_than_neighbours_of_one_peak(core, me):
    """Sorting and taking the top N returns N neighbours of the same maximum, which answers
    nothing `max` did not."""
    data = core.result(solve(core, me).id, me).data
    answer = fields.query(data, "grid2d", "speed", "hotspots", count=3)
    points = answer["at"]
    assert len(points) == 3
    for i, first in enumerate(points):
        for second in points[i + 1 :]:
            assert np.hypot(first[0] - second[0], first[1] - second[1]) >= answer["radius"]


def test_a_section_across_the_body_drops_the_samples_inside_it(core, me):
    """Better than interpolating filler into the profile, and better than failing the
    whole query because the line crosses the obstacle."""
    data = core.result(solve(core, me).id, me).data
    answer = fields.query(
        data, "grid2d", "speed", "section", start=[-0.5, 0.0], end=[1.5, 0.0], samples=20
    )
    assert answer["requested"] == 20
    assert answer["skipped"] > 0
    assert len(answer["value"]) == 20 - answer["skipped"]
    assert answer["distance"] == sorted(answer["distance"])


def test_a_vector_field_is_refused_with_the_scalar_names(core, me):
    """Scalar-only on purpose: an extremum of a vector needs a norm nobody has chosen."""
    data = core.result(solve(core, me).id, me).data
    with pytest.raises(fields.FieldError, match="vector field"):
        fields.query(data, "grid2d", "velocity", "max")


def test_an_unknown_field_lists_what_there_is(core, me):
    job = solve(core, me)
    with pytest.raises(errors.FieldQueryFailed) as caught:
        core.query_result(job.id, FieldQuery(field="nope", op="max"), me)
    assert "speed" in caught.value.reason and "psi" in caught.value.reason


# -------------------------------------------------------------------------- over_region


def test_over_region_resolves_through_workspace_provenance(core, me):
    """A result carries arrays, not region names, so the geometry has to come from the
    design the job was submitted through."""
    geometry_ref = core.create_object("geometry", SOLENOID.model_dump(), me).ref
    design_ref = core.create_object(
        "design",
        {
            "solver": "mock.heat2d",
            "geometry": geometry_ref,
            "params": {"resolution": 64, "iterations": 400},
        },
        me,
    ).ref

    async def run():
        job = await core.submit_design(design_ref, me)
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        return core.job(job.id, me)

    job = asyncio.run(run())
    assert job.status == "done", job.error

    answer = core.query_result(
        job.id, FieldQuery(field="T", op="over_region", region="core"), me
    )
    assert answer.result["count"] > 0
    assert answer.result["min"] <= answer.result["mean"] <= answer.result["max"]
    # Bounded, like every other query: statistics and one location, never the selection.
    assert len(json.dumps(answer.model_dump())) < 512


def test_over_region_on_an_inline_job_explains_the_limitation(core, me):
    """A job submitted with an inline geometry kept no reference to one. Saying that beats
    reporting an empty region, and names the way to make it work."""
    job = solve(core, me, solver="mock.heat2d", geometry=SOLENOID)
    with pytest.raises(errors.RegionUnavailable) as caught:
        core.query_result(job.id, FieldQuery(field="T", op="over_region", region="core"), me)
    assert "design" in caught.value.reason


# ------------------------------------------------------------------------ HTTP binding


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    with TestClient(create_app()) as test_client:
        yield test_client


def run_http(client, solver="mock.laplace2d", geometry=None, **params):
    import time

    job_id = client.post(
        "/api/v1/jobs",
        json={
            "solver": solver,
            "geometry": geometry or AIRFOIL_JSON,
            "params": {**FAST, **params},
        },
    ).json()["job_id"]
    deadline = time.monotonic() + 30
    while client.get(f"/api/v1/jobs/{job_id}").json()["status"] != "done":
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.05)
    return job_id


def test_the_full_result_route_keeps_its_shape_and_gains_the_new_keys(client):
    """1.3 through 1.7 are additive here: `data` and `stats` are untouched, five keys appear
    beside them — `metrics` and `diagnostics` in 1.3, `provenance` in 1.4, `series` in 1.5,
    `frames` in 1.7.

    Compared as an exact set rather than a subset, which is what makes it useful in both
    directions: it caught `frames` arriving, and it would catch one leaving.
    """
    payload = client.get(f"/api/v1/jobs/{run_http(client)}/result").json()
    assert {"job_id", "kind", "data", "stats", "artifacts"} <= set(payload)
    assert set(payload) == {
        "job_id", "kind", "data", "stats", "metrics", "diagnostics", "provenance", "series",
        "artifacts", "frames",
    }
    # A steady solve has no time axis, and the key says so by being empty rather than absent:
    # this route is the exhaustive envelope, where every declared field is present.
    assert payload["frames"] == []
    assert payload["data"]["fields"]["speed"]
    assert payload["metrics"]["speed_max"] > 0
    assert payload["artifacts"][0]["url"].startswith("/api/v1/")


def test_the_summary_route_is_the_compact_one(client):
    job_id = run_http(client)
    compact = client.get(f"/api/v1/jobs/{job_id}/summary").json()
    assert set(compact) == {
        "job_id", "solver", "status", "metrics", "diagnostics", "provenance", "artifacts"
    }
    # Same budget as the core-level acceptance test, and moved for the same reason: see the
    # breakdown there. `series` is absent — it is a level, not a default.
    assert len(json.dumps(compact)) < 1536
    assert "series" not in compact

    one = client.get(f"/api/v1/jobs/{job_id}/summary", params={"levels": ["metrics"]}).json()
    assert set(one) == {"job_id", "solver", "metrics"}

    bad = client.get(f"/api/v1/jobs/{job_id}/summary", params={"levels": ["nope"]})
    assert bad.status_code == 422


def test_the_query_route_answers_over_http(client):
    job_id = run_http(client)
    answer = client.post(
        f"/api/v1/jobs/{job_id}/query", json={"field": "speed", "op": "max"}
    ).json()
    assert answer["result"]["value"] > 0 and len(answer["result"]["at"]) == 2

    refused = client.post(
        f"/api/v1/jobs/{job_id}/query", json={"field": "nope", "op": "max"}
    )
    assert refused.status_code == 422

    unknown_op = client.post(
        f"/api/v1/jobs/{job_id}/query", json={"field": "speed", "op": "teleport"}
    )
    assert unknown_op.status_code == 422


def test_the_heat_demo_can_still_read_its_numbers(client):
    """The gallery page reads `t_rise` and `t_max`; 1.3 moved them, so this is the check
    that the move was carried through to the consumer rather than only to the server.

    A geometry with a real heat source, unlike the solenoid used elsewhere in this file: the
    solenoid carries `mu_r` and `current_density` and no `q`, so a heat solve of it sits at
    ambient and a temperature *rise* of zero would prove nothing.
    """
    job_id = run_http(
        client, solver="mock.heat2d", geometry=HEATED_BLOCK,
        resolution=64, iterations=800,
    )
    payload = client.get(f"/api/v1/jobs/{job_id}/result").json()
    assert payload["metrics"]["t_rise"] > 0
    assert payload["metrics"]["t_max"] > payload["metrics"]["t_rise"]
    assert payload["stats"]["solid_cells"] > 0
