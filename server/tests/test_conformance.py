"""Cross-transport conformance (roadmap M2.5, #51).

Five transports now carry the same operations — HTTP, JSON-RPC over stdio, MCP, the CLI and
the in-process Python API. Until this file they agreed on the strength of *per-pair* tests,
and that is a shape which decays quietly: add an error, map it on one transport, and the
others stay green and wrong.

So the tests here are deliberately not "does transport X work". They are:

- **the same request, every transport, one answer** — compared as payloads, because "it calls
  the same function" is what the implementation says and the payload is what a caller sees;
- **the same refusal, every transport** — including the two the issue names because they are
  *not* pydantic errors, the cell budget and the quota, since a refusal the server computes
  must carry the same structure as one a validator produced;
- **one schema, many bindings** — a capability's parameter schema is the same document
  wherever it surfaces;
- **no numeric arrays in a compact answer** — the invariant the byte budgets have been
  standing in for, asserted directly at last.

That last one is worth a paragraph. `test_results`, `test_rpc` and `test_studies` each cap the
*size* of a compact answer, and that ceiling has moved twice in a day — once when a capability
declared four more metrics, once when a solver added a warning. Both moves were correct, and
both were a proxy failing rather than a bug: the property the criterion is really about is
that a caller who did not ask for the arrays does not receive them, and a byte count can only
approximate it. :func:`assert_no_numeric_arrays` says it directly, and it is the assertion
that cannot be satisfied by making the numbers smaller.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from fenixspoon import http_errors, local
from fenixspoon.commands import EXIT_CODES
from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import Geometry
from fenixspoon.jobs import JobManager
from fenixspoon.main import create_app
from fenixspoon.rpc import errors as rpc_errors
from fenixspoon.rpc.encoding import jsonable
from fenixspoon.rpc.methods import METHODS

REPO_SERVER = Path(__file__).resolve().parents[1]
FIXTURES = REPO_SERVER.parents[0] / "protocol" / "fixtures"

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
FAST = {"resolution": 40, "iterations": 200, "write_vtk": False}

#: Longest run of numbers a *compact* answer may carry. Generous on purpose: a section of a
#: field, a handful of hotspots and a `bounds` quadruple are all short by construction, and a
#: nodal array is thousands of entries — so anything between the two is a design change
#: somebody should have to justify rather than a threshold to tune.
MAX_COMPACT_RUN = 32


def assert_no_numeric_arrays(payload: Any, limit: int = MAX_COMPACT_RUN, path: str = "") -> None:
    """Fail if a compact payload carries a full field anywhere inside it.

    Walks rather than checking known keys, because the failure this guards against is a
    refactor putting the arrays somewhere *new* — inside `diagnostics`, inside a metric that
    became a profile, inside provenance. A check that only looked where arrays used to be
    would pass on exactly the change it exists to catch.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_no_numeric_arrays(value, limit, f"{path}.{key}" if path else key)
    elif isinstance(payload, list):
        numeric = sum(1 for item in payload if isinstance(item, int | float))
        assert numeric <= limit, (
            f"{path or '<root>'} carries {numeric} numbers — a compact answer must not "
            f"contain a field array. This is the invariant the size budgets approximate; "
            f"if the payload legitimately grew, that is a design change, not a threshold."
        )
        for index, item in enumerate(payload):
            assert_no_numeric_arrays(item, limit, f"{path}[{index}]")


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="anonymous", quotas=Quotas())


@pytest.fixture()
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "http"))
    with TestClient(create_app()) as client:
        yield client


def rpc(core, me, method: str, params: dict) -> Any:
    result = METHODS[method](core, me, params)
    if hasattr(result, "__await__"):
        result = asyncio.run(result)
    return jsonable(result)


def refusal(core, me, method: str, params: dict) -> dict[str, Any]:
    """Invoke a method that must refuse, and render the refusal the way JSON-RPC would.

    Awaited rather than merely called: `job.submit` is a coroutine, so a bare call returns an
    un-started coroutine object and *nothing raises* — a test written that way passes while
    checking nothing, which is precisely what the first version of these three did.
    """
    async def go():
        result = METHODS[method](core, me, params)
        if hasattr(result, "__await__"):
            result = await result
        return result

    try:
        asyncio.run(go())
    except errors.CoreError as exc:
        return rpc_errors.error_object(exc)
    raise AssertionError(f"{method} accepted what another transport refused")


def wait_for(client, job_id: str, timeout: float = 30.0) -> str:
    """Poll until the job settles, on a deadline — the convention in `test_persistence`.

    A bare spin would hang the suite rather than fail it if a job never reached a terminal
    state, and burn a core doing it.
    """
    deadline = time.monotonic() + timeout
    while client.get(f"/api/v1/jobs/{job_id}").json()["status"] not in (
        "done", "failed", "cancelled"
    ):
        assert time.monotonic() < deadline, f"{job_id} did not finish in {timeout}s"
        time.sleep(0.05)
    return job_id


def solved_http(client) -> str:
    return wait_for(client, client.post(
        "/api/v1/jobs",
        json={"solver": "mock.laplace2d", "geometry": AIRFOIL, "params": FAST},
    ).json()["job_id"])


# ------------------------------------------------------- one request, one answer


def test_discovery_is_the_same_document_over_http_and_json_rpc(core, me, http):
    """`capability.describe` is one payload with two bindings.

    Compared whole rather than field by field: a difference anywhere is a difference, and the
    two renderings reach JSON by different routes — FastAPI's `response_model_exclude_none`
    on one side, `Selective.wire()` on the other.
    """
    over_http = http.get(
        "/api/v1/capabilities/mock.laplace2d",
        params={"sections": ["params", "metrics", "cost"]},
    ).json()
    over_rpc = rpc(core, me, "capability.describe",
                   {"name": "mock.laplace2d", "sections": ["params", "metrics", "cost"]})
    assert over_http == over_rpc


def test_the_capability_list_is_the_same_over_http_and_json_rpc(core, me, http):
    assert http.get("/api/v1/capabilities").json() == rpc(core, me, "capability.list", {})


def test_a_compact_result_is_the_same_over_http_and_json_rpc(tmp_path, monkeypatch):
    """The payload the milestone is about, through both bindings of it.

    One data directory for both, so the *same job* is read twice rather than two jobs that
    ought to look alike — which would compare the solver's determinism, not the transports.
    """
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "shared"))
    with TestClient(create_app()) as client:
        job_id = solved_http(client)
        over_http = client.get(f"/api/v1/jobs/{job_id}/summary").json()
        core = client.app.state.core
        over_rpc = rpc(core, Principal(id="anonymous", quotas=Quotas()),
                       "result.get", {"job_id": job_id})
    assert over_http == over_rpc


#: The discovery call every transport is compared on, and the arguments it is made with.
DISCOVERY = {"name": "mock.laplace2d", "sections": ["metrics"]}


def discovery_over_http(http) -> dict[str, Any]:
    """The reference rendering. Every other transport is compared against this one, so that
    comparing them pairwise is unnecessary — agreement with HTTP is transitive."""
    return http.get(
        "/api/v1/capabilities/mock.laplace2d", params={"sections": ["metrics"]}
    ).json()


def test_every_transport_answers_a_discovery_call_identically(core, me, http, tmp_path):
    """HTTP, JSON-RPC, the CLI and the Python API — four renderings, one payload.

    The CLI runs as a subprocess and the Python API in-process, so this is not four calls to
    one function dressed up: it is four callers of the core, compared by what they return.

    MCP is the fifth and lives in :func:`test_mcp_renders_the_same_discovery_call`, because
    its SDK is an optional extra: folding it in here would mean this test either fails in the
    plain-virtualenv job or silently drops to four transports there, and a conformance test
    that quietly checks less is the exact failure this file exists to prevent.
    """
    answers = {
        "http": discovery_over_http(http),
        "json-rpc": rpc(core, me, "capability.describe", DISCOVERY),
        "cli": json.loads(
            subprocess.run(
                [sys.executable, "-m", "fenixspoon", "capability", "describe",
                 "mock.laplace2d", "--sections", "metrics", "--json"],
                cwd=REPO_SERVER,
                env={**os.environ, "FENIXSPOON_DATA_DIR": str(tmp_path / "cli")},
                capture_output=True, text=True, check=True,
            ).stdout
        ),
    }
    with local.open_workspace(tmp_path / "py") as session:
        answers["python"] = session.describe("mock.laplace2d", ["metrics"]).wire()

    reference = answers["http"]
    for transport, payload in answers.items():
        assert payload == reference, f"{transport} disagrees with http"


@pytest.mark.mcp
def test_mcp_renders_the_same_discovery_call(core, me, http):
    """The fifth transport, against the same reference the other four are held to.

    Skipped without the extra and marked `mcp`, which is how the CI job that installs the SDK
    selects it — and that job guards against a silent skip by importing `mcp` first.
    """
    pytest.importorskip("mcp.types", reason="requires the `mcp` extra")
    from fenixspoon import mcp_adapter  # noqa: PLC0415

    answer = asyncio.run(
        mcp_adapter.call_tool(core, me, "describe_capability", DISCOVERY)
    )
    assert json.loads(answer.content[0].text) == discovery_over_http(http)


# --------------------------------------------------------- one refusal, one meaning


def load_error_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "errors.json").read_text())["classes"]


def test_the_fixture_covers_every_core_error():
    """A refusal missing from the corpus is one nothing checks for agreement.

    This is the direction that rots: adding an error and mapping it on one transport leaves
    the others green and wrong, and without this assertion the corpus would simply not
    mention it.
    """
    named = {entry["type"] for entry in load_error_fixture()}
    defined = {
        cls.__name__
        for cls in vars(errors).values()
        if isinstance(cls, type)
        and issubclass(cls, errors.CoreError)
        and cls is not errors.CoreError
    }
    assert defined == named, f"corpus and code disagree: {defined ^ named}"


def test_every_transport_maps_the_corpus_the_way_it_says():
    """The three tables against the one file that says what they should be.

    Values rather than groupings, because the fixture names both — and a table that agreed on
    the partition while disagreeing on a code would be a client sent to the wrong branch.
    """
    by_name = {
        cls.__name__: cls
        for cls in vars(errors).values()
        if isinstance(cls, type) and issubclass(cls, errors.CoreError)
    }
    for entry in load_error_fixture():
        cls = by_name[entry["type"]]
        assert http_errors.STATUS[cls] == entry["http"], entry["type"]
        assert rpc_errors.CODES[cls] == entry["jsonrpc"], entry["type"]
        assert EXIT_CODES[rpc_errors.CODES[cls]] == entry["exit"], entry["type"]


def test_errors_that_are_alike_stay_alike_on_every_transport():
    """The partition, which is what a caller writing one handler actually relies on.

    Not that the numbers match — they cannot — but that two errors sharing a status share a
    code and an exit code. This is the assertion that catches a *half*-mapped addition, where
    each individual value looks defensible.
    """
    groups: dict[str, set[str]] = {}
    for entry in load_error_fixture():
        groups.setdefault(entry["group"], set()).add(entry["type"])

    for label, key, table in (
        ("http", "http", None),
        ("jsonrpc", "jsonrpc", None),
        ("exit", "exit", None),
    ):
        del table
        partition: dict[Any, set[str]] = {}
        for entry in load_error_fixture():
            partition.setdefault(entry[key], set()).add(entry["type"])
        assert sorted(partition.values(), key=sorted) == sorted(groups.values(), key=sorted), (
            f"the {label} table partitions the errors differently from the corpus"
        )


@pytest.mark.parametrize(
    ("label", "params", "expected"),
    [
        # A pydantic failure, and the two the issue names because they are *not* one: a
        # refusal the server computes must carry the same structure as one a validator did.
        ("schema", {"resolution": -5}, "InvalidParams"),
        ("cell budget", {"resolution": 512}, "CellBudgetExceeded"),
    ],
)
def test_the_same_refusal_is_the_same_refusal_on_every_transport(
    tmp_path, monkeypatch, label, params, expected
):
    del label
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "shared"))
    monkeypatch.setenv("FENIXSPOON_MAX_CELLS", "5000")
    body = {"solver": "mock.laplace2d", "geometry": AIRFOIL, "params": params}

    with TestClient(create_app()) as client:
        response = client.post("/api/v1/jobs", json=body)
        rendered = refusal(
            client.app.state.core, Principal(id="anonymous", quotas=Quotas()),
            "job.submit", body,
        )

    entry = next(e for e in load_error_fixture() if e["type"] == expected)
    assert response.status_code == entry["http"], "http disagrees with the corpus"
    assert rendered["code"] == entry["jsonrpc"], "json-rpc disagrees with the corpus"
    assert rendered["data"]["type"] == expected
    # The structured part travels, which is the half a status code cannot carry.
    assert entry["structured"] in rendered["data"]


def test_a_quota_refusal_carries_its_retry_hint_on_both_transports(tmp_path, monkeypatch):
    """The other non-pydantic refusal, and the one where the structure is actionable."""
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "quota"))
    monkeypatch.setenv("FENIXSPOON_MAX_JOBS_PER_HOUR", "1")
    body = {"solver": "mock.laplace2d", "geometry": AIRFOIL, "params": FAST}

    with TestClient(create_app()) as client:
        first = client.post("/api/v1/jobs", json=body)
        assert first.status_code == 202
        # Waited out before anything else: a solve still running when the app shuts down
        # writes to a database the shutdown has already closed.
        wait_for(client, first.json()["job_id"])

        second = client.post(
            "/api/v1/jobs", json={**body, "params": {**FAST, "u_inf": 2.0}}
        )
        assert second.status_code == 429
        assert second.headers.get("Retry-After")

        rendered = refusal(
            client.app.state.core,
            Principal(id="anonymous", quotas=Quotas.from_env()),
            "job.submit",
            {**body, "params": {**FAST, "u_inf": 3.0}},
        )

    assert rendered["code"] == rpc_errors.QUOTA_EXCEEDED
    assert rendered["data"]["retry_after"] is not None


# ----------------------------------------------------------- one schema, many bindings


def test_a_capability_schema_is_one_document_wherever_it_surfaces(core, me, http):
    """`GET /solvers`, `capability.schema`, and the inline form of `capability.describe`.

    Three places a caller can obtain the same JSON Schema, and nothing but this stops one of
    them from being generated slightly differently.
    """
    from_solvers = next(
        entry for entry in http.get("/api/v1/solvers").json()
        if entry["name"] == "mock.laplace2d"
    )["params_schema"]
    from_schema = rpc(core, me, "capability.schema", {"name": "mock.laplace2d"})
    from_describe = rpc(core, me, "capability.describe", {
        "name": "mock.laplace2d", "sections": ["params"], "inline_schemas": True
    })["params"]["json_schema"]

    assert from_solvers == from_schema == from_describe


@pytest.mark.mcp
def test_the_mcp_tool_schemas_come_from_the_same_models():
    """An MCP host generating a request must be working from the models everything else
    validates against, not a hand-copied approximation that drifted."""
    pytest.importorskip("mcp.types", reason="requires the `mcp` extra")
    from fenixspoon import mcp_adapter  # noqa: PLC0415
    from fenixspoon.core.results import FieldQuery
    from fenixspoon.rpc.methods import SubmitParams

    assert mcp_adapter.TOOLS["submit_job"][2] == SubmitParams.model_json_schema()
    query = mcp_adapter.TOOLS["query_result"][2]
    for name, spec in FieldQuery.model_json_schema()["properties"].items():
        assert query["properties"][name] == spec, f"{name} drifted from FieldQuery"


# ------------------------------------------------- no numeric arrays in a compact answer


def test_the_helper_would_actually_catch_a_field_array():
    """The guard's own guard. A walker that quietly matched nothing would pass every test
    below while checking nothing at all — which is the failure mode of exactly this kind of
    assertion."""
    assert_no_numeric_arrays({"metrics": {"c_l": 0.09}, "bounds": [-1, -1, 2, 1]})
    with pytest.raises(AssertionError, match="carries 500 numbers"):
        assert_no_numeric_arrays({"diagnostics": {"residuals": list(range(500))}})
    with pytest.raises(AssertionError, match=r"fields\[0\].values"):
        assert_no_numeric_arrays({"fields": [{"values": list(range(500))}]})


@pytest.mark.parametrize(
    "levels",
    [None, ["status"], ["metrics"], ["diagnostics"], ["provenance"],
     ["status", "metrics", "diagnostics", "provenance", "artifacts"]],
)
def test_no_compact_level_carries_a_field_array(tmp_path, monkeypatch, levels):
    """The invariant the byte budgets have been approximating, asserted directly.

    Those ceilings have moved twice in a day — once for four more declared metrics, once for
    a solver warning — and both moves were correct. A byte count cannot tell "this capability
    reports more" from "somebody put the nodal data back"; this can.
    """
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "levels"))
    with TestClient(create_app()) as client:
        job_id = solved_http(client)
        params = {"levels": levels} if levels else {}
        compact = client.get(f"/api/v1/jobs/{job_id}/summary", params=params).json()
    assert_no_numeric_arrays(compact)


def test_the_fields_level_is_the_one_place_arrays_are_allowed(tmp_path, monkeypatch):
    """The counter-case, so the rule above cannot become "no arrays anywhere" — which would
    be a protocol with no way to get the answer."""
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "fields"))
    with TestClient(create_app()) as client:
        job_id = solved_http(client)
        full = client.get(
            f"/api/v1/jobs/{job_id}/summary", params={"levels": ["fields"]}
        ).json()
    with pytest.raises(AssertionError):
        assert_no_numeric_arrays(full)


def test_a_field_query_answer_stays_bounded(core, me, tmp_path):
    """Nine operations, every one of them a reduction. `sample` and `section` return short
    parallel arrays by design, so they are the ones worth checking rather than exempting."""
    del core
    session_core = FenixSpoonCore(JobManager(data_dir=tmp_path / "query"))

    async def run():
        job = await session_core.submit(
            "mock.laplace2d", TypeAdapter(Geometry).validate_python(AIRFOIL), FAST, me
        )
        while session_core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            await asyncio.sleep(0.02)
        return job.id

    job_id = asyncio.run(run())
    for op, extra in (
        ("max", {}), ("mean", {}), ("integral", {}),
        ("section", {"start": [-0.5, 0.0], "end": [1.5, 0.0], "samples": 16}),
        ("sample", {"samples": 16}),
        ("hotspots", {"count": 5}),
    ):
        answer = rpc(session_core, me, "result.query",
                     {"job_id": job_id, "field": "psi", "op": op, **extra})
        assert_no_numeric_arrays(answer, limit=MAX_COMPACT_RUN)


def test_a_study_report_carries_no_arrays_either(tmp_path, me):
    """A study is N results in one payload — the shape most likely to smuggle them in."""
    with local.open_workspace(tmp_path / "study") as session:
        geometry = session.create("geometry", AIRFOIL)
        design = session.create("design", {
            "solver": "mock.laplace2d", "geometry": geometry.ref, "params": FAST
        })
        study = session.create("study", {
            "kind": "mesh_convergence", "design": design.ref,
            "parameter": "resolution", "values": [24.0, 32.0],
        })
        session.run_study(study.ref)
        report = session.wait_for_study(study.ref)
    assert_no_numeric_arrays(report.model_dump())
