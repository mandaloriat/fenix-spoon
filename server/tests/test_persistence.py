"""Job persistence, listing and retention (#13).

The acceptance criterion is one sentence: restart the API and finished jobs, their
events and their artifacts are still there. Most of what follows is that sentence, plus
the failure modes a restart introduces — a job left mid-solve by a process that died,
and records outliving the files they point at.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fenixspoon.backends import InProcessBackend
from fenixspoon.events import InProcessEventBus
from fenixspoon.jobs import JobManager
from fenixspoon.main import create_app
from fenixspoon.solvers.base import SolverResult
from fenixspoon.store import JobRecord, MemoryJobStore, SqliteJobStore

GEOMETRY = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
FAST_PARAMS = {"resolution": 32, "iterations": 100, "report_every": 20}


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """A data directory shared by every app this test starts — the restart's whole point."""
    path = tmp_path / "jobs"
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(path))
    monkeypatch.setenv("FENIXSPOON_STORE", "sqlite")
    return path


def run_job(client, **overrides) -> str:
    body = {"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": FAST_PARAMS}
    body.update(overrides)
    job_id = client.post("/api/v1/jobs", json=body).json()["job_id"]
    deadline = time.monotonic() + 15
    while client.get(f"/api/v1/jobs/{job_id}").json()["status"] not in (
        "done",
        "failed",
        "cancelled",
    ):
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.05)
    return job_id


def test_history_survives_a_restart(data_dir):
    """The acceptance test for #13, verbatim: run a job, throw the app away, ask again."""
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        before = client.get(f"/api/v1/jobs/{job_id}/result").json()

    # A completely new app object, same data directory: this is the restart.
    with TestClient(create_app()) as client:
        status = client.get(f"/api/v1/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "done"

        after = client.get(f"/api/v1/jobs/{job_id}/result").json()
        assert after["kind"] == before["kind"]
        assert after["data"] == before["data"]
        assert after["stats"] == before["stats"]

        # Artifacts too — they are files, but the *record* of them was in memory.
        assert [a["name"] for a in after["artifacts"]] == ["solution.vtk"]
        assert client.get(after["artifacts"][0]["url"]).text.startswith("# vtk DataFile")

        # And the event log, so a late subscriber still gets the full history.
        with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as ws:
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event.get("type") == "status" and event["status"] == "done":
                    break
        assert events[0] == {"type": "status", "status": "running"}
        assert any(e["type"] == "progress" for e in events)


def test_a_finished_job_is_not_kept_in_memory(data_dir):
    """The live cache must not become a second, unbounded copy of the result store.

    Results are written to disk precisely so they are not held in RAM — a 512x341 grid is
    about 3 MB of JSON. But the manager also cached the live ``Job``, and nothing dropped
    it until the retention sweep, so every finished job's payload stayed resident for the
    TTL (7 days by default). Twelve solves at resolution 384 cost 82 MB of RSS that was
    never coming back; a server under real load runs out of memory long before the sweep.

    Asserting on ``_jobs`` rather than on RSS: the private attribute is the mechanism, and
    a memory measurement in a test is a flake waiting to happen.
    """
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        manager = client.app.state.jobs

        assert job_id not in manager._jobs, "finished job still cached in memory"

        # …and it is still fully answerable, rehydrated from the store.
        result = client.get(f"/api/v1/jobs/{job_id}/result")
        assert result.status_code == 200
        assert result.json()["data"], "result payload lost with the cache entry"
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "done"


def test_a_running_job_stays_in_memory(data_dir):
    """The counter-case, so the eviction above cannot become "never cache anything".

    While a job is running the live entry is what holds its cancel handle and the status
    that has moved on since the last write. Dropping *that* would break cancellation.
    """
    with TestClient(create_app()) as client:
        body = {"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": {"resolution": 220}}
        job_id = client.post("/api/v1/jobs", json=body).json()["job_id"]
        manager = client.app.state.jobs
        assert job_id in manager._jobs

        assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 202
        deadline = time.monotonic() + 15
        while client.get(f"/api/v1/jobs/{job_id}").json()["status"] not in (
            "done",
            "failed",
            "cancelled",
        ):
            assert time.monotonic() < deadline, "job did not finish"
            time.sleep(0.05)
        assert job_id not in manager._jobs


def test_listing_is_paginated_and_newest_first(data_dir):
    with TestClient(create_app()) as client:
        ids = [run_job(client) for _ in range(3)]

        page = client.get("/api/v1/jobs", params={"limit": 2}).json()
        assert page["total"] == 3
        assert [j["job_id"] for j in page["jobs"]] == ids[::-1][:2]
        assert all(j["status"] == "done" for j in page["jobs"])

        second = client.get("/api/v1/jobs", params={"limit": 2, "offset": 2}).json()
        assert [j["job_id"] for j in second["jobs"]] == [ids[0]]

    with TestClient(create_app()) as client:  # and the listing outlives the process
        restarted = client.get("/api/v1/jobs").json()
        assert restarted["total"] == 3
        assert [j["job_id"] for j in restarted["jobs"]] == ids[::-1]


def test_listing_rejects_a_silly_page_size(data_dir):
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/jobs", params={"limit": 5000}).status_code == 422
        assert client.get("/api/v1/jobs", params={"offset": -1}).status_code == 422


def test_a_job_the_server_died_on_is_failed_not_left_running(data_dir):
    """A client polling a job from a dead process must not wait forever."""
    manager = JobManager()
    manager.store.put(
        JobRecord(id="j-orphan", solver="mock.laplace2d", status="running")
    )
    manager.store.add_event("j-orphan", {"type": "status", "status": "running"})

    with TestClient(create_app()) as client:  # startup reconciles
        status = client.get("/api/v1/jobs/j-orphan").json()
        assert status["status"] == "failed"
        assert "restart" in status["error"]
        # The stream must terminate, or a reconnecting client hangs on it.
        with client.websocket_connect("/api/v1/jobs/j-orphan/events") as ws:
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event.get("type") == "status" and event["status"] in (
                    "done",
                    "failed",
                    "cancelled",
                ):
                    break
        assert events[-1]["status"] == "failed"


def test_reconcile_leaves_this_process_own_jobs_alone(data_dir):
    """Only *stranded* jobs are failed — a job this manager is running is not stranded."""
    with TestClient(create_app()) as client:
        slow = {"resolution": 256, "iterations": 20000, "report_every": 100}
        job_id = client.post(
            "/api/v1/jobs",
            json={"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": slow},
        ).json()["job_id"]
        manager = client.app.state.jobs
        assert manager.reconcile() == []
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] in ("queued", "running")
        client.post(f"/api/v1/jobs/{job_id}/cancel")


def test_expired_jobs_lose_their_records_and_their_files(data_dir):
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        artifact_dir = data_dir / job_id
        assert artifact_dir.is_dir()

        manager = client.app.state.jobs
        manager.job_ttl = 3600
        assert manager.purge_expired() == []  # young enough to keep

        # Pretend an hour passed rather than sleeping through it.
        purged = manager.purge_expired(now=datetime.now(UTC) + timedelta(hours=2))
        assert purged == [job_id]
        assert not artifact_dir.exists()
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert client.get("/api/v1/jobs").json()["total"] == 0


def test_zero_ttl_keeps_jobs_forever(data_dir):
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        manager = client.app.state.jobs
        manager.job_ttl = 0
        assert manager.purge_expired(now=datetime.now(UTC) + timedelta(days=3650)) == []
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200


def test_result_metadata_without_its_payload_is_not_a_crash(data_dir):
    """Someone deletes result.json by hand; the job must still be listable."""
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        (data_dir / job_id / "result.json").unlink()

    with TestClient(create_app()) as client:
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "done"
        assert client.get("/api/v1/jobs").json()["total"] == 1
        # The result itself is genuinely gone, and says so rather than serving nothing.
        # This was a `409` until protocol 1.3: the underlying error was `JobNotFinished`
        # with a status of `done`, which is self-contradictory and told a caller to keep
        # polling something that had already finished. `410 Gone` is the honest code — the
        # job is there, only its arrays are not.
        assert client.get(f"/api/v1/jobs/{job_id}/result").status_code == 410


@pytest.mark.parametrize("store_factory", ["memory", "sqlite"])
def test_stores_agree_on_the_contract(tmp_path, store_factory):
    """Both backends implement the same six methods with the same semantics."""
    store = (
        MemoryJobStore()
        if store_factory == "memory"
        else SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    )
    old = datetime.now(UTC) - timedelta(days=30)
    store.put(JobRecord(id="j-1", solver="s", status="running", created_at=old))
    store.put(JobRecord(id="j-2", solver="s", status="done"))
    store.add_event("j-1", {"type": "status", "status": "running"})
    store.add_event("j-1", {"type": "progress", "iteration": 1})

    assert store.count() == 2
    assert [r.id for r in store.list_jobs()] == ["j-2", "j-1"]  # newest first
    assert [r.id for r in store.list_jobs(limit=1, offset=1)] == ["j-1"]
    assert store.unfinished_ids() == ["j-1"]

    loaded = store.get("j-1")
    assert loaded is not None
    assert [e["type"] for e in loaded.events] == ["status", "progress"]
    assert store.get("j-nope") is None

    # put() updates metadata without disturbing the event log.
    loaded.status = "done"
    store.put(loaded)
    assert store.get("j-1").status == "done"
    assert len(store.get("j-1").events) == 2

    assert store.purge_before(datetime.now(UTC) - timedelta(days=1)) == ["j-1"]
    assert store.count() == 1
    assert store.get("j-1") is None


@pytest.mark.parametrize("store_factory", ["memory", "sqlite"])
def test_stores_agree_on_partial_loads(tmp_path, store_factory):
    """`with_result` / `with_events` mean the same thing on both backends.

    They exist because the expensive parts of a record are the parts most callers do not
    want. The SQLite store genuinely skips a file read and a query; the memory store has
    nothing to skip but must still honour the contract, or code that works against one
    backend quietly does the wrong thing against the other.
    """
    store = (
        MemoryJobStore()
        if store_factory == "memory"
        else SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    )
    payload = SolverResult(kind="grid2d", data={"fields": {"psi": [1.0, 2.0]}}, stats={"cells": 2})
    store.put(JobRecord(id="j-1", solver="s", status="done", result=payload))
    store.add_event("j-1", {"type": "status", "status": "done"})

    full = store.get("j-1")
    assert full.result is not None and full.events

    assert store.get("j-1", with_result=False).result is None
    assert store.get("j-1", with_result=False).events, "events dropped with the payload"
    assert store.get("j-1", with_events=False).events == []
    assert store.get("j-1", with_events=False).result is not None, "payload dropped with events"

    # A history page carries neither: it renders six metadata fields per row.
    listed = store.list_jobs()[0]
    assert listed.result is None and listed.events == []

    # …and it is a copy too. `Job.from_record` hands `artifacts` straight to a live `Job`,
    # so a listing that aliased it would let a caller rewrite the stored record.
    listed.artifacts.append({"name": "bogus"})
    assert store.get("j-1").artifacts == [], "listing aliased the stored artifact list"

    # A partial record must be safe to mutate — it must not alias what the store holds.
    partial = store.get("j-1", with_result=False)
    partial.status = "cancelled"
    partial.events.append({"type": "bogus"})
    assert store.get("j-1").status == "done", "mutating a partial record rewrote the store"
    assert len(store.get("j-1").events) == 1


def test_shutdown_releases_the_backend_and_the_bus(tmp_path):
    """Shutdown closed the store and nothing else, leaving two resources behind.

    It matters most for the worker deployment, where the backend and the bus each hold a
    Redis connection pool. But it bites the default too: the in-process backend owns a
    `ThreadPoolExecutor`, and `concurrent.futures` installs an atexit hook that joins its
    threads, so an abandoned executor makes a process with a long solve in flight refuse
    to exit.
    """
    closed = []

    class SpyBus(InProcessEventBus):
        async def close(self):
            closed.append("bus")

    class SpyBackend(InProcessBackend):
        async def close(self):
            closed.append("backend")
            await super().close()

    store = MemoryJobStore()
    bus = SpyBus()
    app = create_app()
    app.state.jobs = JobManager(
        data_dir=tmp_path,
        store=store,
        bus=bus,
        backend=SpyBackend(store, bus, tmp_path, timeout=5, max_workers=1),
    )
    with TestClient(app):
        pass

    # Backend before bus: nothing new starts solving before delivery stops.
    assert closed == ["backend", "bus"]
    assert app.state.jobs.backend._executor._shutdown, "thread pool left running"


def test_one_failing_close_does_not_skip_the_others(tmp_path):
    """The counter-case: a backend that throws must not strand the bus and the store."""
    closed = []

    class ExplodingBackend(InProcessBackend):
        async def close(self):
            raise RuntimeError("redis went away")

    class SpyBus(InProcessEventBus):
        async def close(self):
            closed.append("bus")

    class SpyStore(MemoryJobStore):
        def close(self):
            closed.append("store")

    store = SpyStore()
    bus = SpyBus()
    app = create_app()
    app.state.jobs = JobManager(
        data_dir=tmp_path,
        store=store,
        bus=bus,
        backend=ExplodingBackend(store, bus, tmp_path, timeout=5, max_workers=1),
    )
    with TestClient(app):
        pass
    assert closed == ["bus", "store"]


def test_status_and_listing_never_read_a_result_payload(data_dir):
    """The point of the flags, asserted where it pays: the HTTP layer.

    Answering "is it done yet?" used to load and parse the whole result — 50 ms and a
    3 MB read for six small fields — and `GET /jobs` did it once per row, so a 50-job
    page read 150 MB. Counting payload reads rather than timing anything, because a
    latency assertion in a test suite is a flake.
    """
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        store = client.app.state.jobs.store

        reads = []
        original = store._result_path
        store._result_path = lambda jid: (reads.append(jid), original(jid))[1]

        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
        assert reads == [], f"status poll read the payload: {reads}"

        assert client.get("/api/v1/jobs").status_code == 200
        assert reads == [], f"listing read the payload: {reads}"

        # …and the endpoint that genuinely needs it still gets it.
        assert client.get(f"/api/v1/jobs/{job_id}/result").json()["data"]
        assert reads == [job_id]


def test_memory_store_forgets_across_restarts(tmp_path, monkeypatch):
    """The opt-out is real: FENIXSPOON_STORE=memory is the pre-M3 behaviour."""
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_STORE", "memory")
    with TestClient(create_app()) as client:
        job_id = run_job(client)
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
    with TestClient(create_app()) as client:
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404


def test_unknown_store_backend_is_refused_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_STORE", "postgres")
    with pytest.raises(ValueError, match="postgres"):
        create_app()
