"""API-key auth, per-principal isolation, and quotas (#14).

Two properties matter more than the mechanics. **Auth must not be bypassable through a
side door**: every route is checked here, including the WebSocket, which is the one that
cannot carry an Authorization header. And **principals must not see each other's work**:
a job id from someone else's server session is a 404, not a 403 or a peek.
"""

import time

import pytest
from fastapi.testclient import TestClient

from fenixspoon.auth import Authenticator, Quotas, cors_origins, parse_api_keys
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

ALICE = "sk-alice-secret"
BOB = "sk-bob-secret"


@pytest.fixture()
def keyed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_API_KEYS", f"alice:{ALICE},bob:{BOB}")
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def open_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.delenv("FENIXSPOON_API_KEYS", raising=False)
    with TestClient(create_app()) as client:
        yield client


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def submit(client, key: str | None = None, params=FAST_PARAMS):
    return client.post(
        "/api/v1/jobs",
        json={"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": params},
        headers=auth(key) if key else {},
    )


def wait_terminal(client, job_id, key):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}", headers=auth(key)).json()
        if status["status"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


# ------------------------------------------------------------------ configuration


def test_api_key_parsing():
    assert parse_api_keys("alice:sk-a, bob:sk-b") == {"sk-a": "alice", "sk-b": "bob"}
    # A bare secret is its own principal — the one-off deployment that does not care who.
    assert parse_api_keys("sk-shared") == {"sk-shared": "sk-shared"}
    assert parse_api_keys("") == {}
    assert parse_api_keys("  ,  ") == {}
    # A trailing colon is a typo, not a key named "alice:" — refuse it loudly rather
    # than mint a guessable credential out of a config mistake.
    with pytest.raises(ValueError, match="empty API key"):
        parse_api_keys("alice:")
    with pytest.raises(ValueError, match="empty principal name"):
        parse_api_keys(":sk-nameless")


def test_anonymous_is_the_default(open_client):
    """Every demo in this repo depends on this: no keys configured, no auth."""
    assert open_client.get("/api/v1/solvers").status_code == 200
    assert submit(open_client).status_code == 202


def test_cors_defaults_follow_the_auth_mode(monkeypatch):
    monkeypatch.delenv("FENIXSPOON_CORS_ORIGINS", raising=False)
    assert cors_origins(auth_required=False) == ["*"]
    # Keys configured and origins unset: same-origin only, not wildcard-with-credentials.
    assert cors_origins(auth_required=True) == []
    monkeypatch.setenv("FENIXSPOON_CORS_ORIGINS", "https://app.example, https://b.example")
    assert cors_origins(auth_required=True) == ["https://app.example", "https://b.example"]


# ------------------------------------------------------------------ authentication


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/solvers"),
        ("get", "/api/v1/jobs"),
        ("get", "/api/v1/jobs/j-whatever"),
        ("get", "/api/v1/jobs/j-whatever/result"),
        ("get", "/api/v1/jobs/j-whatever/artifacts/solution.vtk"),
        ("post", "/api/v1/jobs/j-whatever/cancel"),
    ],
)
def test_every_route_refuses_an_unauthenticated_request(keyed_client, method, path):
    response = getattr(keyed_client, method)(path)
    assert response.status_code == 401, path
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_version_is_the_one_route_outside_the_gate(keyed_client):
    """Version discovery must work without a credential, and must stay the only exception.

    A client has to learn whether it can speak to a server *before* it can sensibly send
    anything, and if that needed a key then a misconfigured client could not distinguish
    "wrong key" from "wrong protocol". The route discloses two version strings and a path
    prefix — nothing that is not already on the OpenAPI page.

    The second half of this test is the guard that matters: it enumerates the routes and
    fails if a *new* one ever becomes reachable unauthenticated. Opening this door once was
    deliberate; opening it again by accident should not be possible quietly.
    """
    response = keyed_client.get("/api/v1/version")
    assert response.status_code == 200
    assert set(response.json()) == {"protocol", "implementation", "api_path"}

    from fenixspoon.api import router

    unauthenticated = set()
    for route in router.routes:
        path, methods = route.path, getattr(route, "methods", set())
        if "GET" not in methods or "{" in path:
            continue  # parametrised paths are covered above; WS has its own test
        if keyed_client.get(path).status_code != 401:
            unauthenticated.add(path)
    assert unauthenticated == {"/api/v1/version"}, (
        f"routes reachable without a key: {sorted(unauthenticated)}"
    )


def test_submit_refuses_an_unauthenticated_request(keyed_client):
    assert submit(keyed_client).status_code == 401


def test_a_wrong_key_is_a_401_not_a_500(keyed_client):
    assert submit(keyed_client, "sk-not-a-key").status_code == 401


def test_both_header_forms_are_accepted(keyed_client):
    assert keyed_client.get("/api/v1/solvers", headers={"X-API-Key": ALICE}).status_code == 200
    assert keyed_client.get("/api/v1/solvers", headers=auth(ALICE)).status_code == 200


def test_websocket_takes_the_key_in_the_query_string(keyed_client):
    """The browser path: a WebSocket handshake cannot carry an Authorization header."""
    job_id = submit(keyed_client, ALICE).json()["job_id"]
    wait_terminal(keyed_client, job_id, ALICE)

    with keyed_client.websocket_connect(
        f"/api/v1/jobs/{job_id}/events?api_key={ALICE}"
    ) as ws:
        while True:
            event = ws.receive_json()
            if event.get("type") == "status" and event["status"] == "done":
                break  # reaching the terminal event is the assertion


def test_websocket_without_a_key_is_closed_not_left_hanging(keyed_client):
    """Refused at the handshake, so the socket never opens.

    The close happens before ``accept()``. TestClient reports that as a disconnect with
    the 1008 close code; over a real ASGI server the same refusal is an HTTP 403 on the
    handshake, which is what a browser's ``onerror`` sees. Either way the client is told
    no rather than left holding a socket that will never deliver an event.
    """
    from starlette.websockets import WebSocketDisconnect

    job_id = submit(keyed_client, ALICE).json()["job_id"]
    with pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: PT012 - connect must be inside
        with keyed_client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008  # policy violation


def test_websocket_with_someone_elses_job_is_refused(keyed_client):
    from starlette.websockets import WebSocketDisconnect

    job_id = submit(keyed_client, ALICE).json()["job_id"]
    with keyed_client.websocket_connect(
        f"/api/v1/jobs/{job_id}/events?api_key={BOB}"
    ) as ws:
        assert ws.receive_json() == {"type": "error", "error": "job not found"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# ------------------------------------------------------------------ isolation


def test_principals_cannot_reach_each_others_jobs(keyed_client):
    job_id = submit(keyed_client, ALICE).json()["job_id"]
    wait_terminal(keyed_client, job_id, ALICE)

    # 404 rather than 403: confirming that an id exists is itself a leak.
    for path in (f"/api/v1/jobs/{job_id}", f"/api/v1/jobs/{job_id}/result"):
        assert keyed_client.get(path, headers=auth(BOB)).status_code == 404
    assert keyed_client.post(f"/api/v1/jobs/{job_id}/cancel", headers=auth(BOB)).status_code == 404
    assert (
        keyed_client.get(f"/api/v1/jobs/{job_id}/artifacts/solution.vtk", headers=auth(BOB))
    ).status_code == 404
    # And the owner is unaffected.
    assert keyed_client.get(f"/api/v1/jobs/{job_id}", headers=auth(ALICE)).status_code == 200


def test_history_is_scoped_to_the_principal(keyed_client):
    submit(keyed_client, ALICE)
    submit(keyed_client, ALICE)
    submit(keyed_client, BOB)

    alice = keyed_client.get("/api/v1/jobs", headers=auth(ALICE)).json()
    bob = keyed_client.get("/api/v1/jobs", headers=auth(BOB)).json()
    assert alice["total"] == 2
    assert bob["total"] == 1
    assert not {j["job_id"] for j in alice["jobs"]} & {j["job_id"] for j in bob["jobs"]}


# ------------------------------------------------------------------ quotas


def test_concurrent_job_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_MAX_CONCURRENT_JOBS", "1")
    with TestClient(create_app()) as client:
        first = submit(client, params=SLOW_PARAMS)
        assert first.status_code == 202
        second = submit(client, params=SLOW_PARAMS)
        assert second.status_code == 429
        assert "already running" in second.json()["detail"]

        # Freeing the slot frees the quota — it counts active jobs, not lifetime ones.
        client.post(f"/api/v1/jobs/{first.json()['job_id']}/cancel")
        wait_terminal(client, first.json()["job_id"], None)
        assert submit(client, params=FAST_PARAMS).status_code == 202


def test_hourly_job_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_MAX_JOBS_PER_HOUR", "2")
    with TestClient(create_app()) as client:
        for _ in range(2):
            job = submit(client)
            assert job.status_code == 202
            wait_terminal(client, job.json()["job_id"], None)
        refused = submit(client)
        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "3600"


def test_artifact_byte_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_MAX_ARTIFACT_BYTES", "1024")  # one VTK blows past this
    with TestClient(create_app()) as client:
        job = submit(client)
        assert job.status_code == 202
        wait_terminal(client, job.json()["job_id"], None)
        refused = submit(client)
        assert refused.status_code == 429
        assert "stored artifacts" in refused.json()["detail"]


def test_quotas_are_per_principal_not_global(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_API_KEYS", f"alice:{ALICE},bob:{BOB}")
    monkeypatch.setenv("FENIXSPOON_MAX_CONCURRENT_JOBS", "1")
    with TestClient(create_app()) as client:
        assert submit(client, ALICE, SLOW_PARAMS).status_code == 202
        assert submit(client, ALICE, SLOW_PARAMS).status_code == 429
        # Bob's slot is his own.
        bob_job = submit(client, BOB, SLOW_PARAMS)
        assert bob_job.status_code == 202
        for key in (ALICE, BOB):
            page = client.get("/api/v1/jobs", headers=auth(key)).json()
            for entry in page["jobs"]:
                client.post(f"/api/v1/jobs/{entry['job_id']}/cancel", headers=auth(key))


def test_unlimited_is_the_default(open_client):
    quotas = open_client.app.state.auth.quotas
    assert quotas == Quotas(concurrent_jobs=0, jobs_per_hour=0, artifact_bytes=0)
    for _ in range(3):
        assert submit(open_client, params=SLOW_PARAMS).status_code == 202
    for entry in open_client.get("/api/v1/jobs").json()["jobs"]:
        open_client.post(f"/api/v1/jobs/{entry['job_id']}/cancel")


def test_authenticator_is_replaceable(open_client):
    """The OIDC seam: swap app.state.auth and the API layer never notices."""
    from fenixspoon.auth import Principal

    class AlwaysBob(Authenticator):
        def principal(self, presented):  # noqa: ARG002 - the point is ignoring the key
            return Principal(id="bob", quotas=Quotas())

    open_client.app.state.auth = AlwaysBob(api_keys={})
    job_id = submit(open_client).json()["job_id"]
    manager = open_client.app.state.jobs
    assert manager.get(job_id).owner == "bob"
