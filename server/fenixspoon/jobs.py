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

import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .backends import ExecutionBackend, default_backend
from .events import EventBus, InProcessEventBus
from .geometry import Domain2D
from .solvers.base import Solver, SolverResult
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


def _default_workers() -> int:
    """How many solves may run at once.

    Defaults to the core count. Going wider does not finish work sooner — the box has
    the cores it has — and it does make the API slower to answer, because solves and the
    event loop share a GIL. Load testing (docs/06-load-test.md) shows submit latency
    climbing with over-subscription while throughput stays flat.
    """
    configured = os.environ.get("FENIXSPOON_MAX_WORKERS")
    if configured:
        return max(1, int(configured))
    return max(1, os.cpu_count() or 1)


def _default_bus() -> EventBus:
    """Redis when workers are configured, in-process otherwise.

    These two settings have to agree: a Redis backend publishes progress from another
    process, and an in-process bus in the API would never hear it. Deriving both from
    the same variable is what stops a deployment from being half-distributed — a
    misconfiguration whose only symptom is a progress bar that never moves.
    """
    url = os.environ.get("FENIXSPOON_REDIS_URL")
    if not url:
        return InProcessEventBus()
    from .redis_bus import RedisEventBus

    return RedisEventBus(url)


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
    owner: str = "anonymous"
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: SolverResult | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    artifact_dir: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_record(self) -> JobRecord:
        return JobRecord(
            id=self.id,
            solver=self.solver_name,
            status=self.status,
            owner=self.owner,
            error=self.error,
            created_at=self.created_at,
            finished_at=self.finished_at,
            result=self.result,
            artifacts=self.artifacts,
            inputs=self.inputs,
        )

    def absorb(self, record: JobRecord) -> None:
        """Copy an execution outcome back onto this live cache entry."""
        self.status = record.status
        self.error = record.error
        self.finished_at = record.finished_at
        self.result = record.result
        self.artifacts = record.artifacts

    @classmethod
    def from_record(cls, record: JobRecord, artifact_dir: Path | None) -> "Job":
        """Rehydrate a stored job. It has no subscribers and nothing is solving it."""
        return cls(
            id=record.id,
            solver_name=record.solver,
            status=record.status,
            owner=record.owner,
            error=record.error,
            created_at=record.created_at,
            finished_at=record.finished_at,
            result=record.result,
            artifacts=record.artifacts,
            artifact_dir=artifact_dir,
            events=record.events,
            inputs=record.inputs,
        )


class JobStatus(BaseModel):
    """A job's current state, as `GET /api/v1/jobs/{id}` returns it."""

    job_id: str = Field(description="Identifier assigned at submit.")
    solver: str = Field(description="Which solver is running it.")
    status: str = Field(
        description="`queued`, `running`, or the terminal `done` / `failed` / `cancelled`."
    )
    error: str | None = Field(description="Why it failed. Null unless `status` is `failed`.")
    created_at: datetime = Field(description="When the job was accepted (RFC 3339, UTC).")
    finished_at: datetime | None = Field(
        description="When it reached a terminal status; null while it is still running."
    )

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
        max_workers: int | None = None,
        bus: EventBus | None = None,
        backend: ExecutionBackend | None = None,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._data_dir = data_dir if data_dir is not None else _default_data_dir()
        self._timeout = job_timeout if job_timeout is not None else _default_timeout()
        self.max_cells = max_cells if max_cells is not None else _default_max_cells()
        self.job_ttl = job_ttl if job_ttl is not None else _default_ttl()
        self.store = store if store is not None else _default_store(self._data_dir)
        self.bus = bus if bus is not None else _default_bus()
        self.max_workers = max_workers if max_workers is not None else _default_workers()
        self.backend = backend if backend is not None else default_backend(
            self.store, self.bus, self._data_dir, self._timeout, self.max_workers
        )

    @property
    def data_dir(self) -> Path:
        """Where jobs, results and artifacts live. Read-only, and reported by
        `environment.inspect` (#43) — a local caller needs to know where its files land,
        and an operator needs to know what to back up."""
        return self._data_dir

    @property
    def job_timeout(self) -> float:
        """Wall-clock seconds a solve may run; 0 when disabled. Read-only, see #43."""
        return self._timeout

    def get(
        self, job_id: str, owner: str | None = None, *, with_result: bool = False
    ) -> Job | None:
        """A live job if this process owns one, otherwise whatever the store remembers.

        ``owner`` scopes the lookup to one principal: a job belonging to somebody else
        comes back as None, so the caller answers 404. Not 403 — telling a stranger that
        a job id exists is itself a leak, and the ids are only 48 bits of randomness.

        ``with_result`` defaults to False because almost nothing needs the payload: a
        status poll, a cancel and an artifact download all want metadata, and loading a
        multi-megabyte result to answer them costs more than the answer. Only
        ``/jobs/{id}/result`` asks for it.
        """
        job = self._jobs.get(job_id)
        if job is None:
            record = self.store.get(job_id, with_result=with_result, with_events=False)
            job = Job.from_record(record, self._data_dir / record.id) if record else None
        if job is None or (owner is not None and job.owner != owner):
            return None
        return job

    def list_jobs(
        self, limit: int = 50, offset: int = 0, owner: str | None = None
    ) -> tuple[list[Job], int]:
        """Newest-first page of job history, plus the total for pagination."""
        records = self.store.list_jobs(limit=limit, offset=offset, owner=owner)
        jobs = []
        for record in records:
            # A live job is authoritative: its status may have moved on since the last
            # write, and it carries the in-memory result.
            live = self._jobs.get(record.id)
            jobs.append(live or Job.from_record(record, self._data_dir / record.id))
        return jobs, self.store.count(owner=owner)

    async def submit(
        self,
        solver_cls: type[Solver],
        geometry: Domain2D,
        params: BaseModel,
        owner: str = "anonymous",
        inputs: dict[str, Any] | None = None,
    ) -> Job:
        job = Job(
            id=f"j-{uuid.uuid4().hex[:12]}",
            solver_name=solver_cls.name,
            owner=owner,
            inputs=inputs or {},
        )
        job.artifact_dir = self._data_dir / job.id
        record = job.to_record()
        self.store.put(record)
        on_finish = job.absorb
        if self.backend.runs_locally:
            # Cache it only when this process is the one running it. A remote backend
            # never calls back, so a cached Job would stay `queued` forever while the
            # store — the only thing both processes share — said `done`.
            self._jobs[job.id] = job
            on_finish = self._retire(job)
        await self.backend.start(record, solver_cls, geometry, params, on_finish=on_finish)
        return job

    def _retire(self, job: Job):
        """Copy the outcome onto the live entry, then drop it from the cache.

        The cache exists to answer for a job *while it is being solved* — a status that
        has moved on since the last write, and the cancel handle. Once the job is
        terminal it answers for nothing the store cannot, and it holds the one thing this
        design went out of its way to keep out of memory: the full result payload. A
        512×341 grid is ~3 MB of JSON, so keeping finished jobs for the retention window
        (7 days by default) is an unbounded leak measured in gigabytes — the process runs
        out of memory long before the TTL sweep would have freed anything.

        Safe because :func:`~fenixspoon.execution.run_solve` persists the terminal record
        *before* returning, so there is no window in which the entry is gone and the store
        still says `running`. ``get()`` rehydrates from the store, reading `result.json`
        back off disk on demand.
        """

        def finish(record: JobRecord) -> None:
            job.absorb(record)
            self._jobs.pop(job.id, None)

        return finish

    async def cancel(self, job: Job) -> bool:
        """Request cooperative cancellation. Returns False if the job already ended.

        Cooperative in both backends: in-process it sets an event the solver checks, and
        with workers it sets a Redis flag the worker polls. Neither kills anything.
        """
        if job.status in TERMINAL:
            return False
        await self.backend.cancel(job.id)
        return True

    def reconcile(self) -> list[str]:
        """Fail jobs the store left running: after a restart, nobody is solving them.

        Without this a client polling a job from a previous process lifetime waits for a
        terminal status that can never arrive.
        """
        if not self.backend.runs_locally:
            # Workers kept solving while the API was down; failing their jobs here would
            # be the API lying about work that is still happening.
            return []
        stranded = []
        active = self.backend.active_ids()
        for job_id in self.store.unfinished_ids():
            if job_id in active:
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

    async def subscribe(self, job: Job) -> AsyncIterator[dict[str, Any]]:
        """Yield all past events, then live ones, until a terminal status event.

        Order matters: attach to the live feed *first*, then read the stored history.
        The reverse loses any event published in between. Overlap is then removed by
        sequence number rather than by counting, which is what keeps this correct when
        the publisher is another process.
        """
        async with self.bus.subscribe(job.id) as live:
            replayed = 0
            record = self.store.get(job.id, with_result=False)
            history = record.events if record is not None else list(job.events)
            for event in history:
                replayed += 1
                yield event
                if _is_terminal(event):
                    return
            async for seq, event in live:
                if seq <= replayed:
                    continue  # already delivered from the stored history
                replayed = seq
                yield event
                if _is_terminal(event):
                    return


def _is_terminal(event: dict[str, Any]) -> bool:
    return event.get("type") == "status" and event.get("status") in TERMINAL
