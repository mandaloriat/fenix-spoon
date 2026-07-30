"""Conformance suite: the golden fixtures in protocol/fixtures/ must parse (or fail to
parse) against the pydantic protocol models, and live server output must round-trip
through the same models. The JS SDK (roadmap M2) consumes the identical fixture files."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from fenixspoon import series
from fenixspoon.api import JobRequest, router
from fenixspoon.core.discovery import (
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
)
from fenixspoon.core.results import LeveledResult
from fenixspoon.geometry import Geometry
from fenixspoon.protocol import (
    PROTOCOL_VERSION,
    ProgressEvent,
    ProtocolVersion,
    ResultEnvelope,
    StatusEvent,
)

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"

_event_adapter = TypeAdapter(ProgressEvent | StatusEvent)

VALIDATORS = {
    "geometries.json": TypeAdapter(Geometry).validate_python,
    "events.json": _event_adapter.validate_python,
    "results.json": TypeAdapter(ResultEnvelope).validate_python,
    "job-requests.json": TypeAdapter(JobRequest).validate_python,
    "version.json": TypeAdapter(ProtocolVersion).validate_python,
    # Protocol 1.2, issue #43. The JS suite's list is separate and does not include these:
    # discovery has no browser consumer yet, so there is no second implementation to drift
    # from. Each file's `$comment` says so, and says what changes when one appears.
    "environment.json": TypeAdapter(EnvironmentInfo).validate_python,
    "capability-summaries.json": TypeAdapter(CapabilitySummary).validate_python,
    "capability-descriptions.json": TypeAdapter(CapabilityDescription).validate_python,
    # Protocol 1.3, issue #46.
    "compact-results.json": TypeAdapter(LeveledResult).validate_python,
}


def test_the_corpus_and_the_server_agree_on_the_protocol_version():
    """The corpus names a version; the server must be speaking it.

    This is the tripwire that makes a bump deliberate. `PROTOCOL_VERSION` and the corpus
    are edited in the same commit or this fails — and because the SDK's conformance suite
    asserts the *same* corpus value against its own constant, changing either side alone
    goes red on the other. That is the whole mechanism: one number, three places, no way
    to move one of them quietly.
    """
    declared = json.loads((FIXTURES / "version.json").read_text())["protocol_version"]
    assert declared == PROTOCOL_VERSION, (
        f"protocol/fixtures/version.json says {declared}, the server says {PROTOCOL_VERSION}"
    )


def test_the_major_version_matches_the_path():
    """`/api/v1` is not decoration — it is the major version, and must stay in step."""
    major = PROTOCOL_VERSION.split(".")[0]
    assert router.prefix == f"/api/v{major}", (
        f"protocol {PROTOCOL_VERSION} is served under {router.prefix}"
    )


def test_the_corpus_and_the_server_agree_on_the_series_ceilings():
    """The other tripwire, on the same model as the version one.

    The three `MAX_SERIES_*` limits are mirrored into the SDK's `validate.ts`, because that file
    promises "where the server rejects, these reject". Mirroring with no check is how the two
    drift: lowering a limit here would leave the SDK accepting payloads this server refuses,
    which is the single failure mode the shared corpus exists to prevent. The SDK suite asserts
    against the same four numbers.
    """
    declared = json.loads((FIXTURES / "results.json").read_text())["series_limits"]
    assert declared["max_points_per_trace"] == series.MAX_SERIES_POINTS
    assert declared["max_traces_per_series"] == series.MAX_SERIES_TRACES
    assert declared["max_series_per_result"] == series.MAX_SERIES_PER_RESULT
    assert declared["max_total_points"] == series.MAX_SERIES_TOTAL_POINTS


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
