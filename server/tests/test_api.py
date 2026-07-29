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
SLOW_PARAMS = {"resolution": 256, "iterations": 20000, "report_every": 100}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def capped_client(tmp_path, monkeypatch):
    """A client factory whose server is built with a given ``FENIXSPOON_MAX_CELLS``.

    The budget is read when the app is created, so it has to be set before ``create_app``
    rather than per-request.
    """
    from contextlib import ExitStack

    stack = ExitStack()

    def make(max_cells: int):
        monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
        monkeypatch.setenv("FENIXSPOON_MAX_CELLS", str(max_cells))
        return stack.enter_context(TestClient(create_app()))

    with stack:
        yield make


def submit(client, **overrides):
    body = {"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": FAST_PARAMS}
    body.update(overrides)
    return client.post("/api/v1/jobs", json=body)


def wait_terminal(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()
        if status["status"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def test_client_packages_are_mounted_when_built(client):
    """The widget demo loads the packages from /packages via an import map, so a built
    checkout must serve them — and an unbuilt one must serve nothing rather than a
    half-loaded page."""
    from fenixspoon.main import _CLIENT_PACKAGES_DIR

    built = [p.parent.name for p in sorted(_CLIENT_PACKAGES_DIR.glob("*/dist")) if p.is_dir()]
    assert client.app.state.client_packages == built
    for name in built:
        assert client.get(f"/packages/{name}/index.js").status_code == 200
    assert client.get("/packages/nope/index.js").status_code == 404


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

    status = wait_terminal(client, job_id)
    assert status["status"] == "done"

    result = client.get(f"/api/v1/jobs/{job_id}/result")
    assert result.status_code == 200
    payload = result.json()
    assert payload["kind"] == "grid2d"
    ny, nx = payload["data"]["shape"]
    assert len(payload["data"]["fields"]["psi"]) == ny * nx


def test_mesh2d_result(client):
    job_id = submit(client, params={**FAST_PARAMS, "output": "mesh2d"}).json()["job_id"]
    assert wait_terminal(client, job_id)["status"] == "done"
    payload = client.get(f"/api/v1/jobs/{job_id}/result").json()
    assert payload["kind"] == "mesh2d"
    assert len(payload["data"]["triangles"]) > 0
    assert len(payload["data"]["point_fields"]["psi"]) == len(payload["data"]["points"])


def test_artifact_listed_and_downloadable(client):
    job_id = submit(client).json()["job_id"]
    wait_terminal(client, job_id)
    payload = client.get(f"/api/v1/jobs/{job_id}/result").json()
    arts = payload["artifacts"]
    assert [a["name"] for a in arts] == ["solution.vtk"]
    resp = client.get(arts[0]["url"])
    assert resp.status_code == 200
    assert resp.text.startswith("# vtk DataFile")
    # Unregistered names must 404 (also covers traversal attempts).
    assert client.get(f"/api/v1/jobs/{job_id}/artifacts/nope.vtk").status_code == 404


def test_cancel_running_job(client):
    job_id = submit(client, params=SLOW_PARAMS).json()["job_id"]
    resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert resp.status_code == 202
    status = wait_terminal(client, job_id)
    assert status["status"] == "cancelled"
    assert client.get(f"/api/v1/jobs/{job_id}/result").status_code == 409
    # Cancelling a finished job is a 409.
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 409


def test_event_stream_replays_history(client):
    job_id = submit(client).json()["job_id"]
    wait_terminal(client, job_id)
    # Subscribe after completion: full history must be replayed, ending in a terminal event.
    with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as ws:
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event.get("type") == "status" and event.get("status") in (
                "done",
                "failed",
                "cancelled",
            ):
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
    job_id = submit(client, params=SLOW_PARAMS).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}/result").status_code == 409
    client.post(f"/api/v1/jobs/{job_id}/cancel")  # don't leave the worker running


def test_result_reports_mesh_statistics(client):
    """A finished job says what it actually cost, so a user can see why it was slow."""
    from fenixspoon.geometry import Domain2D
    from fenixspoon.solvers.mock_laplace import MockLaplace2D

    job_id = submit(client).json()["job_id"]
    wait_terminal(client, job_id)
    payload = client.get(f"/api/v1/jobs/{job_id}/result").json()
    stats = payload["stats"]

    ny, nx = payload["data"]["shape"]
    assert stats["cells"] == ny * nx
    assert stats["iterations"] > 0
    assert stats["seconds"] > 0.0  # added by the job manager, not the solver

    # The submit-time estimate must agree with what the solve really used, or the cap
    # is policing a number nobody pays.
    estimate = MockLaplace2D.estimate_cells(
        Domain2D.model_validate(GEOMETRY), MockLaplace2D.Params(**FAST_PARAMS)
    )
    assert estimate == stats["cells"]


def test_oversize_job_refused_at_submit(capped_client):
    """Over-budget work is refused with an actionable 422 rather than started and killed."""
    client = capped_client(100)  # FAST_PARAMS asks for 21*32 = 672
    resp = submit(client)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "672" in detail and "100" in detail
    assert "FENIXSPOON_MAX_CELLS" in detail  # tells the operator how to raise it


def test_within_budget_job_accepted(capped_client):
    client = capped_client(10_000)
    assert submit(client).status_code == 202


def test_zero_max_cells_disables_the_cap(capped_client):
    client = capped_client(0)
    assert submit(client).status_code == 202


def test_solver_without_an_estimate_is_admitted(capped_client, monkeypatch):
    """``estimate_cells`` returning None means 'cannot say' — the job runs and the
    wall-clock timeout stays the backstop."""
    from fenixspoon.solvers.mock_laplace import MockLaplace2D

    client = capped_client(1)
    assert submit(client).status_code == 422  # the cap is genuinely active
    monkeypatch.setattr(MockLaplace2D, "estimate_cells", classmethod(lambda cls, g, p: None))
    assert submit(client).status_code == 202
