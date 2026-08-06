"""Running one solve (roadmap M3, issue #12).

This is the code that turns a queued job into a finished one: run the solver, stream
progress, persist the result, land on a terminal status. It lives here rather than in the
job manager because **two callers need it** — the in-process backend and the worker
process — and a solve that behaves differently depending on where it ran would be a
miserable class of bug to chase.

Everything the run needs is passed in: where to write artifacts, what to publish to, how
long it may take, and how to tell it to stop. It knows nothing about HTTP, queues, or
which process it is in.
"""

import asyncio
import threading
import time
from concurrent.futures import Executor
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from .events import EventBus
from .geometry import Geometry
from .solvers.base import (
    JobCancelled,
    ProgressEvent,
    Solver,
    SolverContext,
    check_declared_convergence,
    fill_declared_metrics,
)
from .store import JobRecord, JobStore


class EventSink:
    """Records an event, then publishes it with the sequence number the store assigned.

    Store first, always. The seq is what a subscriber uses to reconcile the durable log
    with the live feed, so publishing before persisting would emit a number that does
    not exist yet.
    """

    def __init__(
        self,
        store: JobStore,
        bus: EventBus,
        job_id: str,
        mirror: list[dict] | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._job_id = job_id
        # The in-process manager keeps a live Job whose event list it wants kept current;
        # a worker has no such object and passes None.
        self._mirror = mirror

    async def emit(self, event: dict) -> None:
        if self._mirror is not None:
            self._mirror.append(event)
        seq = self._store.add_event(self._job_id, event)
        await self._bus.publish(self._job_id, seq, event)


async def run_solve(
    record: JobRecord,
    solver_cls: type[Solver],
    geometry: Geometry,
    params: BaseModel,
    *,
    conditions: dict[str, dict[str, float]] | None = None,
    store: JobStore,
    sink: EventSink,
    artifact_dir: Path,
    executor: Executor | None,
    timeout: float,
    cancel_event: threading.Event,
) -> JobRecord:
    """Run one solve to a terminal status, persisting and publishing as it goes.

    Returns the record it has been updating, so an in-process caller can copy the
    outcome onto whatever live object it holds. Never raises for a failed solve: a
    solver bug fails the job, not the caller.
    """
    loop = asyncio.get_running_loop()
    record.status = "running"
    store.put(record)
    await sink.emit({"type": "status", "status": "running"})

    pending: set[asyncio.Task] = set()

    def on_progress(event: ProgressEvent) -> None:
        # Called from the solver thread; hop onto the event loop to publish. The task is
        # held because asyncio keeps only a weak reference to a running one.
        def schedule() -> None:
            task = loop.create_task(sink.emit(event.model_dump()))
            pending.add(task)
            task.add_done_callback(pending.discard)

        loop.call_soon_threadsafe(schedule)

    ctx = SolverContext(
        progress_cb=on_progress,
        cancel_event=cancel_event,
        artifact_dir=artifact_dir,
        conditions=conditions,
    )
    started = time.monotonic()
    try:
        solver = solver_cls()
        work = loop.run_in_executor(executor, solver.solve, geometry, params, ctx)
        record.result = await asyncio.wait_for(work, timeout or None)
        record.result.stats = {
            **record.result.stats,
            "seconds": round(time.monotonic() - started, 4),
        }
        # After the timing, before persisting: the metrics belong to the stored record, and
        # computing them here rather than in each adapter is what keeps the declaration in
        # #43 and the value in #46 from being two independent statements about one number.
        # Before the metrics, because a capability that declares `on_failure="fail"` must
        # not have `t_max` computed off an iterate it already disowned.
        check_declared_convergence(solver_cls, record.result)
        fill_declared_metrics(solver_cls, record.result)
        record.artifacts = ctx.artifacts
        record.status = "done"
    except TimeoutError:
        # The solver thread cannot be killed; ask it to stop and fail the job now.
        cancel_event.set()
        record.status = "failed"
        record.error = f"job exceeded the wall-clock timeout ({timeout:g}s)"
    except JobCancelled:
        record.status = "cancelled"
    except Exception as exc:  # solver bugs must fail the job, not the process
        record.status = "failed"
        record.error = f"{type(exc).__name__}: {exc}"

    record.finished_at = datetime.now(UTC)
    store.put(record)
    # Drain any progress publish still in flight, so the terminal event is genuinely
    # last: a subscriber stops reading at it, and anything after would be lost.
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await sink.emit({"type": "status", "status": record.status, "error": record.error})
    return record
