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
from ..jobs import TERMINAL, Job, JobManager
from ..objects import ObjectFileStore, parse_ref
from ..solvers import available_solvers, get_solver, registered_solvers
from ..solvers.base import Solver, SolverInfo
from . import discovery, errors, results, studies
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
    t: float | None = None
    """The instant this file holds, for a time-dependent solve (#86); None otherwise.

    On the handle rather than in a list beside it, so the time index of a result is derived
    from the files themselves and cannot name one that is not there.
    """


@dataclass(frozen=True)
class ResultView:
    """A finished job's result, without any transport's framing around it."""

    job_id: str
    kind: str
    data: dict[str, Any]
    stats: dict[str, float]
    artifacts: list[ArtifactHandle]


@dataclass(frozen=True)
class EventPage:
    """A slice of a job's progress log, read by sequence number (roadmap M2.5, #45).

    The polling counterpart of the streaming subscription. Both exist because a caller with
    a context window and a caller with a socket want different things: an agent submits and
    then asks "anything since 4?" at whatever cadence suits it, while a browser wants every
    tick as it happens. The log is sequence-numbered and stored, so the two read the same
    events and neither can miss one — which is what makes polling a real answer here rather
    than a degraded one.
    """

    job_id: str
    events: list[dict[str, Any]]
    #: Sequence number to pass as ``since`` next time. Numbering starts at 1, so a first
    #: call passes 0 and this comes back as the count delivered so far.
    next: int
    #: True once a terminal status event has been delivered. The caller's stop condition,
    #: so it does not have to know which event types are terminal.
    final: bool


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
        conditions: dict[str, dict[str, float]] | None = None,
    ) -> Job:
        """Validate, authorize, and hand the job to the execution backend.

        The order of the checks is deliberate and was inherited from the routes:
        identity of the solver, then whether it accepts this geometry, then whether the
        params parse, then the load case, then cost, and only then quota. A request that is
        *malformed* should hear about that rather than about a quota it also happens to be
        over — the quota message would send the caller to fix the wrong thing.

        ``inputs`` records which workspace object revisions this job came from, when it came
        from any. It is metadata rather than a second submission path: an inline geometry
        and a resolved design reach the backend identically, which is what stops the
        workspace becoming a parallel job system (#44).

        ``conditions`` is the load case, already merged (#85). It arrives here as values
        rather than as a reference for the same reason: `submit_design` resolves, and this
        method takes what a solve actually needs, so there is one submission path and not two.
        """
        solver_cls = self.capability(solver)

        if geometry.type not in solver_cls.geometry_types:
            raise errors.GeometryKindMismatch(solver, solver_cls.geometry_types, geometry.type)

        try:
            parsed = solver_cls.Params.model_validate(params)
        except ValidationError as exc:
            raise errors.InvalidParams(json.loads(exc.json())) from exc

        conditions = self._check_conditions(solver_cls, geometry, conditions)

        estimate = solver_cls.estimate_cells(geometry, parsed)
        limit = self.jobs.max_cells
        if estimate is not None and limit and estimate > limit:
            raise errors.CellBudgetExceeded(estimate, limit)

        # Captured here, once, and used for both things that need it: the cache key below
        # and the provenance this job will report for as long as it exists. Read back from
        # the record rather than recomputed, so a later `Solver.version` bump or package
        # upgrade cannot rewrite what an old job says produced it.
        fingerprint = cache.environment_fingerprint(list(solver_cls.requires))
        provenance = {"solver_version": solver_cls.version, "environment": fingerprint}

        key = self.cache_key_for(
            solver_cls, geometry, parsed, conditions=conditions, environment=fingerprint
        )
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
            solver_cls,
            geometry,
            parsed,
            owner=principal.id,
            inputs=inputs,
            cache_key=key,
            provenance=provenance,
            conditions=conditions,
        )

    @staticmethod
    def _check_conditions(
        solver_cls: type[Solver],
        geometry: Geometry,
        conditions: dict[str, dict[str, float]] | None,
    ) -> dict[str, dict[str, float]]:
        """The three refusals a load case can earn, before anything is queued (#85).

        All three exist because the failure they prevent is *silent*. A condition that does not
        reach the assembly leaves the body under-constrained or unloaded, and the solve still
        converges and still answers — for a different problem, under the caller's name for this
        one. There is nothing downstream that can notice, which is why the checks are here and
        why none of them is a warning.

        Checked against the **validated** geometry and the adapter's declaration, so a boundary
        renamed by a geometry patch fails the next submit rather than the next reading of the
        numbers.
        """
        if not conditions:
            return {}
        if not solver_cls.conditions:
            raise errors.CapabilityTakesNoConditions(solver_cls.name)

        declared = {spec.key: spec for spec in solver_cls.conditions}
        available = [entry.name for entry in getattr(geometry, "boundaries", [])]
        for boundary, values in conditions.items():
            if boundary not in available:
                raise errors.UnknownBoundary(boundary, solver_cls.name, available)
            for key in values:
                if key not in declared:
                    raise errors.UnknownConditionKey(
                        key, boundary, solver_cls.name, list(declared)
                    )
            for key in values:
                missing = [item for item in declared[key].requires if item not in values]
                if missing:
                    raise errors.ConditionNeedsCompanion(key, boundary, missing)
        return {name: dict(values) for name, values in conditions.items()}

    def cache_key_for(
        self,
        solver_cls: type[Solver],
        geometry: Geometry,
        params: Any,
        *,
        conditions: dict[str, dict[str, float]] | None = None,
        environment: dict[str, str] | None = None,
    ) -> str | None:
        """This solve's content-addressed identity, or None if it must not be cached.

        None means one of two things, and both are refusals rather than failures: the server
        has caching switched off, or the adapter has not declared itself deterministic. The
        second is the default — see :attr:`Solver.deterministic` for why serving a cached
        answer from a solver that does not reproduce is the worst outcome available.

        The *validated* geometry and params go into the hash, never what the caller sent.
        That is what makes the cache hit at all: an omitted default and an explicit one are
        different JSON and the same solve.

        ``environment`` lets a caller that has already fingerprinted the environment pass it
        in, so the key and the provenance recorded beside it are built from one reading
        rather than two. Omitted, it is taken now.
        """
        if not self.jobs.cache or not solver_cls.deterministic:
            return None
        if environment is None:
            environment = cache.environment_fingerprint(list(solver_cls.requires))
        return cache.cache_key(
            solver=solver_cls.name,
            solver_version=solver_cls.version,
            geometry=geometry.model_dump(mode="json"),
            params=params.model_dump(mode="json"),
            conditions=conditions or {},
            environment=environment,
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
            # `conditions` is excluded for the same reason `params` is: `inputs` is the map of
            # *object references* this job ran on, and `jobs_for_object` reads it backwards
            # looking for them. The conditions are recoverable from the pinned `load_cases`
            # already in there, and putting a dict of floats beside the references would give
            # that backwards scan strings to walk that are not references at all.
            inputs=resolved.model_dump(exclude={"params", "conditions"}),
            conditions=resolved.conditions,
        )

    # -------------------------------------------------------------------- studies (#48)

    def _study_plan(self, ref: str, principal: Principal):
        """Everything both study operations need: the spec, the design it varies, the solver.

        Shared because `study.run` and `study.get` must agree on what the study *means* down
        to the parameters of every rung — they resolve the same design, apply the same
        overrides and, in `get`'s case, recompute the same cache keys. Two copies of that
        would be two chances to disagree, and the symptom would be a report about jobs the
        run never submitted.
        """
        record = self.workspace.get_typed(ref, principal.id, "study")
        try:
            body = studies.StudyBody.model_validate(record.body)
        except ValidationError as exc:  # pragma: no cover - create() already validated it
            raise errors.InvalidObject("study", json.loads(exc.json())) from exc

        geometry, resolved = self.workspace.resolve_design(body.design, principal.id)
        solver_cls = self.capability(resolved.solver)

        # The guard that earns its place. A solver's `Params` ignores unknown fields, so a
        # study varying `resolutoin` would submit every rung with identical parameters, the
        # result cache would collapse them onto one job, and the report would show the same
        # metric at every rung — a *perfectly converged* answer that is entirely fabricated.
        # A study is exactly the operation where that failure is invisible.
        if body.parameter not in solver_cls.Params.model_fields:
            raise errors.InvalidObject(
                "study",
                [
                    {
                        "type": "unknown_parameter",
                        "loc": ["parameter"],
                        "msg": (
                            f"{solver_cls.name!r} has no parameter {body.parameter!r}; "
                            f"it accepts {sorted(solver_cls.Params.model_fields)}"
                        ),
                    }
                ],
            )
        # Same rule for the metric names, and checked here so it applies to `study.run` as
        # well as `study.get` — a study that cannot be tabulated should not spend compute
        # first and say so afterwards.
        studies.tabulated_metrics(body, [spec.name for spec in solver_cls.metrics])
        return record, body, geometry, resolved, solver_cls

    async def run_study(self, ref: str, principal: Principal) -> studies.StudyRun:
        """Submit every rung of a study — `study.run` (roadmap M2.5, #48).

        Returns as soon as the work is accepted, not when it is done: solving a five-rung
        mesh ladder takes minutes and this is one call on a channel that must stay usable.
        The table appears in :meth:`study_report`.

        Each rung goes through :meth:`submit` like any other job, which is what makes a study
        obey the cell budget and the quota per job rather than per study, and what makes an
        already-computed rung free — the result cache answers it without solving.

        A rung the server refuses does not fail the study. Rung 4 exceeding the cell budget
        says nothing about rungs 1–3, and reporting the whole run as failed would throw away
        work that succeeded; the refusal is recorded and shows up against that rung in the
        report.
        """
        record, body, geometry, resolved, solver_cls = self._study_plan(ref, principal)

        jobs: list[str] = []
        submitted = reused = refused = 0
        for index, value in enumerate(body.values):
            try:
                job = await self.submit(
                    resolved.solver,
                    geometry,
                    {**resolved.params, body.parameter: value},
                    principal,
                    inputs=studies.variation_inputs(record.pinned, index, value, resolved),
                )
            except errors.CoreError:
                refused += 1
                continue
            jobs.append(job.id)
            if job.reused > 0:
                reused += 1
            else:
                submitted += 1
        return studies.StudyRun(
            study=record.pinned,
            jobs=jobs,
            submitted=submitted,
            reused=reused,
            refused=refused,
        )

    def study_report(self, ref: str, principal: Principal) -> studies.StudyReport:
        """The (variation → metric) table — `study.get` (roadmap M2.5, #48).

        Built by resolving each rung back to its job rather than by reading a stored run
        record, because there is no stored run record: the study says what to solve, the jobs
        are the answer, and a third thing tracking which is which is a third thing that can
        be wrong.

        Two resolution paths, and the second is not defensive padding. The **cache key** is
        the normal one: it is deterministic, and it finds a rung that was answered by a solve
        somebody ran standalone last week — which is the whole point of reusing the cache and
        which no `inputs` lookup can find, because that job's inputs never mentioned this
        study. When there is no key (caching off, or the adapter does not declare itself
        deterministic) the recorded `inputs` answer instead.
        """
        record, body, geometry, resolved, solver_cls = self._study_plan(ref, principal)
        by_input = {
            job.inputs.get("variation_index"): job
            for job in self.jobs_for_object(record.pinned, principal, limit=len(body.values) * 4)
        }

        rungs: list[studies.StudyRung] = []
        declared = [spec.name for spec in solver_cls.metrics]
        columns: dict[str, list[float | None]] = {
            name: [] for name in studies.tabulated_metrics(body, declared)
        }
        for index, value in enumerate(body.values):
            job = self._rung_job(solver_cls, geometry, resolved, body, value, principal)
            if job is None:
                job = by_input.get(index)
            rungs.append(self._rung(value, job))
            metrics = rungs[-1].metrics
            for name, column in columns.items():
                column.append(metrics.get(name))

        return studies.StudyReport(
            study=record.pinned,
            kind=body.kind,
            design=resolved.design,
            parameter=body.parameter,
            solver=resolved.solver,
            rungs=rungs,
            convergence=studies.convergence_of(body.values, columns, body.tolerance),
            complete=all(rung.status in (*TERMINAL, "refused") for rung in rungs),
        )

    def _rung_job(self, solver_cls, geometry, resolved, body, value, principal) -> Job | None:
        """This rung's job by content address, or None when it has no address to look up."""
        try:
            parsed = solver_cls.Params.model_validate(
                {**resolved.params, body.parameter: value}
            )
        except ValidationError:
            # The rung's parameters do not validate — the same refusal `run_study` recorded.
            # Reported as a missing rung rather than raised, because one bad value must not
            # take the whole table with it.
            return None
        key = self.cache_key_for(solver_cls, geometry, parsed)
        if key is None:
            return None
        found = self.jobs.store.find_cached(key, principal.id)
        return Job.from_record(found, self.jobs.data_dir / found.id) if found else None

    @staticmethod
    def _rung(value: float, job: Job | None) -> studies.StudyRung:
        if job is None:
            return studies.StudyRung(
                value=value,
                status="refused",
                error="this rung was not accepted; run the study to see why",
            )
        return studies.StudyRung(
            value=value,
            job_id=job.id,
            status=job.status,
            cached=job.reused > 0,
            # From the summary, which is database columns rather than the payload file — a
            # five-rung study reads five rows instead of five multi-megabyte JSON documents.
            metrics=dict(job.summary.metrics) if job.summary else {},
            error=job.error,
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
                    t=handle.t,
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

        The solver version and environment come from what the job *recorded*, never from the
        registry as it stands now. Recomputing was the first implementation and it was
        wrong: it made an old job report the version of whatever adapter happens to be
        installed today, and made an uninstalled solver's history report an empty
        environment as if none had been used. Worse, a job could contradict itself — the
        `cache_key` it stored was derived from the old version while `solver_version` beside
        it named the new one. Raised in review of #47.
        """
        recorded = job.provenance
        return results.Provenance(
            job_id=job.id,
            cached=job.reused > 0,
            solver=job.solver_name,
            # Only for jobs written before the column existed. Saying `unknown` is the
            # honest answer — the facts were not captured — and it is exactly what
            # recomputing would paper over.
            solver_version=str(recorded.get("solver_version") or "unknown"),
            cache_key=job.cache_key,
            computed_at=job.finished_at.isoformat() if job.finished_at else None,
            seconds=(job.summary.stats.get("seconds") if job.summary else None),
            environment=dict(recorded.get("environment") or {}),
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

    def job_events(
        self, job_id: str, principal: Principal, *, since: int = 0, limit: int = 200
    ) -> EventPage:
        """Events after sequence ``since`` — the polling read of the progress log (#45).

        Read from the store rather than from the live job, because the store is where events
        are numbered: :class:`~fenixspoon.execution.EventSink` writes them there and the
        sequence *is* the position in that log. A live in-process job holds no copy, so
        reading it would answer "no events" for a solve that is emitting them.

        ``limit`` bounds the page for the same reason every other listing is bounded — a
        solve reporting every hundred iterations produces a log a caller should page through
        rather than receive in one answer it did not size.
        """
        self.job(job_id, principal)  # ownership check; raises JobNotFound
        record = self.jobs.store.get(job_id, with_result=False, with_events=True)
        stored = record.events if record is not None else []
        page = stored[since : since + limit]
        delivered = since + len(page)
        return EventPage(
            job_id=job_id,
            events=page,
            next=delivered,
            # Terminal *as far as this caller has read*: a page that stops short of the end
            # is not final even if the log has since finished, or the caller would stop
            # polling with events it never saw still sitting in the log.
            final=any(
                event.get("type") == "status" and event.get("status") in TERMINAL
                for event in page
            ),
        )

    @staticmethod
    def _handle(job: Job, entry: dict[str, Any]) -> ArtifactHandle:
        directory = job.artifact_dir or Path()
        return ArtifactHandle(
            name=entry["name"],
            content_type=entry.get("content_type", "application/octet-stream"),
            size=int(entry.get("size", 0)),
            path=directory / entry["name"],
            t=(float(entry["t"]) if entry.get("t") is not None else None),
        )
