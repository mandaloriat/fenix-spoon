"""The distributed path: Redis pub/sub, the arq queue, and the worker's own logic (#12).

Skipped unless a Redis is reachable, which CI has no reason to run — so these are the
tests you run when you touch the worker path:

    redis-server --port 6399 --save '' --daemonize yes
    FENIXSPOON_TEST_REDIS_URL=redis://127.0.0.1:6399 pytest -m redis

``test_worker_solves_a_job_end_to_end`` is the important one: it calls the worker's own
entry point, so what it exercises is the code a real worker container runs, not a
re-implementation of it.
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.redis

REDIS_URL = os.environ.get("FENIXSPOON_TEST_REDIS_URL")
if not REDIS_URL:
    pytest.skip("set FENIXSPOON_TEST_REDIS_URL to run the Redis tests", allow_module_level=True)

pytest.importorskip("redis", reason="requires redis-py")
pytest.importorskip("arq", reason="requires arq")

from fenixspoon.backends import ArqBackend, cancel_key  # noqa: E402
from fenixspoon.geometry import Domain2D, Polygon2D  # noqa: E402
from fenixspoon.redis_bus import RedisEventBus  # noqa: E402
from fenixspoon.solvers.mock_laplace import MockLaplace2D  # noqa: E402
from fenixspoon.store import JobRecord, SqliteJobStore  # noqa: E402

GEOMETRY = Domain2D(
    bounds=(-1.0, -1.0, 2.0, 1.0),
    obstacle=Polygon2D(points=[(0.0, 0.0), (0.35, 0.09), (1.0, 0.0), (0.35, -0.06)]),
)
FAST = MockLaplace2D.Params(resolution=32, iterations=100, report_every=20)


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL)
    await client.flushall()
    yield client
    await client.aclose()


@pytest.mark.anyio
async def test_bus_delivers_across_connections(redis_client):
    """Publisher and subscriber are separate connections — the worker/API split."""
    del redis_client
    publisher = RedisEventBus(REDIS_URL)
    subscriber = RedisEventBus(REDIS_URL)
    received = []
    try:
        async with subscriber.subscribe("j-1") as feed:

            async def consume():
                async for seq, event in feed:
                    received.append((seq, event))
                    if event.get("status") == "done":
                        return

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.2)  # let SUBSCRIBE land before publishing
            await publisher.publish("j-1", 1, {"type": "progress", "iteration": 7})
            await publisher.publish("j-other", 1, {"type": "progress", "iteration": 999})
            await publisher.publish("j-1", 2, {"type": "status", "status": "done"})
            await asyncio.wait_for(task, 5)
    finally:
        await publisher.close()
        await subscriber.close()

    assert received == [
        (1, {"type": "progress", "iteration": 7}),
        (2, {"type": "status", "status": "done"}),
    ]  # and nothing from j-other


@pytest.mark.anyio
async def test_a_malformed_message_does_not_kill_the_stream(redis_client):
    """Some other process writing junk on the channel must not end a live subscription."""
    bus = RedisEventBus(REDIS_URL)
    received = []
    try:
        async with bus.subscribe("j-1") as feed:

            async def consume():
                async for seq, event in feed:
                    received.append((seq, event))
                    return

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.2)
            await redis_client.publish("fenixspoon:events:j-1", b"not json at all")
            await bus.publish("j-1", 4, {"type": "status", "status": "done"})
            await asyncio.wait_for(task, 5)
    finally:
        await bus.close()
    assert received == [(4, {"type": "status", "status": "done"})]


@pytest.mark.anyio
async def test_submitting_enqueues_and_cancelling_sets_the_flag(tmp_path, redis_client):
    backend = ArqBackend(REDIS_URL)
    record = JobRecord(id="j-queued", solver="mock.laplace2d", status="queued")
    try:
        await backend.start(record, MockLaplace2D, GEOMETRY, FAST)
        # arq records the job under its own key; the worker picks it off the queue.
        assert await redis_client.exists("arq:job:j-queued")

        assert not await redis_client.exists(cancel_key("j-queued"))
        await backend.cancel("j-queued")
        assert await redis_client.exists(cancel_key("j-queued"))
    finally:
        await backend.close()
    del tmp_path


@pytest.mark.anyio
async def test_worker_solves_a_job_end_to_end(tmp_path, redis_client):
    """The worker's own entry point, against a real store and a real Redis."""
    from fenixspoon.worker import solve_job

    store = SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    bus = RedisEventBus(REDIS_URL)
    store.put(JobRecord(id="j-work", solver="mock.laplace2d", status="queued"))

    ctx = {"store": store, "bus": bus, "data_dir": tmp_path, "redis": redis_client}
    try:
        status = await solve_job(
            ctx,
            "j-work",
            "mock.laplace2d",
            GEOMETRY.model_dump(mode="json"),
            FAST.model_dump(),
        )
    finally:
        await bus.close()

    assert status == "done"
    record = store.get("j-work")
    assert record.status == "done"
    assert record.result.kind == "grid2d"
    assert record.result.stats["cells"] > 0
    assert [a["name"] for a in record.artifacts] == ["solution.vtk"]
    # The event log is what a late subscriber replays, so it must be complete and ordered.
    assert record.events[0] == {"type": "status", "status": "running"}
    assert record.events[-1]["status"] == "done"
    assert any(e["type"] == "progress" for e in record.events)
    store.close()


@pytest.mark.anyio
async def test_worker_fails_a_job_whose_solver_it_does_not_have(tmp_path, redis_client):
    """A worker image that lags the API must fail loudly, not hang the job as running."""
    from fenixspoon.worker import solve_job

    store = SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    bus = RedisEventBus(REDIS_URL)
    store.put(JobRecord(id="j-missing", solver="physics.from_the_future", status="queued"))
    ctx = {"store": store, "bus": bus, "data_dir": tmp_path, "redis": redis_client}
    try:
        status = await solve_job(ctx, "j-missing", "physics.from_the_future", {}, {})
    finally:
        await bus.close()

    assert status == "failed"
    record = store.get("j-missing")
    assert "no solver named" in record.error
    assert record.events[-1]["status"] == "failed"
    store.close()


@pytest.mark.anyio
async def test_worker_stops_when_the_cancel_flag_appears(tmp_path, redis_client):
    from fenixspoon.worker import solve_job

    store = SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    bus = RedisEventBus(REDIS_URL)
    store.put(JobRecord(id="j-cancel", solver="mock.laplace2d", status="queued"))
    await redis_client.set(cancel_key("j-cancel"), b"1")  # already cancelled at pickup

    ctx = {"store": store, "bus": bus, "data_dir": tmp_path, "redis": redis_client}
    try:
        slow = MockLaplace2D.Params(resolution=256, iterations=20000, report_every=100)
        status = await solve_job(
            ctx, "j-cancel", "mock.laplace2d", GEOMETRY.model_dump(mode="json"), slow.model_dump()
        )
    finally:
        await bus.close()

    assert status == "cancelled"
    assert store.get("j-cancel").status == "cancelled"
    store.close()
