"""Conformance suite: the golden fixtures in protocol/fixtures/ must parse (or fail to
parse) against the pydantic protocol models, and live server output must round-trip
through the same models. The JS SDK (roadmap M2) consumes the identical fixture files."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from fenixspoon.api import JobRequest
from fenixspoon.geometry import Domain2D
from fenixspoon.protocol import ProgressEvent, ResultEnvelope, StatusEvent

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"

_event_adapter = TypeAdapter(ProgressEvent | StatusEvent)

VALIDATORS = {
    "geometries.json": TypeAdapter(Domain2D).validate_python,
    "events.json": _event_adapter.validate_python,
    "results.json": TypeAdapter(ResultEnvelope).validate_python,
    "job-requests.json": TypeAdapter(JobRequest).validate_python,
}


def _cases(kind: str):
    for filename, validate in VALIDATORS.items():
        corpus = json.loads((FIXTURES / filename).read_text())
        for case in corpus[kind]:
            yield pytest.param(validate, case, id=f"{filename}::{case['name']}")


@pytest.mark.parametrize(("validate", "case"), _cases("valid"))
def test_valid_fixture_parses(validate, case):
    validate(case["payload"])


@pytest.mark.parametrize(("validate", "case"), _cases("invalid"))
def test_invalid_fixture_rejected(validate, case):
    with pytest.raises(ValidationError):
        validate(case["payload"])


def test_live_results_conform(tmp_path, monkeypatch):
    """Whatever the server actually returns must parse with the protocol models."""
    import time

    from fastapi.testclient import TestClient

    from fenixspoon.main import create_app

    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    geometry = json.loads((FIXTURES / "geometries.json").read_text())["valid"][0]["payload"]
    with TestClient(create_app()) as client:
        for output in ("grid2d", "mesh2d"):
            job_id = client.post(
                "/api/v1/jobs",
                json={
                    "solver": "mock.laplace2d",
                    "geometry": geometry,
                    "params": {"resolution": 32, "iterations": 50, "output": output},
                },
            ).json()["job_id"]
            deadline = time.monotonic() + 15
            while client.get(f"/api/v1/jobs/{job_id}").json()["status"] != "done":
                assert time.monotonic() < deadline, "job did not finish"
                time.sleep(0.05)
            envelope = ResultEnvelope.model_validate(
                client.get(f"/api/v1/jobs/{job_id}/result").json()
            )
            assert envelope.kind == output
            with client.websocket_connect(f"/api/v1/jobs/{job_id}/events") as ws:
                while True:
                    event = _event_adapter.validate_python(ws.receive_json())
                    if isinstance(event, StatusEvent) and event.status in (
                        "done",
                        "failed",
                        "cancelled",
                    ):
                        break
