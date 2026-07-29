"""The application core: everything a caller needs that is not about a transport.

Roadmap M2.5, issue #42. Before this, the logic below lived in the bodies of FastAPI
routes and raised `HTTPException`, so the only way to submit a job was to speak HTTP. M3
had already lifted execution, persistence, event delivery and identity out of the API
layer — `ExecutionBackend`, `JobStore`, `EventBus`, `Principal` — which is why this module
is thin: what was left entangled was the *request* side.

Two rules keep it transport-neutral, and both are load-bearing rather than stylistic:

1. **It raises domain errors, never `HTTPException`.** See :mod:`.errors`.
2. **It does not build URLs.** `result()` returns artifact metadata with a `path`; the HTTP
   adapter turns that into a `url` its own routes can serve, and a local caller reads the
   file directly. A core that emitted `/api/v1/...` strings would be an HTTP core wearing
   a different name.

The job *lifecycle* is not reimplemented here. :class:`~fenixspoon.jobs.JobManager` already
owns submit / get / cancel / subscribe over a pluggable backend; this wraps it with the
validation and authorization that used to sit in the routes.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..geometry import Geometry
from ..jobs import Job, JobManager
from ..solvers import available_solvers, get_solver, registered_solvers
from ..solvers.base import Solver, SolverInfo
from . import discovery, errors
from .discovery import (
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
    LimitsInfo,
    UsageInfo,
)
from .errors import CoreError  # noqa: F401  (re-export: adapters catch this one class)
from .identity import Principal, QuotaUsage, check_quotas, hour_ago


@dataclass(frozen=True)
class ArtifactHandle:
    """An artifact resolved to something a caller can actually open.

    `path` rather than a URL is the whole point: an HTTP adapter serves the file and
    advertises a route, a local caller opens it. Neither is privileged by the core.
    """

    name: str
    content_type: str
    size: int
    path: Path


@dataclass(frozen=True)
class ResultView:
    """A finished job's result, without any transport's framing around it."""

    job_id: str
    kind: str
    data: dict[str, Any]
    stats: dict[str, float]
    artifacts: list[ArtifactHandle]


class FenixSpoonCore:
    """Operations on solvers and jobs, callable from anything.

    Holds a :class:`~fenixspoon.jobs.JobManager` and nothing else. Construct one per
    process; the HTTP app keeps it on `app.state`, and a script can build its own.
    """

    def __init__(self, jobs: JobManager) -> None:
        self.jobs = jobs

    # -------------------------------------------------------------- capabilities

    def capabilities(self) -> list[SolverInfo]:
        """Every installed solver, with its params schema.

        The exhaustive answer, kept because a form generator needs exactly this. For a
        caller that does not, see :meth:`capability_list` and :meth:`capability_describe`.
        """
        return available_solvers()

    def capability(self, name: str) -> type[Solver]:
        """One solver class by name, or :class:`~.errors.UnknownCapability`."""
        solver_cls = get_solver(name)
        if solver_cls is None:
            raise errors.UnknownCapability(name)
        return solver_cls

    # ------------------------------------------------------- progressive discovery (#43)

    def environment(self, principal: Principal) -> EnvironmentInfo:
        """What this installation is, for the principal asking.

        Per-principal because half the interesting numbers are: a caller wants to know its
        own quotas and how much of them it has spent, not the server's abstract policy.
        """
        active, recent, artifact_bytes = self.jobs.store.usage(principal.id, hour_ago())
        return discovery.environment_info(
            principal=principal,
            usage=UsageInfo(
                concurrent_jobs=active,
                jobs_last_hour=recent,
                artifact_bytes=artifact_bytes,
            ),
            execution_backend=self.jobs.backend.kind,
            event_bus=self.jobs.bus.kind,
            store=self.jobs.store.kind,
            data_dir=str(self.jobs.data_dir),
            capabilities=len(registered_solvers()),
            limits=LimitsInfo(
                job_timeout_seconds=self.jobs.job_timeout,
                max_cells=self.jobs.max_cells,
                job_ttl_seconds=self.jobs.job_ttl,
                max_workers=self.jobs.max_workers,
            ),
        )

    def capability_list(self) -> list[CapabilitySummary]:
        """One line per installed capability. No schemas, no prose."""
        return [discovery.summarize(cls) for cls in registered_solvers()]

    def capability_describe(
        self,
        name: str,
        sections: list[str] | None = None,
        *,
        inline_schemas: bool = False,
    ) -> CapabilityDescription:
        """Selected sections of one capability.

        ``sections=None`` returns everything, which is what a human at a CLI wants. An
        explicit list returns those sections and nothing else.
        """
        return discovery.describe_capability(
            self.capability(name),
            sections=sections,
            inline_schemas=inline_schemas,
            max_cells=self.jobs.max_cells,
            job_timeout=self.jobs.job_timeout,
        )

    def capability_schema(self, name: str) -> dict[str, Any]:
        """Resolve a capability's `schema:params/<name>` reference to the JSON Schema.

        The second half of the reference mechanism: `capability.describe` hands back a
        reference so a caller that does not need a few kilobytes of schema never receives
        it, and this is how the caller that does need it asks.
        """
        return self.capability(name).Params.model_json_schema()

    # ---------------------------------------------------------------------- jobs

    async def submit(
        self, solver: str, geometry: Geometry, params: dict[str, Any], principal: Principal
    ) -> Job:
        """Validate, authorize, and hand the job to the execution backend.

        The order of the four checks is deliberate and was inherited from the routes:
        identity of the solver, then whether it accepts this geometry, then whether the
        params parse, then cost, and only then quota. A request that is *malformed* should
        hear about that rather than about a quota it also happens to be over — the
        quota message would send the caller to fix the wrong thing.
        """
        solver_cls = self.capability(solver)

        if geometry.type not in solver_cls.geometry_types:
            raise errors.GeometryKindMismatch(solver, solver_cls.geometry_types, geometry.type)

        try:
            parsed = solver_cls.Params.model_validate(params)
        except ValidationError as exc:
            raise errors.InvalidParams(json.loads(exc.json())) from exc

        estimate = solver_cls.estimate_cells(geometry, parsed)
        limit = self.jobs.max_cells
        if estimate is not None and limit and estimate > limit:
            raise errors.CellBudgetExceeded(estimate, limit)

        active, recent, artifact_bytes = self.jobs.store.usage(principal.id, hour_ago())
        check_quotas(principal, QuotaUsage(active, recent, artifact_bytes))

        return await self.jobs.submit(solver_cls, geometry, parsed, owner=principal.id)

    def job(self, job_id: str, principal: Principal, *, with_result: bool = False) -> Job:
        """One job belonging to this principal, or :class:`~.errors.JobNotFound`."""
        job = self.jobs.get(job_id, owner=principal.id, with_result=with_result)
        if job is None:
            raise errors.JobNotFound()
        return job

    def history(
        self, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[Job], int]:
        """This principal's jobs, newest first, plus the total for pagination."""
        return self.jobs.list_jobs(limit=limit, offset=offset, owner=principal.id)

    async def cancel(self, job_id: str, principal: Principal) -> Job:
        """Request cooperative cancellation. Raises if the job already finished."""
        job = self.job(job_id, principal)
        if not await self.jobs.cancel(job):
            raise errors.JobAlreadyFinished(job.status)
        return job

    def result(self, job_id: str, principal: Principal) -> ResultView:
        """A finished job's result, or an error saying why there isn't one."""
        job = self.job(job_id, principal, with_result=True)
        if job.status in ("failed", "cancelled"):
            raise errors.JobDidNotSucceed(job.status, job.error)
        if job.status != "done" or job.result is None:
            raise errors.JobNotFinished(job.status)
        return ResultView(
            job_id=job.id,
            kind=job.result.kind,
            data=job.result.data,
            stats=job.result.stats,
            artifacts=[self._handle(job, entry) for entry in job.artifacts],
        )

    def artifact(self, job_id: str, name: str, principal: Principal) -> ArtifactHandle:
        """Resolve one artifact to a path on disk.

        Only names the solver registered are resolvable. That, plus the bare-filename rule
        enforced at registration, is what makes path traversal impossible — the name is
        never joined onto a path until it has been matched against the recorded list.
        """
        job = self.job(job_id, principal)
        entry = next((a for a in job.artifacts if a["name"] == name), None)
        if entry is None or job.artifact_dir is None:
            raise errors.ArtifactNotFound()
        handle = self._handle(job, entry)
        if not handle.path.is_file():
            raise errors.ArtifactNotFound("artifact file missing")
        return handle

    def events(self, job: Job):
        """Async iterator of this job's events, history first then live."""
        return self.jobs.subscribe(job)

    @staticmethod
    def _handle(job: Job, entry: dict[str, Any]) -> ArtifactHandle:
        directory = job.artifact_dir or Path()
        return ArtifactHandle(
            name=entry["name"],
            content_type=entry.get("content_type", "application/octet-stream"),
            size=int(entry.get("size", 0)),
            path=directory / entry["name"],
        )
