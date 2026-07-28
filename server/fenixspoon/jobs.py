"""In-process job manager (roadmap M0).

Runs solves on a thread pool inside the API process and fans progress events out to
WebSocket subscribers, replaying history to late joiners. The public surface (submit /
get / cancel / subscribe) is deliberately small so an out-of-process backend (Celery,
arq) can replace it at M3 without touching the API layer.

Configuration (environment):

- ``FENIXSPOON_DATA_DIR`` — root directory for per-job artifact files
  (default: ``<system tmp>/fenixspoon-jobs``).
- ``FENIXSPOON_JOB_TIMEOUT`` — wall-clock seconds a solve may run (default 600; 0 disables).
  Timeouts are cooperative: the worker thread is asked to stop via the cancel event, and
  the job is failed immediately from the caller's point of view.
"""

import asyncio
import os
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .geometry import Domain2D
from .solvers.base import JobCancelled, ProgressEvent, Solver, SolverContext, SolverResult

TERMINAL = ("done", "failed", "cancelled")


def _default_data_dir() -> Path:
    env = os.environ.get("FENIXSPOON_DATA_DIR")
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "fenixspoon-jobs"


def _default_timeout() -> float:
    return float(os.environ.get("FENIXSPOON_JOB_TIMEOUT", "600"))


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
    def __init__(self, data_dir: Path | None = None, job_timeout: float | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._data_dir = data_dir if data_dir is not None else _default_data_dir()
        self._timeout = job_timeout if job_timeout is not None else _default_timeout()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def submit(
        self, solver_cls: type[Solver], geometry: Domain2D, params: BaseModel
    ) -> Job:
        job = Job(id=f"j-{uuid.uuid4().hex[:12]}", solver_name=solver_cls.name)
        job.artifact_dir = self._data_dir / job.id
        self._jobs[job.id] = job
        asyncio.create_task(self._run(job, solver_cls, geometry, params))
        return job

    def cancel(self, job: Job) -> bool:
        """Request cooperative cancellation. Returns False if the job already ended."""
        if job.status in TERMINAL:
            return False
        job.cancel_event.set()
        return True

    async def _run(
        self, job: Job, solver_cls: type[Solver], geometry: Domain2D, params: BaseModel
    ) -> None:
        loop = asyncio.get_running_loop()
        job.status = "running"
        self._publish(job, {"type": "status", "status": "running"})

        def on_progress(event: ProgressEvent) -> None:
            # Called from the worker thread; hop onto the event loop to publish.
            loop.call_soon_threadsafe(self._publish, job, event.model_dump())

        ctx = SolverContext(
            progress_cb=on_progress,
            cancel_event=job.cancel_event,
            artifact_dir=job.artifact_dir,
        )
        try:
            solver = solver_cls()
            work = loop.run_in_executor(None, solver.solve, geometry, params, ctx)
            job.result = await asyncio.wait_for(work, self._timeout or None)
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
        self._publish(job, {"type": "status", "status": job.status, "error": job.error})

    def _publish(self, job: Job, event: dict[str, Any]) -> None:
        job.events.append(event)
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
