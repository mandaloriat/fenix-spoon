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

from .. import cache, fields
from ..geometry import Geometry
from ..jobs import Job, JobManager
from ..objects import ObjectFileStore, parse_ref
from ..solvers import available_solvers, get_solver, registered_solvers
from ..solvers.base import Solver, SolverInfo
from . import discovery, errors, results
from .discovery import (
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
    LimitsInfo,
    UsageInfo,
)
from .errors import CoreError  # noqa: F401  (re-export: adapters catch this one class)
from .identity import Principal, QuotaUsage, check_quotas, hour_ago
from .results import LeveledResult
from .workspace import ObjectSummary, ObjectView, ResolvedDesign, Workspace, WorkspaceInfo


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

    def __init__(self, jobs: JobManager, workspace: Workspace | None = None) -> None:
        self.jobs = jobs
        #: Objects live beside the jobs, in the same data directory (roadmap M2.5, #44).
        #: Defaulted rather than injected because there is exactly one sensible location and
        #: making every caller pass it would be ceremony — but it stays a parameter so a
        #: test can point a workspace somewhere else.
        self.workspace = workspace or Workspace(ObjectFileStore(jobs.data_dir))

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
            workspace=str(self.workspace.store.root),
            cache=discovery.CacheInfo(
                enabled=self.jobs.cache,
                scheme=cache.SCHEME,
                cacheable_capabilities=[
                    cls.name for cls in registered_solvers() if cls.deterministic
                ],
                retention=(
                    "a cache entry is the job it points at, so it expires with the job "
                    f"(FENIXSPOON_JOB_TTL, currently {self.jobs.job_ttl:g}s; 0 keeps "
                    "them forever). Sweeping a job makes the next identical submission a "
                    "miss, which recomputes — there is no dangling entry to clean up."
                ),
            ),
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
        self,
        solver: str,
        geometry: Geometry,
        params: dict[str, Any],
        principal: Principal,
        inputs: dict[str, Any] | None = None,
    ) -> Job:
        """Validate, authorize, and hand the job to the execution backend.

        The order of the four checks is deliberate and was inherited from the routes:
        identity of the solver, then whether it accepts this geometry, then whether the
        params parse, then cost, and only then quota. A request that is *malformed* should
        hear about that rather than about a quota it also happens to be over — the
        quota message would send the caller to fix the wrong thing.

        ``inputs`` records which workspace object revisions this job came from, when it came
        from any. It is metadata rather than a second submission path: an inline geometry
        and a resolved design reach the backend identically, which is what stops the
        workspace becoming a parallel job system (#44).
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

        key = self.cache_key_for(solver_cls, geometry, parsed)
        if key is not None:
            reusable = self.jobs.store.find_cached(key, principal.id)
            if reusable is not None:
                # A hit is a lookup, not a copy: the job *is* the cache entry, and what comes
                # back is the one that already ran. Two consequences worth knowing. It costs
                # no quota, because it costs no compute — the checks below are deliberately
                # after this. And a `running` or `queued` match is a hit too, which is how
                # two identical submissions attach to one solve instead of racing; the second
                # caller polls the job the first one started.
                self.jobs.store.mark_reused(reusable.id)
                reusable.reused += 1
                return Job.from_record(reusable, self.jobs.data_dir / reusable.id)

        active, recent, artifact_bytes = self.jobs.store.usage(principal.id, hour_ago())
        check_quotas(principal, QuotaUsage(active, recent, artifact_bytes))

        return await self.jobs.submit(
            solver_cls, geometry, parsed, owner=principal.id, inputs=inputs, cache_key=key
        )

    def cache_key_for(
        self, solver_cls: type[Solver], geometry: Geometry, params: Any
    ) -> str | None:
        """This solve's content-addressed identity, or None if it must not be cached.

        None means one of two things, and both are refusals rather than failures: the server
        has caching switched off, or the adapter has not declared itself deterministic. The
        second is the default — see :attr:`Solver.deterministic` for why serving a cached
        answer from a solver that does not reproduce is the worst outcome available.

        The *validated* geometry and params go into the hash, never what the caller sent.
        That is what makes the cache hit at all: an omitted default and an explicit one are
        different JSON and the same solve.
        """
        if not self.jobs.cache or not solver_cls.deterministic:
            return None
        return cache.cache_key(
            solver=solver_cls.name,
            solver_version=solver_cls.version,
            geometry=geometry.model_dump(mode="json"),
            params=params.model_dump(mode="json"),
            environment=cache.environment_fingerprint(list(solver_cls.requires)),
        )

    def jobs_for_object(
        self, reference: str, principal: Principal, limit: int = 50
    ) -> list[Job]:
        """Every solve that used this workspace object — the design → job relation, backwards.

        Unpinned finds all revisions, pinned finds one. Study is not in the chain yet: #48
        introduces the object, and this reads whatever `inputs` records, so it will pick up
        `study:s-1` the day a study writes one without this method changing.
        """
        try:
            parse_ref(reference)
        except ValueError as exc:
            raise errors.MalformedReference(reference, str(exc)) from exc
        records = self.jobs.store.find_by_input(reference, principal.id, limit=limit)
        return [Job.from_record(record, self.jobs.data_dir / record.id) for record in records]

    async def submit_design(self, design: str, principal: Principal) -> Job:
        """Solve a design by reference — the iteration loop this milestone is for.

        No parameter overrides, deliberately. An override would produce a job whose inputs
        are not fully described by any object revision, and "what exactly was solved" is the
        question the workspace exists to answer. To change a parameter, patch the design:
        that is one small JSON Patch, it is versioned, and the next solve is reproducible
        from the workspace alone.
        """
        geometry, resolved = self.workspace.resolve_design(design, principal.id)
        return await self.submit(
            resolved.solver,
            geometry,
            resolved.params,
            principal,
            inputs=resolved.model_dump(exclude={"params"}),
        )

    # ---------------------------------------------------------------- workspace (#44)

    def workspace_info(self) -> WorkspaceInfo:
        """Where the workspace is and what is in it — `workspace.open`.

        There is no open/close: the workspace is a directory, and this reports on it. A
        session handle would be state to lose, and the data directory is already configured
        for the process.
        """
        return self.workspace.info()

    def objects(self, principal: Principal, object_type: str | None = None) -> list[ObjectSummary]:
        """This principal's objects, newest first, without their bodies — `workspace.list`."""
        return self.workspace.list_objects(principal.id, object_type)

    def create_object(
        self,
        object_type: str,
        body: dict[str, Any],
        principal: Principal,
        label: str | None = None,
    ) -> ObjectView:
        return self.workspace.create(object_type, body, principal.id, label)

    def object(self, ref: str, principal: Principal) -> ObjectView:
        """One object: the head, or the exact revision if ``ref`` is pinned."""
        return self.workspace.get(ref, principal.id)

    def object_revisions(self, ref: str, principal: Principal) -> list[int]:
        return self.workspace.revisions(ref, principal.id)

    def patch_object(
        self,
        ref: str,
        patch: list[dict[str, Any]],
        principal: Principal,
        label: str | None = None,
    ) -> ObjectView:
        """Apply an RFC 6902 patch and return the new revision."""
        return self.workspace.patch(ref, patch, principal.id, label)

    def resolve_design(self, ref: str, principal: Principal) -> ResolvedDesign:
        """What a design currently resolves to, without submitting anything.

        The dry run: it is how a caller checks that a reference chain is intact, and how it
        learns which revisions a submission *would* freeze.
        """
        return self.workspace.resolve_design(ref, principal.id)[1]

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
        if job.status != "done":
            raise errors.JobNotFinished(job.status)
        if job.result is None:
            # Was `JobNotFinished("done")`, which is self-contradictory: it told a caller to
            # keep polling a job that had already finished. Raised in review of #67.
            raise errors.ResultPayloadMissing(job.id)
        return ResultView(
            job_id=job.id,
            kind=job.result.kind,
            data=job.result.data,
            stats=job.result.stats,
            artifacts=[self._handle(job, entry) for entry in job.artifacts],
        )

    # ------------------------------------------------------------ compact results (#46)

    def result_levels(
        self, job_id: str, principal: Principal, levels: list[str] | None = None
    ) -> LeveledResult:
        """A finished job's answer, at the levels asked for — the compact `result.get`.

        The default omits `fields`, which is the point: a caller that says nothing receives
        something it can read. Whether the field payload is loaded off disk at all follows
        from the request, so the compact levels cost a row read rather than a multi-megabyte
        parse.
        """
        wanted = results.check_levels(levels)
        job = self.job(job_id, principal, with_result="fields" in wanted)
        if job.status in ("failed", "cancelled"):
            raise errors.JobDidNotSucceed(job.status, job.error)
        if job.status != "done":
            raise errors.JobNotFinished(job.status)
        if "fields" in wanted and job.result is None:
            # A *requested* level must never go quietly missing. Absent-means-unrequested is
            # the rule this payload is built on, so returning no `fields` key here would tell
            # a caller that asked for the arrays that the result has none — the same silent
            # wrong answer an unknown level name is refused to avoid. Raised in review of #67.
            raise errors.ResultPayloadMissing(job.id)
        return results.build(
            job_id=job.id,
            solver=job.solver_name,
            status=job.status,
            error=job.error,
            created_at=job.created_at.isoformat(),
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
            summary=job.summary,
            artifacts=[
                results.ArtifactView(
                    name=handle.name,
                    content_type=handle.content_type,
                    size=handle.size,
                    path=str(handle.path),
                )
                for handle in (self._handle(job, entry) for entry in job.artifacts)
            ],
            data=job.result.data if job.result is not None else None,
            provenance=self._provenance(job),
            levels=wanted,
        )

    def provenance(self, job_id: str, principal: Principal) -> results.Provenance:
        """Where a job's answer came from. Cheap — metadata only, never the payload."""
        return self._provenance(self.job(job_id, principal))

    def _provenance(self, job: Job) -> results.Provenance:
        """Where this result came from, assembled from what the job recorded.

        `cached` is `reused > 0`: at least one submission was answered with this job instead
        of solving. That makes the flag answerable at any read rather than only in the reply
        to the submission that hit — the first solve reports false, and the moment an
        identical resubmission lands it reports true, which is the loop the acceptance
        criterion describes.
        """
        solver_cls = get_solver(job.solver_name)
        return results.Provenance(
            job_id=job.id,
            cached=job.reused > 0,
            solver=job.solver_name,
            solver_version=solver_cls.version if solver_cls else "unknown",
            cache_key=job.cache_key,
            computed_at=job.finished_at.isoformat() if job.finished_at else None,
            seconds=(job.summary.stats.get("seconds") if job.summary else None),
            environment=(
                cache.environment_fingerprint(list(solver_cls.requires)) if solver_cls else {}
            ),
            inputs=dict(job.inputs),
        )

    def query_result(
        self, job_id: str, query: results.FieldQuery, principal: Principal
    ) -> results.FieldQueryResult:
        """Ask one bounded question of one field, without the field going anywhere.

        This is the level that has to load the payload — you cannot find a peak without
        reading the array — and the point is that the array stops here. What crosses back is
        a number and a coordinate.
        """
        job = self.job(job_id, principal, with_result=True)
        if job.status in ("failed", "cancelled"):
            raise errors.JobDidNotSucceed(job.status, job.error)
        if job.status != "done":
            raise errors.JobNotFinished(job.status)
        if job.result is None:
            # A query is the one operation that genuinely needs the arrays — you cannot find
            # a peak without reading them — so this is where their absence has to be said
            # plainly rather than reported as a job that has not finished. Raised in review
            # of #67.
            raise errors.ResultPayloadMissing(job.id)

        inside = None
        if query.op == "over_region":
            inside = self._region_mask(job, query, principal)
        try:
            answer = fields.query(
                job.result.data,
                job.result.kind,
                query.field,
                query.op,
                at=query.at,
                start=query.start,
                end=query.end,
                samples=query.samples,
                count=query.count,
                minimum=query.minimum,
                inside=inside,
            )
        except fields.FieldError as exc:
            raise errors.FieldQueryFailed(str(exc)) from exc
        return results.FieldQueryResult(
            job_id=job.id, field=query.field, op=query.op, result=answer
        )

    def _region_mask(self, job: Job, query: results.FieldQuery, principal: Principal):
        """Resolve `over_region` against the geometry this job recorded when it was submitted.

        A result carries arrays, not region names, so the geometry has to come from
        somewhere. Workspace provenance (#44) is that somewhere — and a job submitted with
        an inline geometry kept none, which is a real limitation and is reported as one
        rather than as an empty region. Persisting the geometry with every job would fix it
        and belongs with provenance in #47.
        """
        if not query.region:
            raise errors.RegionUnavailable("", "over_region needs a `region` name")
        reference = job.inputs.get("geometry")
        if not reference:
            raise errors.RegionUnavailable(
                query.region,
                "this job was submitted with an inline geometry, so it kept no reference to "
                "one. Submit through a design (`job.submit` with a design id) and the region "
                "becomes resolvable.",
            )
        geometry = self.workspace.get(reference, principal.id)
        field = fields.load(job.result.data, job.result.kind, query.field)
        return results.region_selection(geometry.body, query.region, field.xy)

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
