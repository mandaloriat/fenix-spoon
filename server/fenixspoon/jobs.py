"""In-process job manager (roadmap M0).

Runs solves on a thread pool inside the API process and fans progress events out to
WebSocket subscribers, replaying history to late joiners. The public surface (submit /
get / subscribe) is deliberately small so an out-of-process backend (Celery, arq) can
replace it at M3 without touching the API layer.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from .geometry import Domain2D
from .solvers.base import ProgressEvent, Solver, SolverResult

TERMINAL = ("done", "failed")


@dataclass
class Job:
    id: str
    solver_name: str
    status: str = "queued"  # queued | running | done | failed
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: SolverResult | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)


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
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def submit(
        self, solver_cls: type[Solver], geometry: Domain2D, params: BaseModel
    ) -> Job:
        job = Job(id=f"j-{uuid.uuid4().hex[:12]}", solver_name=solver_cls.name)
        self._jobs[job.id] = job
        asyncio.create_task(self._run(job, solver_cls, geometry, params))
        return job

    async def _run(
        self, job: Job, solver_cls: type[Solver], geometry: Domain2D, params: BaseModel
    ) -> None:
        loop = asyncio.get_running_loop()
        job.status = "running"
        self._publish(job, {"type": "status", "status": "running"})

        def on_progress(event: ProgressEvent) -> None:
            # Called from the worker thread; hop onto the event loop to publish.
            loop.call_soon_threadsafe(self._publish, job, event.model_dump())

        try:
            solver = solver_cls()
            job.result = await loop.run_in_executor(
                None, solver.solve, geometry, params, on_progress
            )
            job.status = "done"
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
