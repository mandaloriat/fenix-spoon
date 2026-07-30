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
from pydantic import BaseModel, Field

from . import __version__
from .auth import Principal, principal_from_request, principal_from_websocket
from .core import CoreError, FenixSpoonCore
from .core.discovery import (
    SECTIONS,
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
)
from .core.results import LEVELS, FieldQuery, FieldQueryResult, LeveledResult
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
    """What `POST /api/v1/jobs` accepts."""

    solver: str = Field(description="A `name` from `GET /solvers`.")
    geometry: Geometry = Field(
        description="Geometry to solve on; its `type` must be one the solver accepts."
    )
    params: dict[str, Any] = Field(
        default={}, description="Solver parameters, validated against that solver's schema."
    )


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


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    req: JobRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> JobCreated:
    job = await _core(request).submit(req.solver, req.geometry, req.params, principal)
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
        "artifacts": [
            {
                "name": a.name,
                "content_type": a.content_type,
                "size": a.size,
                "url": f"{router.prefix}/jobs/{result.job_id}/artifacts/{a.name}",
            }
            for a in result.artifacts
        ],
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
