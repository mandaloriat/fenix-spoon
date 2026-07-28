"""REST + WebSocket routes implementing wire protocol v0 (docs/04-wire-protocol.md)."""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from .geometry import Domain2D
from .jobs import JobManager, JobStatus
from .solvers import available_solvers, get_solver
from .solvers.base import SolverInfo

router = APIRouter(prefix="/api/v1")


class JobRequest(BaseModel):
    solver: str
    geometry: Domain2D
    params: dict[str, Any] = {}


class JobCreated(BaseModel):
    job_id: str
    status: str


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
    try:
        params = solver_cls.Params.model_validate(req.params)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    job = await _manager(request).submit(solver_cls, req.geometry, params)
    return JobCreated(job_id=job.id, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, request: Request) -> JobStatus:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus.from_job(job)


@router.get("/jobs/{job_id}/result")
def job_result(job_id: str, request: Request) -> dict[str, Any]:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status == "failed":
        raise HTTPException(status_code=409, detail=f"job failed: {job.error}")
    if job.status != "done" or job.result is None:
        raise HTTPException(status_code=409, detail=f"job not finished (status: {job.status})")
    return {
        "job_id": job.id,
        "kind": job.result.kind,
        "data": job.result.data,
        "artifacts": job.result.artifacts,
    }


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
