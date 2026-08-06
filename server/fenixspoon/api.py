"""REST + WebSocket routes implementing the wire protocol (docs/04-wire-protocol.md).

An **adapter** over :class:`~fenixspoon.core.FenixSpoonCore` (roadmap M2.5, issue #42): each
route reads the request, calls one core method, and shapes the reply. Validation,
authorization and the job lifecycle live in the core, and failures come back as domain
errors that :mod:`fenixspoon.http_errors` turns into status codes — so there is no
`raise HTTPException` here for anything a non-HTTP caller could also hit.

Two things stay in this file on purpose, because they *are* HTTP: the `401` challenge with
`WWW-Authenticate` (an authentication scheme, not an application rule) and every `url` in a
payload, since only this transport can say where its own routes live.
"""

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .auth import Principal, principal_from_request, principal_from_websocket
from .core import CoreError, FenixSpoonCore
from .core.discovery import (
    SECTIONS,
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
)
from .core.optimize import OptimizationReport, OptimizationRun
from .core.results import LEVELS, FieldQuery, FieldQueryResult, LeveledResult
from .core.studies import StudyReport, StudyRun
from .core.workspace import ObjectSummary, ObjectView
from .frames import frames_of
from .geometry import Geometry
from .jobs import JobStatus
from .protocol import PROTOCOL_VERSION, ProtocolVersion
from .solvers.base import SolverInfo

router = APIRouter(prefix="/api/v1")

# Every route takes this: it is the auth gate as much as the identity. Annotated rather
# than a `Depends(...)` default so the dependency is part of the type, not a mutable
# argument default.
CurrentPrincipal = Annotated[Principal, Depends(principal_from_request)]


class JobRequest(BaseModel):
    """What `POST /api/v1/jobs` accepts: a design reference, or an inline solve.

    The design form is protocol 1.10 (ADR 0002) and is the reason the object routes exist.
    A page that submits an inline geometry resends the whole outline on every iteration, can
    never hit the result cache on an unchanged one, and — the part that does not show until
    somebody asks — can never say *which design revision* a picture came from, because
    provenance can only name revisions the caller submitted.

    Sending both is refused rather than resolved by precedence, exactly as `job.submit`
    refuses it over JSON-RPC: a request carrying a design *and* a geometry holds two
    different intents, and honouring one of them silently produces a job whose inputs are
    not what the caller thinks they are.
    """

    design: str | None = Field(
        default=None,
        description=(
            "A `design` reference, `design:d-18`, optionally pinned. The design names its "
            "geometry, params and load cases, so the other fields are not used with it — "
            "and are refused rather than ignored."
        ),
    )
    solver: str | None = Field(
        default=None, description="A `name` from `GET /solvers`, for the inline form."
    )
    geometry: Geometry | None = Field(
        default=None,
        description="Geometry to solve on; its `type` must be one the solver accepts.",
    )
    params: dict[str, Any] = Field(
        default={}, description="Solver parameters, validated against that solver's schema."
    )
    conditions: dict[str, dict[str, float]] = Field(
        default={},
        description=(
            "An inline load case (protocol 1.9, #85): boundary name to the condition keys "
            "in force there. Every name must be one this geometry's `boundaries` declares, "
            "and every key one the capability declares in its `conditions` section — both "
            "are refused rather than ignored. Omit it to let the solver's own parameters "
            "govern, which is what every capability shipped before #85 does."
        ),
    )

    @model_validator(mode="after")
    def _one_form_only(self) -> "JobRequest":
        # `params` counts as the inline form, which it did not until a review of #21 asked
        # what happens to `{"design": ..., "params": {"resolution": 512}}`. The answer was
        # that the route dropped the parameters on the floor and solved the design as
        # written — the exact "a job whose inputs are not what the caller thinks they are"
        # this refusal exists to prevent, arrived at from the one direction nobody checked.
        inline = (
            self.solver is not None
            or self.geometry is not None
            or bool(self.conditions)
            or bool(self.params)
        )
        if self.design is not None and inline:
            raise ValueError(
                "send a design reference or an inline solve, not both: a request carrying "
                "each cannot say which one it means"
            )
        if self.design is None and (self.solver is None or self.geometry is None):
            raise ValueError("an inline solve needs both `solver` and `geometry`")
        return self


class JobCreated(BaseModel):
    """The 202 from `POST /api/v1/jobs`. The job has been accepted; it may already be done."""

    job_id: str = Field(description="Use it to poll status, stream events and fetch the result.")
    status: str = Field(
        description=(
            "`queued` for work that will run. **Since protocol 1.4 it can be `done` or "
            "`running` immediately**: an identical solve is answered from the result cache "
            "(#47), and what comes back is the job that already has the answer."
        )
    )
    cached: bool = Field(
        default=False,
        description=(
            "True when this submission was answered from an earlier identical solve rather "
            "than starting one. Added in protocol 1.4. Still a `202`: the submission was "
            "accepted, and giving it a different status code would be reusing a code to mean "
            "something new."
        ),
    )


class ObjectCreate(BaseModel):
    """What `POST /api/v1/objects/{type}` accepts (protocol 1.10, ADR 0002).

    The type is in the path rather than in the body, because it is what decides which
    schema `body` is validated against — putting it in both would create a request that can
    contradict itself.
    """

    body: dict[str, Any] = Field(
        description=(
            "The object itself. Validated against the schema for this type where one "
            "exists; `boundary_condition` is stored as given, and says why in the workspace "
            "documentation rather than pretending to a schema it does not have."
        )
    )
    label: str | None = Field(
        default=None, description="Optional human label for this revision."
    )


class ObjectPatch(BaseModel):
    """An RFC 6902 patch and an optional label for the revision it writes."""

    patch: list[dict[str, Any]] = Field(
        description=(
            "JSON Patch operations. Merge patch would replace arrays wholesale, which for a "
            "geometry means resending every control point — the cost the workspace exists to "
            "remove."
        )
    )
    label: str | None = Field(default=None, description="Optional label for the new revision.")


class ObjectRevisions(BaseModel):
    """Which revisions of one object exist."""

    ref: str = Field(description="The object, unpinned.")
    revisions: list[int] = Field(description="Every revision number, ascending.")


class JobList(BaseModel):
    """One page of job history, newest first."""

    jobs: list[JobStatus] = Field(description="This page of jobs, newest first.")
    total: int = Field(description="Total stored jobs for this principal, not just this page.")
    limit: int = Field(description="Page size that was applied.")
    offset: int = Field(description="Offset that was applied.")


def _core(request: Request) -> FenixSpoonCore:
    return request.app.state.core


@router.get("/version", response_model=ProtocolVersion)
def protocol_version() -> ProtocolVersion:
    """What this server speaks. **The one route outside the auth gate.**

    Every other route requires a key when keys are configured. This one must not: a client
    needs to know whether it can talk to a server *before* deciding what to send, and
    making version discovery require a credential means a misconfigured client cannot tell
    "wrong key" from "wrong protocol". It leaks two version strings and a path prefix,
    none of which is a secret — the same information the OpenAPI page already serves.
    """
    return ProtocolVersion(
        protocol=PROTOCOL_VERSION, implementation=__version__, api_path=router.prefix
    )


@router.get("/solvers", response_model=list[SolverInfo])
def list_solvers(
    request: Request,
    principal: CurrentPrincipal,  # noqa: ARG001 - present to gate the route
) -> list[SolverInfo]:
    """Which solvers this server has. Behind auth: it describes what the server can run."""
    return _core(request).capabilities()


@router.get("/environment", response_model=EnvironmentInfo)
def inspect_environment(
    request: Request,
    principal: CurrentPrincipal,
) -> EnvironmentInfo:
    """What this installation is: versions, backends, limits, and *your* quotas and usage.

    The HTTP binding of `environment.inspect` (roadmap M2.5, #43). Behind auth, unlike
    `/version`: the two version strings are not secret, but the data directory, the backend
    topology and another principal's quota position are not information to hand out for
    free.
    """
    return _core(request).environment(principal)


@router.get("/capabilities", response_model=list[CapabilitySummary])
def list_capabilities(
    request: Request,
    principal: CurrentPrincipal,  # noqa: ARG001 - present to gate the route
) -> list[CapabilitySummary]:
    """One line per installed capability — `capability.list`.

    The compact counterpart of `/solvers`, which stays exactly as it was: that route serves
    a form generator and must keep returning every schema. This one serves a caller choosing
    between capabilities, which is a different question and a much smaller answer.
    """
    return _core(request).capability_list()


@router.get(
    "/capabilities/{name}",
    response_model=CapabilityDescription,
    response_model_exclude_none=True,
)
def describe_capability(
    name: str,
    request: Request,
    principal: CurrentPrincipal,  # noqa: ARG001 - present to gate the route
    sections: Annotated[
        list[str] | None,
        Query(description=f"Sections to include. Any of: {', '.join(SECTIONS)}."),
    ] = None,
    inline_schemas: Annotated[
        bool,
        Query(description="Return the full params JSON Schema inline instead of a reference."),
    ] = False,
) -> CapabilityDescription:
    """Selected sections of one capability — `capability.describe`.

    Omit `sections` for everything; pass it to get those sections and nothing else.
    `response_model_exclude_none` is what makes "nothing else" literal — an unrequested
    section is absent from the JSON rather than present and null, which is the difference
    between a compact answer and a compact-looking one.
    """
    return _core(request).capability_describe(name, sections, inline_schemas=inline_schemas)


@router.get("/capabilities/{name}/schema")
def capability_schema(
    name: str,
    request: Request,
    principal: CurrentPrincipal,  # noqa: ARG001 - present to gate the route
) -> dict[str, Any]:
    """Resolve the `schema:params/{name}` reference from a `params` section.

    Same JSON Schema `/solvers` embeds, fetched deliberately rather than by default.
    """
    return _core(request).capability_schema(name)


# ------------------------------------------------------------ workspace objects (1.10)
#
# ADR 0002. The workspace has been reachable from a local process since #44 and from nothing
# else, on a deferral whose stated reason — that the transport it exists for might want a
# different shape — expired once JSON-RPC had carried that shape through five releases and
# three consumers. These routes are that shape bound to HTTP, and they call the same core
# methods `object.create` and friends call.
#
# A reference is `geometry:g-12`, and it is *not* a path segment: `{type}/{id}` is the pair
# the identifier decomposes into, so a page never percent-encodes a colon and this file
# rebuilds the canonical reference from its own path parameters. A caller cannot construct a
# URL whose halves disagree — `design/g-12` resolves to `design:g-12`, which does not exist,
# and 404s honestly.


def _ref(object_type: str, object_id: str, revision: int | None = None) -> str:
    """The canonical reference these two path segments name, optionally pinned."""
    return f"{object_type}:{object_id}" + (f"@{revision}" if revision is not None else "")


@router.get("/objects", response_model=list[ObjectSummary])
def list_objects(
    request: Request,
    principal: CurrentPrincipal,
    type: str | None = Query(default=None, description="Filter to one object type."),
) -> list[ObjectSummary]:
    """This principal's objects, newest first, without their bodies.

    Without them because a geometry body is the payload progressive iteration exists to stop
    moving, and a list of twenty designs would carry twenty outlines nobody asked for.
    """
    return _core(request).objects(principal, type)


@router.post("/objects/{object_type}", response_model=ObjectView, status_code=201)
def create_object(
    object_type: str,
    req: ObjectCreate,
    request: Request,
    principal: CurrentPrincipal,
) -> ObjectView:
    """Create an object of this type and return revision 1.

    `201` rather than the `202` jobs get: this *has* happened by the time the response is
    written, and there is nothing to poll.
    """
    return _core(request).create_object(object_type, req.body, principal, req.label)


@router.get("/objects/{object_type}/{object_id}", response_model=ObjectView)
def get_object(
    object_type: str,
    object_id: str,
    request: Request,
    principal: CurrentPrincipal,
    revision: int | None = Query(
        default=None,
        ge=1,
        description="A revision to pin to. Omit for the head, which is the usual case.",
    ),
) -> ObjectView:
    """One object: the head, or the exact revision asked for.

    The revision is a query parameter rather than a path segment because it is a *view* of
    the object at a point in its history, not a sub-resource with an identity of its own —
    so this route answers both, in one shape, exactly as the local API does.
    """
    return _core(request).object(_ref(object_type, object_id, revision), principal)


@router.get("/objects/{object_type}/{object_id}/revisions", response_model=ObjectRevisions)
def object_revisions(
    object_type: str,
    object_id: str,
    request: Request,
    principal: CurrentPrincipal,
) -> ObjectRevisions:
    ref = _ref(object_type, object_id)
    return ObjectRevisions(ref=ref, revisions=_core(request).object_revisions(ref, principal))


@router.patch("/objects/{object_type}/{object_id}", response_model=ObjectView)
def patch_object(
    object_type: str,
    object_id: str,
    req: ObjectPatch,
    request: Request,
    principal: CurrentPrincipal,
) -> ObjectView:
    """Apply an RFC 6902 patch and return the new revision.

    Objects are never mutated: this reads the head, applies the patch and writes the next
    revision, so the one it was computed from stays readable forever. Patching a *pinned*
    reference is refused rather than branched from — see `CannotPatchRevision`.
    """
    return _core(request).patch_object(
        _ref(object_type, object_id), req.patch, principal, req.label
    )


# ------------------------------------------------------------------ studies (1.11)
#
# ADR 0002 decision 3: a study is an object that *runs*, not an endpoint that computes. #21
# sketched `POST /sweeps` with the grid in the body; a sweep posted that way is a computation
# with no identity — it cannot be pinned, re-read a week later, re-run into the cache, or
# shown to a colleague. So the object is created like any other and these two routes act on
# it, which is also why there is no request body here: everything the run needs is in the
# revision it names.
#
# `?revision=` for the same reason it is on the object routes — and it is not decoration.
# "You cannot pin it" is the *first* thing the ADR holds against a sweep posted as a body, so
# a binding that could only ever act on the head would refute its own argument. The core has
# resolved a pinned reference since #44 and the other four transports pass one straight
# through; without this parameter HTTP alone could not say `study:s-1@2`.


@router.post("/studies/{study_id}/run", response_model=StudyRun, status_code=202)
async def run_study(
    study_id: str,
    request: Request,
    principal: CurrentPrincipal,
    revision: int | None = Query(default=None, ge=1),
) -> StudyRun:
    """Submit every variation of a study.

    `202`, and it returns as soon as the work is accepted — the same contract `POST /jobs`
    has, for the same reason: a five-rung ladder takes minutes and this is a request a
    browser is holding open. `reused` is worth reading on the way past, because it says how
    much of the ladder the result cache just made free.

    Pin with `?revision=` to run the study as it was written then. Worth having rather than
    worth documenting: a study's job list is a pure function of its revision, so re-running
    an old one is answered entirely from the cache and costs nothing — which is the property
    ADR 0002 argued a body-shaped sweep could never have.
    """
    return await _core(request).run_study(_ref("study", study_id, revision), principal)


@router.get("/studies/{study_id}", response_model=StudyReport)
def study_report(
    study_id: str,
    request: Request,
    principal: CurrentPrincipal,
    revision: int | None = Query(default=None, ge=1),
) -> StudyReport:
    """The (variation → metric) table, and what it means.

    Free to call before the run, during it, and after: there is no stored run record to be
    absent, because the study says what to solve and the jobs are the answer. A ladder adds
    where each metric settled; a sweep adds a response curve per metric as `Series1DData`,
    which is the model `<fs-plot>` already draws.

    `?revision=` reads the study as it was written then, and reports against *that* — which
    is how a table from last week stays readable after the grid has been widened twice.
    """
    return _core(request).study_report(_ref("study", study_id, revision), principal)


# ------------------------------------------------------------ optimizations (1.12)
#
# ADR 0002 decision 4, and the last of its four pieces. Same two-route shape as studies for
# the same reason — an optimization is an object that *runs* — with one difference that is
# the reason this piece was left until last: `optimize.run` used to block for the whole
# search, which is impossible here. It now returns a receipt on every transport, and the
# waiting moved to the callers that can afford it. The search keeps going as a task owned by
# the server process, which is the one genuinely new mechanism in the whole design.


@router.post(
    "/optimizations/{optimization_id}/run", response_model=OptimizationRun, status_code=202
)
async def run_optimization(
    optimization_id: str,
    request: Request,
    principal: CurrentPrincipal,
    revision: int | None = Query(default=None, ge=1),
) -> OptimizationRun:
    """Start the search. `202`, and it comes back in milliseconds.

    No job ids on the receipt, unlike a study's, and the absence is structural rather than an
    omission: a search cannot say which points it will evaluate, because each one is chosen
    from the last answer. `started: false` means a search for this revision was already in
    flight and this call joined it.
    """
    return await _core(request).run_optimization(
        _ref("optimization", optimization_id, revision), principal
    )


@router.get("/optimizations/{optimization_id}", response_model=OptimizationReport)
def optimization_report(
    optimization_id: str,
    request: Request,
    principal: CurrentPrincipal,
    revision: int | None = Query(default=None, ge=1),
) -> OptimizationReport:
    """The trajectory, the best point so far, the bracket, and why it stopped.

    Poll it while a search runs: it grows a row per evaluation and carries `running` while one
    is in flight in this process. There is no stored run record — the trajectory is rebuilt by
    replaying the method and resolving each point to its job — so this answers before a search
    has ever started, with `stopped: "not_run"`.
    """
    return _core(request).optimization_report(
        _ref("optimization", optimization_id, revision), principal
    )


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    req: JobRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> JobCreated:
    core = _core(request)
    if req.design is not None:
        # One core call, not a resolution step here: `submit_design` freezes the revisions it
        # resolved into the job's provenance, and doing that in a route would mean this
        # transport recording provenance a different way from the other four.
        job = await core.submit_design(req.design, principal)
    else:
        job = await core.submit(
            req.solver, req.geometry, req.params, principal, conditions=req.conditions
        )
    return JobCreated(job_id=job.id, status=job.status, cached=job.reused > 0)


@router.get("/jobs", response_model=JobList)
def list_jobs(
    request: Request,
    principal: CurrentPrincipal,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JobList:
    """This principal's job history, newest first. Survives restarts when persisted."""
    jobs, total = _core(request).history(principal, limit=limit, offset=offset)
    return JobList(
        jobs=[JobStatus.from_job(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=JobStatus)
def job_status(
    job_id: str,
    request: Request,
    principal: CurrentPrincipal,
) -> JobStatus:
    return JobStatus.from_job(_core(request).job(job_id, principal))


@router.post("/jobs/{job_id}/cancel", response_model=JobStatus, status_code=202)
async def cancel_job(
    job_id: str,
    request: Request,
    principal: CurrentPrincipal,
) -> JobStatus:
    return JobStatus.from_job(await _core(request).cancel(job_id, principal))


@router.get(
    "/jobs/{job_id}/summary",
    response_model=LeveledResult,
    response_model_exclude_none=True,
)
def job_summary(
    job_id: str,
    request: Request,
    principal: CurrentPrincipal,
    levels: Annotated[
        list[str] | None,
        Query(description=f"Levels to include. Any of: {', '.join(LEVELS)}."),
    ] = None,
) -> LeveledResult:
    """The compact counterpart of `/result` — protocol 1.3, issue #46.

    The same relationship `/capabilities` has to `/solvers`: the exhaustive route keeps its
    payload, and this one answers the question a caller usually has. **`fields` is not in the
    default**, so an answer is a kilobyte rather than a megabyte, and the arrays are reached
    deliberately — `?levels=fields`, the artifact, or `/query`.

    Paths, not URLs, on the artifacts here: this route reports what the core knows, and a
    caller that wants a download uses the artifact endpoint `/result` already advertises.
    """
    return _core(request).result_levels(job_id, principal, levels)


@router.post(
    "/jobs/{job_id}/query", response_model=FieldQueryResult, response_model_exclude_none=True
)
def job_query(
    job_id: str,
    query: FieldQuery,
    request: Request,
    principal: CurrentPrincipal,
) -> FieldQueryResult:
    """Ask one bounded question of one field — protocol 1.3, issue #46.

    `POST` rather than `GET` because the request is a structured object with a dozen optional
    arguments, not an identifier. It is a read: nothing is created, and repeating it changes
    nothing.
    """
    return _core(request).query_result(job_id, query, principal)


@router.get("/jobs/{job_id}/result")
def job_result(
    job_id: str,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """The full envelope, arrays included. Unchanged shape, plus what 1.3, 1.4 and 1.5 added.

    `metrics` and `diagnostics` are new keys here, which is additive; a client reading
    `data` and `stats` is untouched. What did change inside `stats` is that the heat
    adapters no longer put `t_max` and `t_rise` there — those were never costs, and they now
    appear under `metrics`. Permitted because `stats` keys have always been documented as
    server-defined and all optional.

    `series` is 1.5's addition (#69): the curves a solve produced beside its field, empty for
    a capability that produces none. A `series1d` result carries its curves in `data` instead,
    because that is what `kind` selects — so a consumer reads one place or the other, never
    both.
    """
    core = _core(request)
    result = core.result(job_id, principal)
    summary = core.job(job_id, principal).summary
    return {
        "job_id": result.job_id,
        "kind": result.kind,
        "data": result.data,
        "stats": result.stats,
        "metrics": dict(summary.metrics) if summary else {},
        "diagnostics": (
            {
                "converged": summary.converged,
                "residual": summary.residual,
                "iterations": summary.iterations,
                "warnings": summary.warnings,
            }
            if summary
            else {}
        ),
        "provenance": core.provenance(job_id, principal).model_dump(),
        # Always present on this route, unlike on `/summary`, where it is a level. This is the
        # exhaustive envelope: it already carries the field arrays, so withholding a bounded
        # curve from it would be a saving of nothing.
        "series": [entry.model_dump() for entry in summary.series] if summary else [],
        # The one place a URL is built. The core hands back a path; only this transport
        # knows that the file is reachable at a route it serves.
        #
        # Built by hand rather than by dumping a model, which is why `t` had to be added here
        # explicitly when protocol 1.7 introduced it — and why it was missed the first time.
        # A hand-built payload beside a declared model is a place the two can disagree, and
        # `test_the_result_route_carries_every_field_the_envelope_declares` now says so.
        "artifacts": [
            {
                "name": a.name,
                "content_type": a.content_type,
                "size": a.size,
                "url": f"{router.prefix}/jobs/{result.job_id}/artifacts/{a.name}",
                **({"t": a.t} if a.t is not None else {}),
            }
            for a in result.artifacts
        ],
        # Derived from the artifacts above, so this route cannot advertise an instant whose
        # file it is not serving (protocol 1.7, #86).
        "frames": [frame.model_dump() for frame in frames_of(result.artifacts)],
    }


@router.get("/jobs/{job_id}/artifacts/{name}")
def job_artifact(
    job_id: str,
    name: str,
    request: Request,
    principal: CurrentPrincipal,
) -> FileResponse:
    artifact = _core(request).artifact(job_id, name, principal)
    return FileResponse(artifact.path, media_type=artifact.content_type, filename=name)


@router.websocket("/jobs/{job_id}/events")
async def job_events(websocket: WebSocket, job_id: str) -> None:
    """Progress stream. Authenticate with ``?api_key=`` — a browser cannot put a header
    on a WebSocket handshake — or with the usual header from a non-browser client."""
    core: FenixSpoonCore = websocket.app.state.core
    principal = principal_from_websocket(websocket)
    if principal is None:
        # Closing *before* accept refuses the handshake itself: the ASGI server turns
        # this into an HTTP 403, so a browser's `onerror` fires and the socket never
        # opens. Accepting first and then closing would give the client a moment of
        # apparent success, which is worse than an outright refusal.
        await websocket.close(code=1008)
        return
    # A WebSocket cannot use the HTTP error handler — the handshake is already decided by
    # the time this runs — so it reports the domain error in-band instead.
    try:
        job = core.job(job_id, principal)
    except CoreError as exc:
        await websocket.accept()
        await websocket.send_json({"type": "error", "error": exc.detail})
        await websocket.close(code=4404)
        return
    await websocket.accept()
    try:
        async for event in core.events(job):
            await websocket.send_json(event)
        await websocket.close()
    except WebSocketDisconnect:
        pass
