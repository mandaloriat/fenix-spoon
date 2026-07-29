"""REST + WebSocket routes implementing wire protocol v0 (docs/04-wire-protocol.md)."""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .geometry import Geometry
from .jobs import JobManager, JobStatus
from .solvers import available_solvers, get_solver
from .solvers.base import SolverInfo

router = APIRouter(prefix="/api/v1")


class JobRequest(BaseModel):
    solver: str
    geometry: Geometry
    params: dict[str, Any] = {}


class JobCreated(BaseModel):
    job_id: str
    status: str


class JobList(BaseModel):
    """One page of job history, newest first."""

    jobs: list[JobStatus]
    total: int
    limit: int
    offset: int


def _manager(request: Request) -> JobManager:
    return request.app.state.jobs


@router.get("/solvers", response_model=list[SolverInfo])
def list_solvers() -> list[SolverInfo]:
    return available_solvers()


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(req: JobRequest, request: Request) -> JobCreated:
    solver_cls = get_solver(req.solver)
    if solver_cls is None:
        raise HTTPException(status_code=404, detail=f"unknown solver: {req.solver!r}")
    if req.geometry.type not in solver_cls.geometry_types:
        raise HTTPException(
            status_code=422,
            detail=(
                f"solver {req.solver!r} accepts geometry types "
                f"{solver_cls.geometry_types}, got {req.geometry.type!r}"
            ),
        )
    try:
        params = solver_cls.Params.model_validate(req.params)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc

    # Refuse work that would exhaust the box *before* accepting it: a clear rejection
    # beats a job killed halfway through by the wall-clock timeout.
    manager = _manager(request)
    estimate = solver_cls.estimate_cells(req.geometry, params)
    if estimate is not None and manager.max_cells and estimate > manager.max_cells:
        raise HTTPException(
            status_code=422,
            detail=(
                f"job would use about {estimate:,} cells, over this server's limit of "
                f"{manager.max_cells:,}. Lower the resolution or mesh size, or raise "
                "FENIXSPOON_MAX_CELLS."
            ),
        )

    job = await manager.submit(solver_cls, req.geometry, params)
    return JobCreated(job_id=job.id, status=job.status)


@router.get("/jobs", response_model=JobList)
def list_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JobList:
    """Job history, newest first. Survives restarts when a persistent store is configured."""
    jobs, total = _manager(request).list_jobs(limit=limit, offset=offset)
    return JobList(
        jobs=[JobStatus.from_job(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, request: Request) -> JobStatus:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus.from_job(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobStatus, status_code=202)
def cancel_job(job_id: str, request: Request) -> JobStatus:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not _manager(request).cancel(job):
        raise HTTPException(
            status_code=409, detail=f"job already finished (status: {job.status})"
        )
    return JobStatus.from_job(job)


@router.get("/jobs/{job_id}/result")
def job_result(job_id: str, request: Request) -> dict[str, Any]:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("failed", "cancelled"):
        detail = f"job {job.status}" + (f": {job.error}" if job.error else "")
        raise HTTPException(status_code=409, detail=detail)
    if job.status != "done" or job.result is None:
        raise HTTPException(status_code=409, detail=f"job not finished (status: {job.status})")
    return {
        "job_id": job.id,
        "kind": job.result.kind,
        "data": job.result.data,
        "stats": job.result.stats,
        "artifacts": [
            {**entry, "url": f"/api/v1/jobs/{job.id}/artifacts/{entry['name']}"}
            for entry in job.artifacts
        ],
    }


@router.get("/jobs/{job_id}/artifacts/{name}")
def job_artifact(job_id: str, name: str, request: Request) -> FileResponse:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Only names registered by the solver are servable — this, plus the bare-filename
    # rule enforced at registration, prevents any path traversal.
    entry = next((a for a in job.artifacts if a["name"] == name), None)
    if entry is None or job.artifact_dir is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = job.artifact_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file missing")
    return FileResponse(path, media_type=entry["content_type"], filename=name)


@router.websocket("/jobs/{job_id}/events")
async def job_events(websocket: WebSocket, job_id: str) -> None:
    manager: JobManager = websocket.app.state.jobs
    job = manager.get(job_id)
    await websocket.accept()
    if job is None:
        await websocket.send_json({"type": "error", "error": "job not found"})
        await websocket.close(code=4404)
        return
    try:
        async for event in manager.subscribe(job):
            await websocket.send_json(event)
        await websocket.close()
    except WebSocketDisconnect:
        pass
