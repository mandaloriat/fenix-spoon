"""Where a solve actually runs (roadmap M3, issue #12).

Two implementations of one small interface:

- :class:`InProcessBackend` — a bounded thread pool inside the API process. Zero
  infrastructure, and the default, because the loop this project exists to enable is
  clone, run, open a browser.
- :class:`ArqBackend` — hands the job to a Redis queue that worker containers drain.
  The API process then does no solving at all, which is what lets the workers carry
  dolfinx while the API image stays small, and what makes a per-job memory limit or a
  hard kill possible at last.

**arq rather than Celery.** Celery is the bigger ecosystem, but its shape fits badly
here: it is synchronous-first in an application that is asyncio throughout, it wants to
own job state and results that :mod:`fenixspoon.store` already owns, and it arrives with
a dependency tree larger than the rest of this server put together. arq is a few hundred
lines over ``redis.asyncio``, natively async, and used here purely as a dispatcher —
``max_tries=1``, results ignored — so there is exactly one source of truth for what a job
is doing. A deployment that must run Celery can implement this same interface against it.
"""

import asyncio
import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from .events import EventBus
from .execution import EventSink, run_solve
from .geometry import Geometry
from .solvers.base import Solver
from .store import JobRecord, JobStore

log = logging.getLogger(__name__)

CANCEL_PREFIX = "fenixspoon:cancel:"
QUEUE_NAME = "fenixspoon:queue"


def cancel_key(job_id: str) -> str:
    return f"{CANCEL_PREFIX}{job_id}"


class ExecutionBackend(ABC):
    """How the manager gets a solve run somewhere."""

    #: True when solves run inside this process. Two things follow from it, both for
    #: the same reason — that a job running elsewhere is not this process's to speak
    #: for. The manager only caches a live Job when it runs here (otherwise the store
    #: is the only truth), and only reconciles stranded jobs on startup when it does
    #: (otherwise it would fail work the workers are still doing).
    runs_locally = True

    @abstractmethod
    async def start(
        self,
        record: JobRecord,
        solver_cls: type[Solver],
        geometry: Geometry,
        params: BaseModel,
        on_finish=None,
    ) -> None:
        """Begin executing a job that has already been persisted as ``queued``."""

    @abstractmethod
    async def cancel(self, job_id: str) -> None:
        """Ask the running solve to stop. Cooperative and best-effort in both backends."""

    @abstractmethod
    def active_ids(self) -> set[str]:
        """Jobs this process is executing right now (empty for a remote backend)."""

    async def close(self) -> None:
        return None


class InProcessBackend(ExecutionBackend):
    """Solves on a bounded thread pool in the API process."""

    runs_locally = True

    def __init__(
        self,
        store: JobStore,
        bus: EventBus,
        data_dir: Path,
        timeout: float,
        max_workers: int,
    ) -> None:
        self._store = store
        self._bus = bus
        self._data_dir = data_dir
        self._timeout = timeout
        # Its own pool, not asyncio's default executor: solves must not compete for
        # threads with everything else the server hands off (static files, artifact
        # reads), and a bounded pool is what keeps an unbounded submit rate from
        # becoming unbounded concurrent solves.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="fenixspoon-solve"
        )
        self._cancels: dict[str, threading.Event] = {}
        self._tasks: set[asyncio.Task] = set()

    async def start(self, record, solver_cls, geometry, params, on_finish=None) -> None:
        cancel_event = threading.Event()
        self._cancels[record.id] = cancel_event
        sink = EventSink(self._store, self._bus, record.id, mirror=record.events)

        async def drive() -> None:
            try:
                finished = await run_solve(
                    record,
                    solver_cls,
                    geometry,
                    params,
                    store=self._store,
                    sink=sink,
                    artifact_dir=self._data_dir / record.id,
                    executor=self._executor,
                    timeout=self._timeout,
                    cancel_event=cancel_event,
                )
                if on_finish is not None:
                    on_finish(finished)
            finally:
                self._cancels.pop(record.id, None)

        task = asyncio.create_task(drive())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel(self, job_id: str) -> None:
        event = self._cancels.get(job_id)
        if event is not None:
            event.set()

    def active_ids(self) -> set[str]:
        return set(self._cancels)

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class ArqBackend(ExecutionBackend):
    """Enqueues to Redis; worker containers do the solving.

    Jobs survive an API restart here — they are running somewhere else — so startup
    reconciliation must not touch them. What can still strand a job is a worker dying
    mid-solve, which nothing in the queue notices; the manager's stale sweep is the
    backstop for that.
    """

    runs_locally = False

    def __init__(self, redis_url: str, pool=None) -> None:
        self.redis_url = redis_url
        self._pool = pool

    async def _ensure_pool(self):
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._pool = await create_pool(RedisSettings.from_dsn(self.redis_url))
        return self._pool

    async def start(self, record, solver_cls, geometry, params, on_finish=None) -> None:
        del on_finish  # nothing to hand back: the outcome lands in the shared store
        pool = await self._ensure_pool()
        # Send the geometry and params as JSON rather than pickled models: the worker
        # revalidates them against the solver's own schema, so a version skew between
        # API and worker fails loudly at validation instead of unpickling into nonsense.
        await pool.enqueue_job(
            "solve_job",
            record.id,
            solver_cls.name,
            geometry.model_dump(mode="json"),
            json.loads(params.model_dump_json()),
            _job_id=record.id,
        )

    async def cancel(self, job_id: str) -> None:
        """Set a flag the worker polls.

        arq can abort a job, but only one it has not started; a solve already running in
        a worker thread has to be asked. The key expires so a cancelled-then-purged job
        cannot leave litter behind.
        """
        pool = await self._ensure_pool()
        await pool.set(cancel_key(job_id), b"1", ex=86400)

    def active_ids(self) -> set[str]:
        return set()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()


def _default_redis_url() -> str | None:
    return os.environ.get("FENIXSPOON_REDIS_URL") or None


def default_backend(
    store: JobStore, bus: EventBus, data_dir: Path, timeout: float, max_workers: int
) -> ExecutionBackend:
    """In-process unless ``FENIXSPOON_REDIS_URL`` says otherwise."""
    url = _default_redis_url()
    if url:
        log.info("dispatching jobs to arq workers via %s", url)
        return ArqBackend(url)
    return InProcessBackend(store, bus, data_dir, timeout, max_workers)
