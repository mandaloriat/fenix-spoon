"""The worker process (roadmap M3, issue #12).

Run one or many of these next to the API:

    arq fenixspoon.worker.WorkerSettings

They share two things with the API and nothing else: the job store (a mounted data
directory, or eventually a database) and Redis. The API never solves; the worker never
serves HTTP. That split is what lets the worker image carry dolfinx while the API image
stays small, and what makes it possible to give a solve a memory limit or kill it.

A worker validates the geometry, params and load case it receives against the solver's own
schema rather than trusting the payload. The API validated them too — but the two processes
are separately deployable and can drift, and a mismatch should fail loudly at validation
instead of reaching a solver as nonsense.
"""

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .backends import cancel_key
from .core.conditions import check_conditions
from .execution import EventSink, run_solve
from .geometry import Geometry
from .jobs import _default_data_dir, _default_store, _default_timeout
from .redis_bus import RedisEventBus
from .solvers import get_solver

log = logging.getLogger(__name__)

# How often a running solve checks whether someone asked it to stop. Cancellation is
# cooperative anyway — the solver only notices at its next check point — so polling
# faster than this buys nothing but Redis traffic.
CANCEL_POLL_SECONDS = 0.5

#: The geometry union is a discriminated type, so one adapter validates every kind.
_GEOMETRY = TypeAdapter(Geometry)


def redis_url() -> str:
    return os.environ.get("FENIXSPOON_REDIS_URL", "redis://localhost:6379")


async def _watch_for_cancel(client, job_id: str, event: threading.Event) -> None:
    """Turn a Redis flag into the threading.Event the solver already understands."""
    while not event.is_set():
        if await client.exists(cancel_key(job_id)):
            event.set()
            return
        await asyncio.sleep(CANCEL_POLL_SECONDS)


async def solve_job(
    ctx: dict[str, Any],
    job_id: str,
    solver_name: str,
    geometry_payload: dict,
    params_payload: dict,
    conditions_payload: dict | None = None,
) -> str:
    """Run one job. Returns its terminal status, mostly so arq's log says something useful."""
    store = ctx["store"]
    bus: RedisEventBus = ctx["bus"]
    data_dir: Path = ctx["data_dir"]

    record = store.get(job_id)
    if record is None:
        # Purged between enqueue and pickup: nothing to report to, so drop it.
        log.warning("job %s is not in the store; skipping", job_id)
        return "unknown"

    solver_cls = get_solver(solver_name)
    if solver_cls is None:
        # A worker image without the solver the API offered — a deployment mismatch, and
        # the job must fail with that reason rather than hang as `running` forever.
        record.status = "failed"
        record.error = f"this worker has no solver named {solver_name!r}"
        store.put(record)
        await EventSink(store, bus, job_id).emit(
            {"type": "status", "status": "failed", "error": record.error}
        )
        return "failed"

    geometry = _GEOMETRY.validate_python(geometry_payload)
    params = solver_cls.Params.model_validate(params_payload)
    # Re-checked here for the same reason the geometry and params are re-validated: the API
    # checked them, but the two processes are separately deployable and can drift, and a
    # load case naming a boundary this worker's geometry model does not know about should
    # fail loudly rather than reach an adapter that would apply it to nothing.
    conditions = {key: dict(value) for key, value in (conditions_payload or {}).items()}
    check_conditions(solver_cls, geometry, conditions)

    cancel_event = threading.Event()
    watcher = asyncio.create_task(_watch_for_cancel(ctx["redis"], job_id, cancel_event))
    try:
        finished = await run_solve(
            record,
            solver_cls,
            geometry,
            params,
            conditions=conditions,
            store=store,
            sink=EventSink(store, bus, job_id),
            artifact_dir=data_dir / job_id,
            executor=None,  # arq runs one job at a time per worker; the default pool is fine
            timeout=_default_timeout(),
            cancel_event=cancel_event,
        )
    finally:
        cancel_event.set()  # stop the watcher whatever happened
        await watcher
    return finished.status


async def startup(ctx: dict[str, Any]) -> None:
    import redis.asyncio as aioredis

    data_dir = _default_data_dir()
    ctx["data_dir"] = data_dir
    ctx["store"] = _default_store(data_dir)
    ctx["bus"] = RedisEventBus(redis_url())
    ctx["redis"] = aioredis.from_url(redis_url())
    log.info("worker ready: data_dir=%s redis=%s", data_dir, redis_url())


async def shutdown(ctx: dict[str, Any]) -> None:
    # arq calls this even when startup failed part-way, so nothing here may assume the
    # key exists — otherwise a KeyError buries the real startup error.
    if "bus" in ctx:
        await ctx["bus"].close()
    if "redis" in ctx:
        await ctx["redis"].aclose()
    if "store" in ctx:
        ctx["store"].close()


class WorkerSettings:
    """arq entry point: ``arq fenixspoon.worker.WorkerSettings``."""

    functions = [solve_job]
    on_startup = startup
    on_shutdown = shutdown
    # One try. Retrying a solve that already wrote a terminal status to the store would
    # resurrect a finished job, and the store — not arq — is the source of truth here.
    max_tries = 1
    # arq's own timeout is the hard backstop behind the solver's cooperative one, so it
    # has to be the looser of the two or it fires first and bypasses the clean failure.
    job_timeout = _default_timeout() * 2 or None
    max_jobs = int(os.environ.get("FENIXSPOON_WORKER_CONCURRENCY", "1"))


def _redis_settings():
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(redis_url())


# arq reads this as a value, not a callable, so it is resolved at import — which is also
# when the worker process has its environment.
WorkerSettings.redis_settings = _redis_settings()
