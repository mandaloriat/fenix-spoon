"""Event delivery and replay (#12).

The invariant worth defending: **what a subscriber sees equals the job's stored event
log, exactly once each, whenever it attaches.** Late subscribers were already covered;
what was not is attaching *while* the job is running, which is when a replay-then-listen
implementation drops or duplicates events. It is also the case that gets harder, not
easier, once the publisher is a different process.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from fenixspoon.events import InProcessEventBus
from fenixspoon.main import create_app

GEOMETRY = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
CHATTY = {"resolution": 48, "iterations": 3000, "report_every": 5}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    with TestClient(create_app()) as c:
        yield c


def drain(ws):
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event.get("type") == "status" and event["status"] in (
            "done",
            "failed",
            "cancelled",
        ):
            return events


def test_subscribing_mid_flight_sees_the_whole_log(client):
    """End to end: attach after the solve has started, get everything anyway."""
    job_id = client.post(
        "/api/v1/jobs",
        json={"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": CHATTY},
    ).json()["job_id"]

    time.sleep(0.05)  # let some events accumulate before attaching
    with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as ws:
        seen = drain(ws)

    stored = client.app.state.jobs.store.get(job_id).events
    assert seen == stored, "subscriber stream must equal the stored log"
    iterations = [e["iteration"] for e in seen if e["type"] == "progress"]
    assert iterations == sorted(iterations), "events must arrive in order"


@pytest.mark.anyio
async def test_replay_and_live_feed_are_reconciled_by_sequence(tmp_path):
    """The overlap case, made deterministic.

    A subscriber attaches, then reads stored history; anything published in between
    arrives on *both* paths. The real race window is sub-millisecond and cannot be hit
    reliably from a test, so this drives the bus by hand: re-publish seqs the store
    already had, plus a new one, and require each event exactly once and in order.
    """
    from fenixspoon.jobs import Job, JobManager
    from fenixspoon.store import MemoryJobStore

    manager = JobManager(data_dir=tmp_path, store=MemoryJobStore(), bus=InProcessEventBus())
    job = Job(id="j-1", solver_name="mock.laplace2d", status="running")
    manager._jobs[job.id] = job
    manager.store.put(job.to_record())
    for iteration in (1, 2, 3):
        manager.store.add_event(job.id, {"type": "progress", "iteration": iteration})

    seen: list[dict] = []

    async def consume():
        async for event in manager.subscribe(job):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let it attach and replay the three stored events

    # Seqs 2 and 3 are re-deliveries of history the subscriber already replayed; 4 is new.
    await manager.bus.publish(job.id, 2, {"type": "progress", "iteration": 2})
    await manager.bus.publish(job.id, 3, {"type": "progress", "iteration": 3})
    await manager.bus.publish(job.id, 4, {"type": "status", "status": "done"})
    await asyncio.wait_for(task, 2)

    assert [e["iteration"] for e in seen if e["type"] == "progress"] == [1, 2, 3]
    assert seen[-1] == {"type": "status", "status": "done"}


def test_two_subscribers_on_one_job_each_get_everything(client):
    job_id = client.post(
        "/api/v1/jobs",
        json={"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": CHATTY},
    ).json()["job_id"]
    with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as first:
        with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as second:
            a, b = drain(first), drain(second)
    assert a == b
    assert a[-1]["status"] == "done"


@pytest.mark.anyio
async def test_bus_delivers_to_current_subscribers_only():
    bus = InProcessEventBus()
    async with bus.subscribe("j-1") as feed:
        await bus.publish("j-1", 1, {"type": "progress", "iteration": 1})
        await bus.publish("j-2", 1, {"type": "progress", "iteration": 99})  # other job
        iterator = feed.__aiter__()
        assert await asyncio.wait_for(iterator.__anext__(), 1) == (
            1,
            {"type": "progress", "iteration": 1},
        )

    # After the subscription closes, publishing must not raise or leak a queue.
    await bus.publish("j-1", 2, {"type": "progress", "iteration": 2})
    assert bus._subscribers == {}


@pytest.fixture()
def anyio_backend():
    return "asyncio"
