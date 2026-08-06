"""Content-addressed result cache and provenance (#47).

The acceptance criterion is :func:`test_an_unchanged_design_is_reused_and_a_changed_one_is_not`
— "re-solves an unchanged design in the time it takes to read a file, reports `cached: true`,
and reports `cached: false` the moment any input that affects the answer changes".

The rest fall into three groups. The **key** tests pin what does and does not change it, which
is the whole correctness surface: a key that is too coarse serves a wrong answer, one that is
too fine merely misses. The **reuse** tests cover what a hit does to quotas, history and
in-flight jobs. The **safety** tests cover the two ways this can be wrong on purpose —
a nondeterministic adapter, and a stale adapter version.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from fenixspoon import cache
from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import Domain2D, Polygon2D
from fenixspoon.jobs import JobManager
from fenixspoon.main import create_app

AIRFOIL = Domain2D(
    bounds=(-1.0, -1.0, 2.0, 1.0),
    obstacle=Polygon2D(points=[(0.0, 0.0), (0.35, 0.09), (1.0, 0.0), (0.35, -0.06)]),
)
AIRFOIL_JSON = AIRFOIL.model_dump(mode="json")
FAST = {"resolution": 48, "iterations": 200, "write_vtk": False}


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def solve(core, me, geometry=AIRFOIL, **overrides):
    async def run():
        job = await core.submit("mock.laplace2d", geometry, {**FAST, **overrides}, me)
        deadline = asyncio.get_running_loop().time() + 60
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return core.job(job.id, me)

    job = asyncio.run(run())
    assert job.status == "done", job.error
    return job


def key_for(core, **overrides):
    solver_cls = core.capability("mock.laplace2d")
    params = solver_cls.Params.model_validate({**FAST, **overrides})
    return core.cache_key_for(solver_cls, AIRFOIL, params)


# ------------------------------------------------------------------ acceptance criterion


def test_an_unchanged_design_is_reused_and_a_changed_one_is_not(tmp_path):
    """#47's acceptance criterion, through the workspace loop it exists for."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    me = Principal(id="tester", quotas=Quotas())
    geometry_ref = core.create_object("geometry", AIRFOIL_JSON, me).ref
    design_ref = core.create_object(
        "design", {"solver": "mock.laplace2d", "geometry": geometry_ref, "params": FAST}, me
    ).ref

    def run_design():
        async def go():
            job = await core.submit_design(design_ref, me)
            deadline = asyncio.get_running_loop().time() + 60
            while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
            return core.job(job.id, me)

        return asyncio.run(go())

    first = run_design()
    assert core.result_levels(first.id, me).provenance.cached is False

    # Re-solving the unchanged design reuses the answer.
    second = run_design()
    assert second.id == first.id
    assert core.result_levels(second.id, me).provenance.cached is True

    # This used to also assert `cached_seconds < computed_seconds / 2`, and that assertion
    # was measuring the wrong thing. The wait loop above polls every 20 ms, and `FAST` makes
    # the solve itself a few milliseconds — so *both* runs are one poll tick long and the
    # ratio is scheduler jitter. It failed on CI at 17 ms against a 22 ms baseline, which is
    # two samples of the same number. What the criterion actually claims is "did it
    # recompute", and the two lines above answer that exactly: same job id, and `cached`
    # true from the provenance the server itself recorded.

    # Patch one control point — an input that affects the answer — and it recomputes.
    core.patch_object(
        geometry_ref, [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.2]}], me
    )
    third = run_design()
    assert third.id != first.id
    assert core.result_levels(third.id, me).provenance.cached is False


# ----------------------------------------------------------------------------- the key


def test_the_same_inputs_give_the_same_key(core):
    assert key_for(core) == key_for(core)


def test_the_key_is_stable_across_representations(core):
    """A caller that omits a default and one that states it want the same answer, and a
    caller that sends `64.0` for an integer means 64.

    This is why the *validated* models are hashed rather than the submitted JSON: pydantic
    normalises both, and hashing the raw request would miss on every one of these.
    """
    base = key_for(core)
    assert key_for(core, resolution=48.0) == base  # int-coerced
    assert key_for(core, u_inf=1.0) == base  # explicit default
    # Key order in the request cannot matter either: `canonical` sorts.
    assert cache.canonical({"b": 1, "a": 2}) == cache.canonical({"a": 2, "b": 1})


def test_negative_zero_does_not_miss():
    """Same number, different bytes. A geometry reflected through the origin should not
    recompute over a sign bit."""
    assert cache.canonical([0.0]) == cache.canonical([-0.0])


def test_floats_serialise_the_same_way_every_time():
    """`json.dumps` uses Python's shortest-round-trip repr, which is what makes the key
    portable at all. Asserted rather than assumed."""
    assert cache.canonical([0.1, 1e-7, 1 / 3]) == '[0.1,1e-07,0.3333333333333333]'


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("a parameter that changes the field", {"u_inf": 2.0}),
        ("the resolution", {"resolution": 64}),
        ("the iteration count", {"iterations": 400}),
        ("whether an artifact is written", {"write_vtk": True}),
    ],
)
def test_changing_an_input_changes_the_key(core, label, overrides):
    del label
    assert key_for(core, **overrides) != key_for(core)


def test_changing_the_geometry_changes_the_key(core):
    solver_cls = core.capability("mock.laplace2d")
    params = solver_cls.Params.model_validate(FAST)
    moved = AIRFOIL.model_copy(deep=True)
    moved.obstacle.points[1] = (0.35, 0.2)
    assert core.cache_key_for(solver_cls, moved, params) != core.cache_key_for(
        solver_cls, AIRFOIL, params
    )


def test_a_solver_version_bump_invalidates_the_key(core, monkeypatch):
    """The one thing nothing can detect automatically. An adapter can be rewritten from
    Jacobi to multigrid without a signature changing, and without this the cache would go
    on serving the old field forever."""
    before = key_for(core)
    monkeypatch.setattr(core.capability("mock.laplace2d"), "version", "2")
    assert key_for(core) != before


def test_the_environment_is_part_of_the_key(core, monkeypatch):
    """A dolfinx or numpy upgrade changes answers, and a cache that survived one would be
    serving results from a different program."""
    before = key_for(core)
    monkeypatch.setattr(
        cache, "environment_fingerprint", lambda requires: {"numpy": "0.0.0-pretend"}
    )
    assert key_for(core) != before


def test_a_package_that_is_absent_is_recorded_as_absent():
    """"Solved before dolfinx was installed" and "solved with dolfinx" must be different
    keys, not the same one with a field left out."""
    fingerprint = cache.environment_fingerprint(["definitely_not_installed"])
    assert fingerprint["definitely_not_installed"] == "absent"
    assert fingerprint["numpy"] != "absent"


def test_the_key_cannot_collide_across_field_boundaries():
    """Each part is named in the hashed structure, so two different arrangements of the same
    characters do not land on one key the way a naive concatenation would."""
    common = dict(geometry={}, params={}, environment={})
    assert cache.cache_key(solver="ab", solver_version="c", **common) != cache.cache_key(
        solver="a", solver_version="bc", **common
    )


# ----------------------------------------------------------------------------- reuse


def test_a_hit_returns_the_job_that_already_ran(core, me):
    first = solve(core, me)
    second = solve(core, me)
    assert second.id == first.id
    # One job, not two: the entry *is* the job, so there is nothing copied and nothing to
    # keep consistent.
    assert core.history(me)[1] == 1


def test_a_hit_costs_no_quota(tmp_path):
    """It costs no compute, so it costs no quota. Deliberate, and the reason the quota
    tests elsewhere submit *different* work."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    capped = Principal(id="capped", quotas=Quotas(jobs_per_hour=1))
    first = solve(core, capped)
    for _ in range(3):
        assert solve(core, capped).id == first.id
    # …and real work is still capped.
    with pytest.raises(errors.QuotaExceeded):
        asyncio.run(core.submit("mock.laplace2d", AIRFOIL, {**FAST, "u_inf": 9.0}, capped))


def test_the_cache_is_scoped_to_one_principal(core):
    """A cross-principal hit would be a bigger saving and a disclosure: it would tell bob
    that alice has run this exact geometry."""
    alice = Principal(id="alice", quotas=Quotas())
    bob = Principal(id="bob", quotas=Quotas())
    hers = solve(core, alice)
    his = solve(core, bob)
    assert his.id != hers.id
    assert core.result_levels(his.id, bob).provenance.cached is False


def test_two_identical_submissions_attach_to_one_solve(core, me):
    """Not just finished jobs: a `queued` or `running` match is a hit too, which is how a
    race resolves into one solve instead of two."""

    async def run():
        first = await core.submit("mock.laplace2d", AIRFOIL, {**FAST, "iterations": 20000}, me)
        second = await core.submit("mock.laplace2d", AIRFOIL, {**FAST, "iterations": 20000}, me)
        assert second.id == first.id
        assert core.job(first.id, me).status in ("queued", "running")
        await core.cancel(first.id, me)

    asyncio.run(run())


def test_a_failed_job_is_not_reused(core, me, monkeypatch):
    """A failure is not an answer, and re-running is the whole point of submitting again."""
    solver_cls = core.capability("mock.laplace2d")
    monkeypatch.setattr(
        solver_cls, "solve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    async def run():
        job = await core.submit("mock.laplace2d", AIRFOIL, FAST, me)
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        assert core.job(job.id, me).status == "failed"
        second = await core.submit("mock.laplace2d", AIRFOIL, FAST, me)
        assert second.id != job.id

    asyncio.run(run())


def test_the_cache_survives_a_restart(tmp_path, me):
    """The entry is a row, not an in-memory map: a second process hits the first's work."""
    first = solve(FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs")), me)
    reopened = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    assert solve(reopened, me).id == first.id


def test_sweeping_the_job_sweeps_the_entry(tmp_path, me):
    """The retention question #47 asked, answered by construction rather than by policy: a
    cache entry *is* its job, so there is no second lifetime and no dangling pointer. The
    next identical submission is a miss and recomputes."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs", job_ttl=0.0001))
    first = solve(core, me)
    assert core.jobs.purge_expired() == [first.id]
    assert solve(core, me).id != first.id


def test_caching_can_be_switched_off(tmp_path, me):
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs", cache=False))
    assert solve(core, me).id != solve(core, me).id
    assert core.environment(me).cache.enabled is False


# ---------------------------------------------------------------------------- safety


def test_a_nondeterministic_adapter_is_never_cached(core, me, monkeypatch):
    """The default is opt-out, and this is why: serving a cached answer for a solver that
    does not reproduce is a wrong answer delivered fast."""
    monkeypatch.setattr(core.capability("mock.laplace2d"), "deterministic", False)
    assert key_for(core) is None
    first = solve(core, me)
    assert first.cache_key is None
    assert solve(core, me).id != first.id
    assert core.result_levels(first.id, me).provenance.cache_key is None


def test_every_shipped_adapter_states_a_determinism_position():
    """Not that they all say yes — that each one has an author's answer rather than a
    default nobody looked at."""
    from fenixspoon.solvers import registered_solvers

    for cls in registered_solvers():
        assert cls.deterministic is True, f"{cls.name} opted out of caching; is that intended?"
        assert cls.version


# ------------------------------------------------------------------------ provenance


def test_provenance_is_in_the_default_answer(core, me):
    """`cached` changes how everything else in the payload should be read, so it is not
    behind a flag."""
    job = solve(core, me)
    provenance = core.result_levels(job.id, me).provenance
    assert provenance.job_id == job.id
    assert provenance.cached is False
    assert provenance.solver == "mock.laplace2d"
    assert provenance.solver_version == "1"
    assert provenance.cache_key and len(provenance.cache_key) == cache.DIGEST_CHARS
    assert provenance.computed_at and provenance.seconds > 0
    assert provenance.environment["numpy"]


def test_provenance_reports_what_ran_not_what_is_installed_now(core, me, monkeypatch):
    """Recorded at submit, never recomputed at read (review of #47).

    The first implementation asked the live registry, which answers a different question:
    "what would produce this now". They diverge exactly when it matters — the day an
    adapter ships a fix and bumps its version, every job it ever ran would start claiming
    the new one. And the job would contradict itself, since the `cache_key` stored beside
    the version was derived from the old one.
    """
    job = solve(core, me)
    before = core.provenance(job.id, me)
    assert before.solver_version == "1"

    monkeypatch.setattr(core.capability("mock.laplace2d"), "version", "2")
    after = core.provenance(job.id, me)
    assert after.solver_version == "1", "an adapter bump rewrote a finished job's history"
    assert after.cache_key == before.cache_key

    # The bump does invalidate the *cache*, which is the other half of the same fact: the
    # old job is still described truthfully, and it is no longer reused.
    assert solve(core, me).id != job.id


def test_provenance_records_the_environment_rather_than_re_reading_it(core, me, monkeypatch):
    """Same reason, for the packages: an upgrade does not change what already ran."""
    job = solve(core, me)
    recorded = core.provenance(job.id, me).environment
    assert recorded["numpy"]

    monkeypatch.setattr(core.capability("mock.laplace2d"), "requires", ["numpy", "scipy"])
    assert core.provenance(job.id, me).environment == recorded


def test_a_job_recorded_before_provenance_existed_says_unknown(core, me):
    """An upgraded database has rows with no fingerprint, and `unknown` is the honest answer.

    Filling them in from today's registry is the misreport the whole field exists to
    prevent — it would be indistinguishable from a job that really did run on this version.
    The same answer covers a solver that is no longer installed.
    """
    from fenixspoon.store import JobRecord

    core.jobs.store.put(
        JobRecord(id="j-old", solver="mock.laplace2d", status="done", owner=me.id)
    )
    provenance = core.provenance("j-old", me)
    assert provenance.solver_version == "unknown"
    assert provenance.environment == {}


def test_provenance_names_the_object_revisions_a_design_resolved(tmp_path, me):
    """The design → job → result relation, recorded where it is knowable."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    geometry_ref = core.create_object("geometry", AIRFOIL_JSON, me).ref
    design_ref = core.create_object(
        "design", {"solver": "mock.laplace2d", "geometry": geometry_ref, "params": FAST}, me
    ).ref

    async def run():
        job = await core.submit_design(design_ref, me)
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        return job

    job = asyncio.run(run())
    provenance = core.result_levels(job.id, me).provenance
    assert provenance.inputs["design"] == f"{design_ref}@1"
    assert provenance.inputs["geometry"] == f"{geometry_ref}@1"


def test_the_relation_is_queryable_backwards(tmp_path, me):
    """"Explicit design → job → result relation, queryable from the workspace" — read from
    the object rather than from the job."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    geometry_ref = core.create_object("geometry", AIRFOIL_JSON, me).ref
    design_ref = core.create_object(
        "design", {"solver": "mock.laplace2d", "geometry": geometry_ref, "params": FAST}, me
    ).ref

    async def run():
        job = await core.submit_design(design_ref, me)
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        return job

    job = asyncio.run(run())
    assert [j.id for j in core.jobs_for_object(geometry_ref, me)] == [job.id]
    assert [j.id for j in core.jobs_for_object(design_ref, me)] == [job.id]
    # Pinned finds the one revision; a revision nothing ran on finds nothing.
    assert [j.id for j in core.jobs_for_object(f"{geometry_ref}@1", me)] == [job.id]
    assert core.jobs_for_object(f"{geometry_ref}@9", me) == []
    # Scoped, like everything else keyed on a principal.
    assert core.jobs_for_object(geometry_ref, Principal(id="x", quotas=Quotas())) == []
    with pytest.raises(errors.MalformedReference):
        core.jobs_for_object("not-a-reference", me)


def test_an_unpinned_reference_does_not_match_a_longer_id(tmp_path, me):
    """`geometry:g-1` must not match `geometry:g-12`. A bare prefix comparison would."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    refs = [core.create_object("geometry", AIRFOIL_JSON, me).ref for _ in range(12)]
    assert refs[-1] == "geometry:g-12"
    design = core.create_object(
        "design",
        {"solver": "mock.laplace2d", "geometry": refs[-1], "params": FAST},
        me,
    ).ref

    async def run():
        job = await core.submit_design(design, me)
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        return job

    job = asyncio.run(run())
    assert [j.id for j in core.jobs_for_object("geometry:g-12", me)] == [job.id]
    assert core.jobs_for_object("geometry:g-1", me) == []


# ------------------------------------------------------------------------ HTTP binding


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    with TestClient(create_app()) as test_client:
        yield test_client


def submit_http(client, **params):
    return client.post(
        "/api/v1/jobs",
        json={"solver": "mock.laplace2d", "geometry": AIRFOIL_JSON, "params": {**FAST, **params}},
    ).json()


def wait(client, job_id):
    deadline = time.monotonic() + 30
    while client.get(f"/api/v1/jobs/{job_id}").json()["status"] != "done":
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.05)


def test_the_submit_response_says_whether_anything_ran(client):
    """A page that assumes a fresh submission is always `queued` would wait for a
    transition that already happened, so the 202 has to say."""
    first = submit_http(client)
    assert first["cached"] is False and first["status"] == "queued"
    wait(client, first["job_id"])

    second = submit_http(client)
    assert second["job_id"] == first["job_id"]
    assert second["cached"] is True
    assert second["status"] == "done"


def test_provenance_reaches_both_result_routes(client):
    job_id = submit_http(client)["job_id"]
    wait(client, job_id)

    envelope = client.get(f"/api/v1/jobs/{job_id}/result").json()
    assert envelope["provenance"]["cached"] is False
    assert envelope["provenance"]["cache_key"]

    compact = client.get(f"/api/v1/jobs/{job_id}/summary").json()
    assert compact["provenance"]["job_id"] == job_id

    only = client.get(
        f"/api/v1/jobs/{job_id}/summary", params={"levels": ["provenance"]}
    ).json()
    assert set(only) == {"job_id", "solver", "provenance"}
