"""The Python API: a supported in-process entrypoint (roadmap M2.5, #50).

The design draft's second bad option was "import `fenixspoon` and call internals". This is
the replacement — a small synchronous facade over the same core every other transport uses,
returning the same typed models rather than dicts.

    from fenixspoon import local

    with local.open_workspace() as fs:
        geometry = fs.create("geometry", {"type": "domain2d", ...})
        design = fs.create("design", {"solver": "mock.laplace2d", "geometry": geometry.ref})
        job = fs.submit(design=design.ref).wait()
        print(job.metrics())

## Why it owns an event loop

The core is async and a notebook is not. Three ways to bridge that, and only one of them is
usable from a REPL:

1. Make every method a coroutine. Correct, and it makes the *simplest* use — solve something,
   print a number — require the caller to understand `asyncio.run`, which is precisely the
   audience that does not want to.
2. Wrap each call in its own `asyncio.run`. This is the trap, and it is worth naming because
   the test suites here hit it three times: the in-process backend completes a solve through
   callbacks on the loop that *submitted* it, so a second `asyncio.run` watches a status that
   can never move. It would appear to work for everything except actually finishing a job.
3. Own one loop, on a background thread, for the session's lifetime. Submit and wait are then
   ordinary blocking calls, the solve completes on the loop that started it, and a caller can
   submit, do something else, and wait later — which is the notebook shape.

Three it is. The loop is a daemon thread so an interpreter that exits without closing the
session is not held open by it, and :meth:`Session.close` stops it deterministically for
everything else.

## What it is not

It is not a second implementation of anything. Every method here is a call into
:class:`~fenixspoon.core.FenixSpoonCore` with a loop around it where one is needed; the
validation, the errors, the ids, the quotas and the result cache are the ones every other
transport gets, because they are the same objects.
"""

import asyncio
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from .core import FenixSpoonCore, errors, results, studies
from .core.discovery import CapabilityDescription, CapabilitySummary, EnvironmentInfo
from .core.identity import ANONYMOUS, Principal, Quotas
from .core.service import ArtifactHandle, EventPage
from .core.workspace import ObjectSummary, ObjectView
from .geometry import Geometry
from .jobs import TERMINAL, JobManager, JobStatus

__all__ = ["Job", "Session", "open_workspace"]


class Job:
    """A handle on one submitted solve, with the session that can answer for it.

    Returned rather than a bare id because the id alone forces every follow-up call to name
    the session again — and in a notebook the natural thing to hold on to is the job.
    """

    def __init__(self, session: "Session", job_id: str) -> None:
        self.session = session
        self.id = job_id

    def __repr__(self) -> str:
        # Local data only. `__repr__` runs in debuggers, logs and tracebacks — often while
        # something is already wrong — so a version that called `status()` would raise from
        # inside the report of the original failure, on a closed session or a swept job.
        # Raised in review of #50.
        return f"<Job {self.id}>"

    def status(self) -> JobStatus:
        return JobStatus.from_job(self.session.core.job(self.id, self.session.principal))

    def wait(self, timeout: float = 600.0, poll: float = 0.05) -> "Job":
        """Block until this job is terminal, then return it for chaining.

        Polls rather than subscribes. A subscription would deliver every progress tick to a
        caller that asked to *wait*, which is the opposite of what waiting means; the event
        log is there for a caller that wants them (:meth:`events`).
        """
        deadline = self.session._now() + timeout
        while self.status().status not in TERMINAL:
            if self.session._now() > deadline:
                raise TimeoutError(f"job {self.id} did not finish within {timeout:g}s")
            self.session._sleep(poll)
        return self

    def result(self, levels: list[str] | None = None) -> results.LeveledResult:
        """The compact answer. Defaults omit the field arrays, as everywhere else."""
        return self.session.core.result_levels(self.id, self.session.principal, levels)

    def metrics(self) -> dict[str, float]:
        """Just the declared engineering quantities — the usual reason to look at a solve."""
        return self.result(["metrics"]).metrics or {}

    def query(self, field: str, op: str, **kwargs: Any) -> results.FieldQueryResult:
        """One bounded question of one field. The array stays on the server side of this."""
        query = results.FieldQuery(field=field, op=op, **kwargs)
        return self.session.core.query_result(self.id, query, self.session.principal)

    def provenance(self) -> results.Provenance:
        return self.session.core.provenance(self.id, self.session.principal)

    def events(self, since: int = 0, limit: int = 200) -> EventPage:
        return self.session.core.job_events(
            self.id, self.session.principal, since=since, limit=limit
        )

    def artifact(self, name: str) -> ArtifactHandle:
        """An artifact resolved to a path. In-process, so it is simply a file to open."""
        return self.session.core.artifact(self.id, name, self.session.principal)

    def cancel(self) -> "Job":
        self.session._run(self.session.core.cancel(self.id, self.session.principal))
        return self


class Session:
    """An open workspace and the operations on it.

    One per process is the expected shape; nothing stops a second, and two sessions on the
    same data directory behave like two callers, because that is what they are.
    """

    def __init__(
        self,
        data_dir: Path | str | None = None,
        *,
        principal: str = ANONYMOUS,
        core: FenixSpoonCore | None = None,
    ) -> None:
        self.core = core or FenixSpoonCore(
            JobManager(data_dir=Path(data_dir) if data_dir is not None else None)
        )
        #: A real principal, not a bypass — jobs are owned, quotas apply, and the result
        #: cache is per-principal exactly as it is over every other transport.
        self.principal = Principal(id=principal, quotas=Quotas.from_env())
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="fenixspoon-session", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Release the backend, the bus and the store, then stop the loop. Idempotent.

        The sequence is :meth:`JobManager.aclose`, not one of mine: it is a property of the
        manager, and it exists because an abandoned `ThreadPoolExecutor` turns a long solve
        into a process that will not exit — `concurrent.futures` registers an atexit hook that
        *joins* its threads. A session that closed only the store would reintroduce exactly
        that, in the one place where the caller is a notebook that stays alive. Two shutdown
        sequences would be two chances to get the order wrong. Raised in review of #50.
        """
        if self._loop.is_closed():
            return
        # On the session's loop, because that is where the backend's tasks live — and while
        # it is still running, because `aclose` is a coroutine.
        try:
            asyncio.run_coroutine_threadsafe(self.core.jobs.aclose(), self._loop).result(
                timeout=30
            )
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _run(self, coro) -> Any:
        """Run a coroutine on the session's loop and block for its result.

        The one place threads and asyncio meet here. `run_coroutine_threadsafe` is what makes
        a submit from the calling thread land on the loop that will later complete it.
        """
        future: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    @staticmethod
    def _now() -> float:
        import time

        return time.monotonic()

    @staticmethod
    def _sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)

    # ---------------------------------------------------------------- discovery

    def environment(self) -> EnvironmentInfo:
        return self.core.environment(self.principal)

    def capabilities(self) -> list[CapabilitySummary]:
        return self.core.capability_list()

    def describe(
        self, name: str, sections: list[str] | None = None, *, inline_schemas: bool = False
    ) -> CapabilityDescription:
        return self.core.capability_describe(name, sections, inline_schemas=inline_schemas)

    # ---------------------------------------------------------------- workspace

    def objects(self, object_type: str | None = None) -> list[ObjectSummary]:
        return self.core.objects(self.principal, object_type)

    def create(
        self, object_type: str, body: dict[str, Any], label: str | None = None
    ) -> ObjectView:
        return self.core.create_object(object_type, body, self.principal, label)

    def get(self, ref: str) -> ObjectView:
        return self.core.object(ref, self.principal)

    def patch(
        self, ref: str, patch: list[dict[str, Any]], label: str | None = None
    ) -> ObjectView:
        """Apply an RFC 6902 patch. The iteration primitive: send the edit, not the object."""
        return self.core.patch_object(ref, patch, self.principal, label)

    # --------------------------------------------------------------------- jobs

    def submit(
        self,
        design: str | None = None,
        *,
        solver: str | None = None,
        geometry: Geometry | dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Job:
        """Solve a design by reference, or an inline solver + geometry.

        Returns as soon as the work is accepted — call :meth:`Job.wait` to block. The two
        forms are mutually exclusive for the reason the JSON-RPC binding gives: a caller that
        passes both has two intents in one request, and honouring one silently produces a job
        whose inputs are not what it thinks.
        """
        inline = solver is not None or geometry is not None
        if design is not None and inline:
            raise ValueError(
                "pass either `design` or `solver` + `geometry`, not both: a design already "
                "names its solver and geometry"
            )
        if design is not None:
            return Job(self, self._run(self.core.submit_design(design, self.principal)).id)
        if not (solver and geometry is not None):
            raise ValueError("pass a `design`, or both `solver` and `geometry`")

        from pydantic import TypeAdapter

        parsed = (
            geometry
            if not isinstance(geometry, dict)
            else TypeAdapter(Geometry).validate_python(geometry)
        )
        job = self._run(
            self.core.submit(solver, parsed, params or {}, self.principal)
        )
        return Job(self, job.id)

    def job(self, job_id: str) -> Job:
        """A handle on an existing job. Raises if it is not this principal's."""
        self.core.job(job_id, self.principal)
        return Job(self, job_id)

    def history(self, limit: int = 50, offset: int = 0) -> list[JobStatus]:
        jobs, _total = self.core.history(self.principal, limit=limit, offset=offset)
        return [JobStatus.from_job(job) for job in jobs]

    def jobs_for(self, ref: str, limit: int = 50) -> list[Job]:
        """Every solve that used a workspace object — the relation, read backwards."""
        return [
            Job(self, job.id) for job in self.core.jobs_for_object(ref, self.principal, limit)
        ]

    # ------------------------------------------------------------------ studies

    def run_study(self, ref: str) -> studies.StudyRun:
        return self._run(self.core.run_study(ref, self.principal))

    def study(self, ref: str) -> studies.StudyReport:
        return self.core.study_report(ref, self.principal)

    def wait_for_study(
        self, ref: str, timeout: float = 900.0, poll: float = 0.05
    ) -> studies.StudyReport:
        """Block until every rung is terminal. The convenience a script actually wants."""
        deadline = self._now() + timeout
        while True:
            report = self.study(ref)
            if report.complete:
                return report
            if self._now() > deadline:
                raise TimeoutError(f"study {ref} did not finish within {timeout:g}s")
            self._sleep(poll)


def open_workspace(
    data_dir: Path | str | None = None, *, principal: str = ANONYMOUS
) -> Session:
    """Open a workspace and return a session on it.

    ``data_dir`` defaults to whatever `FENIXSPOON_DATA_DIR` says, or `./.fenix-spoon` — the
    same directory every other transport uses, so a design created here is the one the CLI
    and an MCP host see.

    Named `open_workspace` rather than `open` because shadowing a builtin in a module people
    are told to `from fenixspoon import local` is a trap: `local.open` reads fine right up to
    the moment somebody does `from fenixspoon.local import open`.
    """
    return Session(data_dir, principal=principal)


# Re-exported so a caller that catches failures does not have to know which module they came
# from. `errors.CoreError` is the one class that covers every refusal this API can produce.
CoreError = errors.CoreError
