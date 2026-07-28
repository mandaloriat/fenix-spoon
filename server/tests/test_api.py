import time

import pytest
from fastapi.testclient import TestClient

from fenixspoon.main import create_app

GEOMETRY = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
FAST_PARAMS = {"resolution": 32, "iterations": 100, "report_every": 20}


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def submit(client, **overrides):
    body = {"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": FAST_PARAMS}
    body.update(overrides)
    return client.post("/api/v1/jobs", json=body)


def wait_done(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()
        if status["status"] in ("done", "failed"):
            return status
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def test_solvers_listed(client):
    solvers = client.get("/api/v1/solvers").json()
    names = {s["name"] for s in solvers}
    assert "mock.laplace2d" in names
    mock = next(s for s in solvers if s["name"] == "mock.laplace2d")
    assert "resolution" in mock["params_schema"]["properties"]


def test_job_lifecycle_and_result(client):
    resp = submit(client)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = wait_done(client, job_id)
    assert status["status"] == "done"

    result = client.get(f"/api/v1/jobs/{job_id}/result")
    assert result.status_code == 200
    payload = result.json()
    assert payload["kind"] == "grid2d"
    ny, nx = payload["data"]["shape"]
    assert len(payload["data"]["fields"]["psi"]) == ny * nx


def test_event_stream_replays_history(client):
    job_id = submit(client).json()["job_id"]
    wait_done(client, job_id)
    # Subscribe after completion: full history must be replayed, ending in a terminal event.
    with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as ws:
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event.get("type") == "status" and event.get("status") in ("done", "failed"):
                break
    assert events[0] == {"type": "status", "status": "running"}
    assert any(e["type"] == "progress" for e in events)
    assert events[-1]["status"] == "done"


def test_unknown_solver_404(client):
    assert submit(client, solver="nope.nope").status_code == 404


def test_invalid_params_422(client):
    assert submit(client, params={"resolution": 4}).status_code == 422


def test_invalid_geometry_422(client):
    bad = dict(GEOMETRY, obstacle={"type": "polygon2d", "points": [[0, 0], [1, 1]]})
    assert submit(client, geometry=bad).status_code == 422


def test_result_before_done_409(client):
    job_id = submit(client, params={"resolution": 256, "iterations": 20000}).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}/result").status_code == 409
