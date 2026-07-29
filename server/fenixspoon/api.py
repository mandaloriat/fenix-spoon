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
    """The 202 from `POST /api/v1/jobs`. The job has been accepted, not finished."""

    job_id: str = Field(description="Use it to poll status, stream events and fetch the result.")
    status: str = Field(description="Always `queued` at this point.")


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


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    req: JobRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> JobCreated:
    job = await _core(request).submit(req.solver, req.geometry, req.params, principal)
    return JobCreated(job_id=job.id, status=job.status)


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


@router.get("/jobs/{job_id}/result")
def job_result(
    job_id: str,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    result = _core(request).result(job_id, principal)
    return {
        "job_id": result.job_id,
        "kind": result.kind,
        "data": result.data,
        "stats": result.stats,
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
