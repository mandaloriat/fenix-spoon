"""Job manager (roadmap M0, persistence added in M3).

Runs solves on a thread pool inside the API process and fans progress events out to
WebSocket subscribers, replaying history to late joiners. The public surface (submit /
get / cancel / subscribe / list) is deliberately small so an out-of-process backend
(Celery, arq) can replace it at M3 without touching the API layer.

Live state — subscriber queues, the cancel event, the running future — stays in memory
because none of it is serializable. Everything a client can still ask for after the
process is gone goes to a :class:`~fenixspoon.store.JobStore`: metadata, the event log,
and the result payload. A job the manager has never heard of is loaded back from the
store on demand, so restarting the API does not lose history.

Configuration (environment):

- ``FENIXSPOON_DATA_DIR`` — root directory for per-job artifact files and, for the
  SQLite store, the database (default: ``<system tmp>/fenixspoon-jobs``).
- ``FENIXSPOON_STORE`` — ``sqlite`` (default) or ``memory``. ``memory`` keeps the
  pre-M3 behaviour: history dies with the process.
- ``FENIXSPOON_JOB_TIMEOUT`` — wall-clock seconds a solve may run (default 600; 0 disables).
  Timeouts are cooperative: the worker thread is asked to stop via the cancel event, and
  the job is failed immediately from the caller's point of view.
- ``FENIXSPOON_MAX_CELLS`` — cell budget a single job may ask for (default 2,000,000; 0
  disables). Enforced at submit time from the solver's own estimate, so an over-budget
  job is refused with a clear message instead of being killed mid-solve by the timeout.
- ``FENIXSPOON_JOB_TTL`` — seconds a finished job's record and artifacts are kept
  (default 604800 = 7 days; 0 keeps them forever).
"""

import asyncio
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .geometry import Domain2D
from .solvers.base import JobCancelled, ProgressEvent, Solver, SolverContext, SolverResult
from .store import JobRecord, JobStore, MemoryJobStore, SqliteJobStore

TERMINAL = ("done", "failed", "cancelled")

# How often the retention sweep runs while the server is up. Retention is measured in
# days, so an hourly sweep is prompt enough and costs one indexed query.
PURGE_INTERVAL_SECONDS = 3600.0


def _default_data_dir() -> Path:
    env = os.environ.get("FENIXSPOON_DATA_DIR")
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "fenixspoon-jobs"


def _default_timeout() -> float:
    return float(os.environ.get("FENIXSPOON_JOB_TIMEOUT", "600"))


def _default_max_cells() -> int:
    """Cell budget a single job may ask for. 0 disables the check."""
    return int(os.environ.get("FENIXSPOON_MAX_CELLS", "2000000"))


def _default_ttl() -> float:
    """How long a job's record and artifacts survive. 0 keeps them forever."""
    return float(os.environ.get("FENIXSPOON_JOB_TTL", str(7 * 24 * 3600)))


def _default_store(data_dir: Path) -> JobStore:
    kind = os.environ.get("FENIXSPOON_STORE", "sqlite").lower()
    if kind == "memory":
        return MemoryJobStore()
    if kind != "sqlite":
        raise ValueError(f"unknown FENIXSPOON_STORE {kind!r}: expected 'sqlite' or 'memory'")
    return SqliteJobStore(data_dir / "jobs.db", result_dir=data_dir)


@dataclass
class Job:
    id: str
    solver_name: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: SolverResult | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    artifact_dir: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_record(self) -> JobRecord:
        return JobRecord(
            id=self.id,
            solver=self.solver_name,
            status=self.status,
            error=self.error,
            created_at=self.created_at,
            finished_at=self.finished_at,
            result=self.result,
            artifacts=self.artifacts,
        )

    @classmethod
    def from_record(cls, record: JobRecord, artifact_dir: Path | None) -> "Job":
        """Rehydrate a stored job. It has no subscribers and nothing is solving it."""
        return cls(
            id=record.id,
            solver_name=record.solver,
            status=record.status,
            error=record.error,
            created_at=record.created_at,
            finished_at=record.finished_at,
            result=record.result,
            artifacts=record.artifacts,
            artifact_dir=artifact_dir,
            events=record.events,
        )


class JobStatus(BaseModel):
    job_id: str
    solver: str
    status: str
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_job(cls, job: Job) -> "JobStatus":
        return cls(
            job_id=job.id,
            solver=job.solver_name,
            status=job.status,
            error=job.error,
            created_at=job.created_at,
            finished_at=job.finished_at,
        )


class JobManager:
    def __init__(
        self,
        data_dir: Path | None = None,
        job_timeout: float | None = None,
        max_cells: int | None = None,
        store: JobStore | None = None,
        job_ttl: float | None = None,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._data_dir = data_dir if data_dir is not None else _default_data_dir()
        self._timeout = job_timeout if job_timeout is not None else _default_timeout()
        self.max_cells = max_cells if max_cells is not None else _default_max_cells()
        self.job_ttl = job_ttl if job_ttl is not None else _default_ttl()
        self.store = store if store is not None else _default_store(self._data_dir)

    def get(self, job_id: str) -> Job | None:
        """A live job if this process owns one, otherwise whatever the store remembers."""
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        record = self.store.get(job_id)
        if record is None:
            return None
        return Job.from_record(record, self._data_dir / record.id)

    def list_jobs(self, limit: int = 50, offset: int = 0) -> tuple[list[Job], int]:
        """Newest-first page of job history, plus the total for pagination."""
        records = self.store.list_jobs(limit=limit, offset=offset)
        jobs = []
        for record in records:
            # A live job is authoritative: its status may have moved on since the last
            # write, and it carries the in-memory result.
            live = self._jobs.get(record.id)
            jobs.append(live or Job.from_record(record, self._data_dir / record.id))
        return jobs, self.store.count()

    async def submit(
        self, solver_cls: type[Solver], geometry: Domain2D, params: BaseModel
    ) -> Job:
        job = Job(id=f"j-{uuid.uuid4().hex[:12]}", solver_name=solver_cls.name)
        job.artifact_dir = self._data_dir / job.id
        self._jobs[job.id] = job
        self.store.put(job.to_record())
        asyncio.create_task(self._run(job, solver_cls, geometry, params))
        return job

    def cancel(self, job: Job) -> bool:
        """Request cooperative cancellation. Returns False if the job already ended."""
        if job.status in TERMINAL:
            return False
        job.cancel_event.set()
        return True

    def reconcile(self) -> list[str]:
        """Fail jobs the store left running: after a restart, nobody is solving them.

        Without this a client polling a job from a previous process lifetime waits for a
        terminal status that can never arrive.
        """
        stranded = []
        for job_id in self.store.unfinished_ids():
            if job_id in self._jobs:
                continue
            record = self.store.get(job_id)
            if record is None:
                continue
            record.status = "failed"
            record.error = "server restarted while this job was running"
            record.finished_at = datetime.now(UTC)
            self.store.put(record)
            self.store.add_event(
                job_id, {"type": "status", "status": "failed", "error": record.error}
            )
            stranded.append(job_id)
        return stranded

    def purge_expired(self, now: datetime | None = None) -> list[str]:
        """Drop jobs past the TTL, records and artifact directories together."""
        if not self.job_ttl:
            return []
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=self.job_ttl)
        purged = self.store.purge_before(cutoff)
        for job_id in purged:
            self._jobs.pop(job_id, None)
            shutil.rmtree(self._data_dir / job_id, ignore_errors=True)
        return purged

    async def _run(
        self, job: Job, solver_cls: type[Solver], geometry: Domain2D, params: BaseModel
    ) -> None:
        loop = asyncio.get_running_loop()
        job.status = "running"
        self._publish(job, {"type": "status", "status": "running"})
        self.store.put(job.to_record())

        def on_progress(event: ProgressEvent) -> None:
            # Called from the worker thread; hop onto the event loop to publish.
            loop.call_soon_threadsafe(self._publish, job, event.model_dump())

        ctx = SolverContext(
            progress_cb=on_progress,
            cancel_event=job.cancel_event,
            artifact_dir=job.artifact_dir,
        )
        started = time.monotonic()
        try:
            solver = solver_cls()
            work = loop.run_in_executor(None, solver.solve, geometry, params, ctx)
            job.result = await asyncio.wait_for(work, self._timeout or None)
            job.result.stats = {
                **job.result.stats,
                "seconds": round(time.monotonic() - started, 4),
            }
            job.artifacts = ctx.artifacts
            job.status = "done"
        except TimeoutError:
            # The worker thread cannot be killed; ask it to stop and fail the job now.
            job.cancel_event.set()
            job.status = "failed"
            job.error = f"job exceeded the wall-clock timeout ({self._timeout:g}s)"
        except JobCancelled:
            job.status = "cancelled"
        except Exception as exc:  # solver bugs must fail the job, not the server
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        job.finished_at = datetime.now(UTC)
        self.store.put(job.to_record())
        self._publish(job, {"type": "status", "status": job.status, "error": job.error})

    def _publish(self, job: Job, event: dict[str, Any]) -> None:
        job.events.append(event)
        self.store.add_event(job.id, event)
        for queue in job.subscribers:
            queue.put_nowait(event)

    async def subscribe(self, job: Job) -> AsyncIterator[dict[str, Any]]:
        """Yield all past events, then live events, until a terminal status event."""
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.add(queue)
        try:
            # No await between registering and snapshotting, so nothing is missed or duplicated.
            replay = list(job.events)
            for event in replay:
                yield event
                if _is_terminal(event):
                    return
            while True:
                event = await queue.get()
                yield event
                if _is_terminal(event):
                    return
        finally:
            job.subscribers.discard(queue)


def _is_terminal(event: dict[str, Any]) -> bool:
    return event.get("type") == "status" and event.get("status") in TERMINAL
