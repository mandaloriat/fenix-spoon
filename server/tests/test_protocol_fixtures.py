"""Conformance suite: the golden fixtures in protocol/fixtures/ must parse (or fail to
parse) against the pydantic protocol models, and live server output must round-trip
through the same models. The JS SDK (roadmap M2) consumes the identical fixture files."""

import json
import re
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


#: Prose that states the protocol version, and the pattern that reads the number out of it.
#: Every entry is a place a *person* learns what this server speaks, which is exactly the kind
#: of claim nothing else checks — no code imports a markdown preamble.
#:
#: The list grew twice, and both times for the same reason, which is why it is worth keeping
#: the history: the CHANGELOG check was added *because* its number had drifted three minors
#: behind, and the README then drifted one commit later, in the very change that introduced the
#: check. A guard aimed at one file taught nothing about the others. The third entry is the
#: one that should have been first — **the wire-protocol document itself**, which said 1.5
#: while the server spoke 1.9, in both its opening sentence and the sample payload underneath
#: it. The comment above already said "anything that says the version in words belongs here";
#: what it missed is that the document *defining* the version is the most likely place for a
#: reader to take the number from, and was the only one nothing checked.
#:
#: Each entry is a list, because a file can state the version more than once and the sentence
#: is not always the copy a reader trusts — `04-wire-protocol.md` prints a whole `version`
#: payload, and someone reaching for an example reaches for that.
PROSE_CLAIMS = {
    "CHANGELOG.md": [r"`MAJOR\.MINOR`, currently ([0-9]+\.[0-9]+)\)"],
    # The documentation site's front page, added by the housekeeping pass that found it
    # describing a two-physics project with a protocol three minors old. It is the first page
    # a reader sees, which by the argument above makes it the *last* place a stale number
    # should be allowed to sit.
    "docs/index.md": [r"Wire protocol ([0-9]+\.[0-9]+) with a shared conformance corpus"],
    "README.md": [r"\*\*The wire protocol is at ([0-9]+\.[0-9]+)\.\*\*"],
    "docs/04-wire-protocol.md": [
        r"versioned `MAJOR\.MINOR`, currently \*\*([0-9]+\.[0-9]+)\*\*",
        r'\{ "protocol": "([0-9]+\.[0-9]+)", "implementation"',
    ],
}


@pytest.mark.parametrize("filename", sorted(PROSE_CLAIMS))
def test_the_prose_states_the_current_protocol(filename):
    """A file that tells a reader the protocol version must tell them the right one.

    Deliberately a regex over the sentence rather than a whole-line match: the prose around the
    number should stay free to change, and only the number is the claim being checked.
    """
    text = (FIXTURES.parents[1] / filename).read_text()
    for pattern in PROSE_CLAIMS[filename]:
        stated = re.search(pattern, text)
        assert stated is not None, (
            f"{filename} no longer states the protocol version in the form this test reads "
            f"({pattern}); restore the phrasing or update the pattern, but do not delete the "
            "claim"
        )
        assert stated.group(1) == PROTOCOL_VERSION, (
            f"{filename} says the protocol is {stated.group(1)}, "
            f"the server says {PROTOCOL_VERSION}"
        )


def test_the_wire_protocol_document_lists_the_whole_result_envelope():
    """The other claim in that document nothing was reading: what a result contains.

    It closes with "the result envelope is exactly what is documented above", followed by the
    field names — and it had been *exactly* wrong twice over, missing `provenance` from 1.4 and
    the derived `frames` from 1.7. A list that says "exactly" and is short by two is worse than
    no list: a reader building against it concludes the fields do not exist.

    Checked against the model rather than against a second hand-written list, since a second
    list is a second thing to forget. `frames` is a computed field, so it comes from
    `model_computed_fields` — and it is the one that drifted, which is the argument for reading
    both maps rather than the convenient one.
    """
    text = (FIXTURES.parents[1] / "docs" / "04-wire-protocol.md").read_text()
    sentence = re.search(
        r"the result envelope is exactly what is documented above:(.+?)\.\n", text, re.S
    )
    assert sentence is not None, (
        "docs/04-wire-protocol.md no longer closes with the envelope's field list; restore it "
        "or update this pattern, but do not drop the claim"
    )
    documented = set(re.findall(r"`([a-z_]+)`", sentence.group(1)))
    declared = set(ResultEnvelope.model_fields) | set(ResultEnvelope.model_computed_fields)
    assert documented == declared, (
        f"the document lists {sorted(documented)}; a result envelope carries {sorted(declared)}"
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


#: Every file that states the *implementation* version, as opposed to the protocol's.
#:
#: There are seven of them and until the 0.1.0 release nothing compared any two. That is the
#: same drift the block above guards against, one layer down: the protocol version had a test
#: because it had already drifted three minors, and the package version had none because
#: nothing had ever been released, so `0.1.0` was a number that could not be wrong yet.
#: Tagging it makes it a claim, and a claim gets a check.
#:
#: The four browser packages are here because the changelog says the packages are versioned
#: *together*; a client published at a version the server never had would make that sentence
#: false, and the sentence is what a consumer pins against.
VERSION_CLAIMS = {
    "server/pyproject.toml": r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"',
    "server/fenixspoon/__init__.py": r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"',
    "client/packages/client/package.json": r'"version": "([0-9]+\.[0-9]+\.[0-9]+)"',
    "client/packages/geometry-2d/package.json": r'"version": "([0-9]+\.[0-9]+\.[0-9]+)"',
    "client/packages/viewer/package.json": r'"version": "([0-9]+\.[0-9]+\.[0-9]+)"',
    "client/packages/plot/package.json": r'"version": "([0-9]+\.[0-9]+\.[0-9]+)"',
}


@pytest.mark.parametrize("filename", sorted(VERSION_CLAIMS))
def test_every_package_states_the_implementation_version(filename):
    """The version `environment.inspect` reports is the version every package declares."""
    from fenixspoon import __version__

    text = (FIXTURES.parents[1] / filename).read_text()
    found = re.search(VERSION_CLAIMS[filename], text, re.MULTILINE)
    assert found, f"{filename} states no package version"
    assert found.group(1) == __version__, (
        f"{filename} says {found.group(1)}, but fenixspoon.__version__ is {__version__}"
    )


def test_the_changelog_has_a_section_for_the_released_version():
    """A released version needs an entry, and an entry needs a date rather than a placeholder.

    The whole file was one `Unreleased` block across sixteen protocol minors before 0.1.0,
    which is the failure this guards: not a wrong heading but a missing one, and a missing one
    is invisible until someone goes looking for what a version contains.
    """
    from fenixspoon import __version__

    text = (FIXTURES.parents[1] / "CHANGELOG.md").read_text()
    heading = re.search(rf"^## {re.escape(__version__)} — (\d{{4}}-\d{{2}}-\d{{2}})$", text, re.M)
    assert heading, f"CHANGELOG.md has no dated `## {__version__}` section"
    assert text.index("## Unreleased") < heading.start(), "Unreleased must stay above the releases"
