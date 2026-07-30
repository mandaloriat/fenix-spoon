"""JSON-RPC 2.0 over stdio — the base local transport (#45).

The acceptance criterion is :func:`test_a_spawned_process_solves_over_pipes_alone` — "a test
spawns the process, discovers a capability, submits a mock solve, reads the terminal status
and fetches the result — over pipes only, with no HTTP server anywhere in the test".

That one test is slow (it starts an interpreter) and coarse (a failure anywhere in the stack
shows up as one red line), so it is the *only* one that spawns anything. The rest drive
:class:`~fenixspoon.rpc.RpcServer` over an in-memory pipe, which is the same code path minus
the process: same framing, same dispatcher, same encoder. The groups are **framing** (what
delimits a message, and the property that makes a newline safe), **protocol** (JSON-RPC
conformance: ids, notifications, what this refuses), **errors** (that a domain failure
arrives typed and compact rather than as a traceback), and **operations** (that the
vocabulary reaches the core).

One harness note that is a real constraint rather than a detail. A solve is completed by
callbacks on the event loop that started it, so a conversation involving a job has to happen
inside **one** `asyncio.run`: driving the server once per request would close the loop under
the running solve, and the job would never finish. :class:`Session` is that single loop, and
it is why the solve-bearing tests are written as one `async def` rather than as a sequence of
calls.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.jobs import JobManager
from fenixspoon.rpc import errors as rpc_errors
from fenixspoon.rpc import framing
from fenixspoon.rpc.methods import METHODS, method_names
from fenixspoon.rpc.server import RpcServer

REPO_SERVER = Path(__file__).resolve().parents[1]

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
FAST = {"resolution": 40, "iterations": 150, "write_vtk": False}


# ------------------------------------------------------------------ in-process harness


class _Sink:
    """A writer that keeps the bytes, so a test can read frames back off it."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, payload: bytes) -> None:
        self.buffer += payload

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in self.buffer.splitlines() if line.strip()]


async def _exchange(core, principal, raw: bytes, *, headers: bool = False) -> list[dict]:
    """Feed raw bytes to a server and collect everything it writes back.

    The whole conversation in one call, because at bottom a JSON-RPC server is a function
    from a byte stream to a byte stream, and testing framing as anything else means testing
    the harness. Only for exchanges that start no job — see :class:`Session` for why.
    """
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    sink = _Sink()
    await RpcServer(core, principal, headers=headers).serve(reader, sink)
    return sink.messages()


class Session:
    """A live conversation with a server, on one event loop.

    Requests go in as they are written and replies are matched by `id`, which is also how a
    real client works — and it means a test can hold a solve open across several calls
    instead of restarting the world between them.
    """

    def __init__(self, core, principal) -> None:
        self.core = core
        self.principal = principal
        self.reader = asyncio.StreamReader()
        self.sink = _Sink()
        self.server = RpcServer(core, principal)
        self._next_id = 0
        self._serving: asyncio.Task | None = None

    async def __aenter__(self) -> "Session":
        self._serving = asyncio.create_task(self.server.serve(self.reader, self.sink))
        return self

    async def __aexit__(self, *_exc) -> None:
        self.reader.feed_eof()
        await self._serving

    def send(self, method: str, params: dict | None = None) -> int:
        """Write a request without waiting for it — for testing what overlaps with what."""
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.reader.feed_data(framing.encode(message))
        return self._next_id

    async def reply_to(self, request_id: int, timeout: float = 60.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for message in self.sink.messages():
                if message.get("id") == request_id:
                    return message
            assert asyncio.get_running_loop().time() < deadline, f"no reply to id {request_id}"
            await asyncio.sleep(0.005)

    async def request(self, method: str, params: dict | None = None) -> dict:
        return await self.reply_to(self.send(method, params))

    async def ok(self, method: str, params: dict | None = None):
        reply = await self.request(method, params)
        assert "error" not in reply, reply["error"]
        return reply["result"]

    async def solve(self, params: dict | None = None) -> str:
        """Submit a mock solve and return its id once it has finished."""
        created = await self.ok(
            "job.submit",
            {"solver": "mock.laplace2d", "geometry": AIRFOIL, "params": {**FAST, **(params or {})}},
        )
        return await self.finished(created["job_id"])

    async def finished(self, job_id: str, timeout: float = 60.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            status = (await self.ok("job.get", {"job_id": job_id}))["status"]
            if status in ("done", "failed", "cancelled"):
                assert status == "done", status
                return job_id
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)


def call(core, principal, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """One request in, one response out — the shape most of these tests want."""
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    replies = asyncio.run(_exchange(core, principal, framing.encode(request)))
    assert len(replies) == 1, replies
    return replies[0]


def ok(core, principal, method: str, params: dict | None = None):
    reply = call(core, principal, method, params)
    assert "error" not in reply, reply["error"]
    return reply["result"]


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


# ------------------------------------------------------------------------ acceptance


def test_a_spawned_process_solves_over_pipes_alone(tmp_path):
    """The acceptance criterion, against a real child process.

    Deliberately the only test here that spawns anything: it proves the entry point exists,
    that stdout carries the protocol and nothing else, and that the process ends when its
    input does. Everything else about the transport is cheaper in-process, and a suite that
    starts an interpreter per assertion buys slowness rather than confidence.

    No `TestClient`, no `create_app`, no uvicorn. That FastAPI is merely unused here would be
    weak evidence, which is why `test_the_rpc_adapter_does_not_import_fastapi` sits beside it
    and checks the code rather than the test.
    """
    script = [
        {"jsonrpc": "2.0", "id": 1, "method": "capability.describe",
         "params": {"name": "mock.laplace2d", "sections": ["params"]}},
        {"jsonrpc": "2.0", "id": 2, "method": "job.submit",
         "params": {"solver": "mock.laplace2d", "geometry": AIRFOIL, "params": FAST}},
    ]

    process = subprocess.Popen(
        [sys.executable, "-m", "fenixspoon", "rpc", "--stdio",
         "--data-dir", str(tmp_path / "workspace")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_SERVER,
    )
    process.stdin.write(b"".join(framing.encode(message) for message in script))
    process.stdin.flush()

    first = json.loads(process.stdout.readline())
    assert first["id"] == 1
    assert "params" in first["result"], "capability.describe returned no params section"

    created = json.loads(process.stdout.readline())
    assert created["id"] == 2
    job_id = created["result"]["job_id"]

    # Poll to a terminal status over the same pipe. The point is not the polling but that
    # the channel keeps answering while the solve it started is running.
    status = created["result"]["status"]
    for attempt in range(600):
        process.stdin.write(framing.encode(
            {"jsonrpc": "2.0", "id": 100 + attempt, "method": "job.get",
             "params": {"job_id": job_id}}
        ))
        process.stdin.flush()
        status = json.loads(process.stdout.readline())["result"]["status"]
        if status in ("done", "failed", "cancelled"):
            break
    assert status == "done", status

    process.stdin.write(framing.encode(
        {"jsonrpc": "2.0", "id": 999, "method": "result.get",
         "params": {"job_id": job_id, "levels": ["status", "metrics", "fields"]}}
    ))
    # Closing stdin is how this transport is told to stop; the last answer still arrives,
    # which is what makes `printf ... | fenix-spoon rpc --stdio` a usable thing to type.
    process.stdin.close()

    result = json.loads(process.stdout.readline())["result"]
    assert result["status"]["status"] == "done"
    assert result["fields"]["data"]["fields"]["psi"], "no field payload came back"
    assert result["metrics"], "no declared metrics came back"

    stderr = process.stderr.read()
    assert process.wait(timeout=30) == 0, stderr.decode()
    assert stderr == b"", f"the child wrote to stderr; stdout must stay clean\n{stderr.decode()}"


def test_the_rpc_adapter_does_not_import_fastapi():
    """The issue's requirement — no HTTP server anywhere — as a property of the code.

    A subprocess for the reason `test_the_core_does_not_import_fastapi` uses one: this test
    process already has FastAPI loaded, so an in-process `sys.modules` check would pass
    whatever the adapter imports. The chain most likely to break it is a handler reaching for
    `api.JobCreated` to build the submit reply — which is why that reply is a literal dict
    with `test_the_submit_reply_matches_the_http_model` pinning its shape.
    """
    code = "import sys; import fenixspoon.rpc; sys.exit(1 if 'fastapi' in sys.modules else 0)"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_SERVER, capture_output=True, text=True
    )
    assert result.returncode == 0, f"importing fenixspoon.rpc imported fastapi\n{result.stderr}"


# --------------------------------------------------------------------------- framing


def test_no_encodable_value_can_break_the_frame():
    """The property newline delimiting depends on, asserted rather than assumed.

    If any of these produced a raw newline — or a U+2028/U+2029, which several languages'
    line splitters treat as one even though Python's does not — a single message would arrive
    as two malformed ones. This is the evidence behind choosing NDJSON in `framing.py`;
    without it the choice would be a preference.
    """
    hostile = {
        "newline": "a\nb",
        "carriage": "a\r\nb",
        "line_separator": "a b",
        "paragraph_separator": "a b",
        "nested": [{"deep": {"still": "x\ny"}}],
        "unicode": "λ ψ ∇²",
        "control": "\x00\x1f",
    }
    frame = framing.encode({"jsonrpc": "2.0", "id": 1, "result": hostile})
    assert frame.count(b"\n") == 1 and frame.endswith(b"\n")
    text = frame.decode("utf-8")
    for terminator in (" ", " ", "\r"):
        assert terminator not in text, f"{terminator!r} survived encoding"
    assert json.loads(frame)["result"] == hostile


@pytest.mark.parametrize("style", ["ndjson", "headers"])
def test_both_framings_are_accepted_on_input(core, me, style):
    """A caller built against an LSP-style client should not have to re-frame."""
    request = {"jsonrpc": "2.0", "id": 7, "method": "rpc.describe"}
    raw = framing.encode(request) if style == "ndjson" else framing.encode_with_headers(request)
    replies = asyncio.run(_exchange(core, me, raw))
    assert replies[0]["id"] == 7


def test_the_two_framings_can_be_mixed_on_one_stream(core, me):
    """Detection is per message, so a client that switches mid-stream still works."""
    raw = (
        framing.encode({"jsonrpc": "2.0", "id": 1, "method": "rpc.describe"})
        + framing.encode_with_headers({"jsonrpc": "2.0", "id": 2, "method": "capability.list"})
        + framing.encode({"jsonrpc": "2.0", "id": 3, "method": "workspace.open"})
    )
    replies = asyncio.run(_exchange(core, me, raw))
    assert sorted(reply["id"] for reply in replies) == [1, 2, 3]


def test_header_output_round_trips(core, me):
    """`--framing headers` writes what this module's own reader accepts."""
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(framing.encode({"jsonrpc": "2.0", "id": 1, "method": "rpc.describe"}))
        reader.feed_eof()
        sink = _Sink()
        await RpcServer(core, me, headers=True).serve(reader, sink)
        assert sink.buffer.startswith(b"Content-Length: ")

        back = asyncio.StreamReader()
        back.feed_data(bytes(sink.buffer))
        back.feed_eof()
        return await framing.read_message(back)

    assert asyncio.run(run())["id"] == 1


def test_a_truncated_body_is_a_framing_error_not_end_of_input():
    """The difference decides whether the server shuts down or answers `-32700`."""
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b'Content-Length: 40\r\n\r\n{"jsonrpc"')
        reader.feed_eof()
        return await framing.read_message(reader)

    with pytest.raises(framing.FramingError):
        asyncio.run(run())


def test_garbage_gets_a_parse_error_and_the_connection_survives(core, me):
    """One bad frame must not cost the caller its channel."""
    raw = b"this is not json\n" + framing.encode(
        {"jsonrpc": "2.0", "id": 2, "method": "rpc.describe"}
    )
    replies = asyncio.run(_exchange(core, me, raw))
    assert replies[0]["error"]["code"] == rpc_errors.PARSE_ERROR
    assert replies[0]["id"] is None, "a frame that did not parse has no id to echo"
    assert replies[1]["id"] == 2, "the server stopped reading after a bad frame"


def test_a_batch_is_refused_by_name(core, me):
    """Not supported, and it says so — rather than a parse error the caller must interpret."""
    raw = b'[{"jsonrpc":"2.0","id":1,"method":"rpc.describe"}]\n'
    replies = asyncio.run(_exchange(core, me, raw))
    assert replies[0]["error"]["code"] == rpc_errors.PARSE_ERROR
    assert "batch" in replies[0]["error"]["message"].lower()


# -------------------------------------------------------------------------- protocol


@pytest.mark.parametrize("request_id", [1, "abc", 0])
def test_the_response_echoes_the_request_id(core, me, request_id):
    request = {"jsonrpc": "2.0", "id": request_id, "method": "rpc.describe"}
    replies = asyncio.run(_exchange(core, me, framing.encode(request)))
    assert replies[0]["id"] == request_id


def test_a_notification_is_answered_with_silence(core, me):
    """No `id` means no response, including when it fails — that is the spec, and a server
    that answered anyway would desynchronise a client counting responses."""
    raw = (
        framing.encode({"jsonrpc": "2.0", "method": "rpc.describe"})
        + framing.encode({"jsonrpc": "2.0", "method": "no.such.method"})
        + framing.encode({"jsonrpc": "2.0", "id": 9, "method": "rpc.describe"})
    )
    replies = asyncio.run(_exchange(core, me, raw))
    assert [reply["id"] for reply in replies] == [9]


def test_a_missing_version_field_is_an_invalid_request(core, me):
    replies = asyncio.run(_exchange(core, me, b'{"id":1,"method":"rpc.describe"}\n'))
    assert replies[0]["error"]["code"] == rpc_errors.INVALID_REQUEST


def test_an_unknown_method_lists_the_ones_that_exist(core, me):
    """A misspelling gets its answer here rather than over another round trip."""
    reply = call(core, me, "job.sumbit")
    assert reply["error"]["code"] == rpc_errors.METHOD_NOT_FOUND
    assert "job.submit" in reply["error"]["data"]["methods"]


@pytest.mark.parametrize("bad_id", [{"a": 1}, [1, 2], True])
def test_an_id_the_spec_does_not_allow_is_refused_rather_than_echoed(core, me, bad_id):
    """`id` is String, Number or Null. Anything else cannot be sent back without the
    *response* becoming non-conformant too — so the one error a client would use to find its
    bug would itself be malformed. Answered with `id: null`, which is what the spec says to
    do when the id is unusable. Raised in review of #45.

    Booleans are in here because they are the case a naive check misses: `True` is an `int`
    in Python, so `isinstance(request_id, int)` accepts an id JSON-RPC does not have.
    """
    request = {"jsonrpc": "2.0", "id": bad_id, "method": "rpc.describe"}
    replies = asyncio.run(_exchange(core, me, framing.encode(request)))
    assert replies[0]["error"]["code"] == rpc_errors.INVALID_REQUEST
    assert replies[0]["id"] is None
    assert "result" not in replies[0], "an unusable id still got the method run"


def test_a_fractional_id_is_echoed_rather_than_refused(core, me):
    """The spec says numbers SHOULD NOT have fractional parts — a recommendation, not a
    requirement. Refusing over it would be inventing a rule to enforce against a client that
    is not actually wrong."""
    request = {"jsonrpc": "2.0", "id": 1.5, "method": "rpc.describe"}
    replies = asyncio.run(_exchange(core, me, framing.encode(request)))
    assert replies[0]["id"] == 1.5 and "result" in replies[0]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"limit": {}}, "WrongParameterType"),
        ({"limit": None, "offset": "3"}, "WrongParameterType"),
        ({"limit": True}, "WrongParameterType"),
        ({"limit": 1.5}, "WrongParameterType"),
        ({"limit": 0}, "ParameterOutOfRange"),
        ({"offset": -1}, "ParameterOutOfRange"),
    ],
)
def test_a_malformed_numeric_param_is_the_callers_error_not_the_servers(core, me, params, expected):
    """`int({})` raises `TypeError`, and the first implementation let that reach the
    dispatcher's last-resort handler — reporting a caller's malformed input as `-32603
    internal error`, which sends it to look for a bug in the wrong codebase. Raised in
    review of #45.

    `True` and `1.5` are here because they are the ones a plain `int(...)` accepts silently:
    `int(True)` is a perfectly good `1`, and `int(1.5)` quietly truncates. `0` and `-1` are
    here because a negative bound reaches a list slice, where `stored[-1:]` is a valid
    expression meaning something nobody asked for.
    """
    reply = call(core, me, "job.list", params)
    assert reply["error"]["code"] == rpc_errors.INVALID_PARAMS
    assert reply["error"]["data"]["type"] == expected


def test_a_whole_number_sent_as_a_float_is_accepted(core, me):
    """JSON has one number type, so an encoder emitting `50.0` for fifty is being correct
    rather than sloppy."""
    assert ok(core, me, "job.list", {"limit": 50.0})["limit"] == 50


def test_a_non_boolean_flag_does_not_silently_mean_true(core, me):
    """`bool("false")` is `True`. A client sending the string — which a shell wrapper easily
    does — would have got the *opposite* of what it asked for, and for `inline_schemas` that
    means kilobytes of JSON Schema arriving in a context window that asked not to have it.
    Raised in review of #45."""
    reply = call(core, me, "capability.describe",
                 {"name": "mock.laplace2d", "inline_schemas": "false"})
    assert reply["error"]["code"] == rpc_errors.INVALID_PARAMS
    assert reply["error"]["data"]["parameter"] == "inline_schemas"

    # …and the flag still works when it is actually a boolean.
    described = ok(core, me, "capability.describe",
                   {"name": "mock.laplace2d", "sections": ["params"], "inline_schemas": True})
    assert described["params"]["json_schema"]["properties"], "inline_schemas returned no schema"
    assert "json_schema" not in ok(core, me, "capability.describe",
                                   {"name": "mock.laplace2d", "sections": ["params"]})


def test_positional_params_are_refused(core, me):
    """Named only. Binding an array onto methods with five optional arguments is how
    "I omitted `sections`" becomes "I passed `sections` where `inline_schemas` goes"."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "capability.describe",
               "params": ["mock.laplace2d"]}
    replies = asyncio.run(_exchange(core, me, framing.encode(request)))
    assert replies[0]["error"]["code"] == rpc_errors.INVALID_PARAMS
    assert replies[0]["error"]["data"]["type"] == "PositionalParams"


def test_rpc_describe_names_every_callable_method(core, me):
    """Discovery that cannot be wrong, because the list is derived. The connection-scoped
    methods are in it even though they have no entry in the handler table, which is the one
    way this could quietly under-report."""
    described = ok(core, me, "rpc.describe")
    assert described["methods"] == method_names()
    assert "job.subscribe" in described["methods"]
    assert set(METHODS) <= set(described["methods"])
    assert described["batch"] is False


def test_the_channel_answers_while_a_solve_is_running(core, me):
    """The property that makes jobs asynchronous rather than nominally asynchronous.

    A long solve is started, and then three more calls are made and answered *before it
    finishes* — which is the last assertion: the job is still not terminal when they have all
    come back. What this pins is that the solve does not starve the channel; move
    `solver.solve` off the executor and onto the event loop and every call below waits out
    the twenty thousand iterations, so this test times out.

    What it does *not* pin is concurrent dispatch, and it would be easy to read it as if it
    did: `job.submit` returns as soon as the backend has taken the work, so a strictly serial
    dispatcher would answer these just as fast. That property has its own test below.

    Nor does it assert the job reports `running`. A live in-process job answers from the
    object the manager holds, whose status is copied back only when the solve finishes — so
    an in-flight job reads as `queued` on every transport. Pre-existing and cosmetic; pinning
    it here would be pinning it in the wrong issue.
    """
    async def run():
        async with Session(core, me) as session:
            created = await session.ok("job.submit", {
                "solver": "mock.laplace2d", "geometry": AIRFOIL,
                "params": {"resolution": 256, "iterations": 20000},
            })
            job_id = created["job_id"]

            assert await session.ok("capability.list"), "discovery blocked behind a solve"
            assert (await session.ok("environment.inspect"))["capabilities"] >= 3
            assert (await session.ok("job.get", {"job_id": job_id}))["status"] != "done", (
                "the solve finished first, so this proves nothing — make it longer"
            )

            # Cancelled rather than waited out: the point has been made, and twenty thousand
            # iterations at this resolution is a minute the suite does not need to spend.
            await session.ok("job.cancel", {"job_id": job_id})

    asyncio.run(run())


def test_a_slow_request_does_not_hold_up_the_next_one(core, me, monkeypatch):
    """Concurrent dispatch, asserted by the only thing that can show it: reply order.

    A handler is made to take half a second, and a second request is written straight after
    it without waiting. Its answer comes back *first*. That is legal — JSON-RPC correlates by
    `id`, not by arrival — and it is the reason a caller can poll a job while something else
    is still being computed for it.

    The wait is in the handler rather than in a solver so the test controls it exactly: a
    real solve's duration depends on the machine, and a timing assertion on a loaded CI box
    is a flake generator.
    """
    async def slow(*_a, **_k):
        await asyncio.sleep(0.5)
        return {"slept": True}

    monkeypatch.setitem(METHODS, "workspace.open", slow)

    async def run():
        async with Session(core, me) as session:
            slow_id = session.send("workspace.open")
            fast_id = session.send("capability.list")
            await session.reply_to(fast_id)
            assert [m["id"] for m in session.sink.messages()] == [fast_id], (
                "the second request waited for the first"
            )
            await session.reply_to(slow_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------- errors


def test_every_core_error_has_a_json_rpc_code():
    """Every domain error must be classified deliberately, not fall through to `-32000`.

    The counterpart of `test_every_core_error_has_a_status`. Both exist so that adding an
    error to the core is a decision about how each transport reports it, rather than
    something two adapters silently guess at differently.
    """
    unmapped = [
        cls.__name__
        for cls in vars(errors).values()
        if isinstance(cls, type)
        and issubclass(cls, errors.CoreError)
        and cls is not errors.CoreError
        and cls not in rpc_errors.CODES
    ]
    assert not unmapped, f"no JSON-RPC code for: {unmapped}"


def test_the_two_transports_classify_the_same_errors_the_same_way():
    """HTTP statuses and JSON-RPC codes are independent tables over the same errors, and a
    caller writing one handler for both needs them to agree on *which* errors are alike.

    Not that the values match — they cannot — but that the partitions do: two errors sharing
    a status share a code, and the other way round. Drift here would mean one transport
    thinks a failed patch is a validation error while the other calls it a conflict, and #51
    would find it much later.
    """
    from fenixspoon import http_errors

    by_status: dict[int, set[str]] = {}
    by_code: dict[int, set[str]] = {}
    for cls, status in http_errors.STATUS.items():
        by_status.setdefault(status, set()).add(cls.__name__)
    for cls, code in rpc_errors.CODES.items():
        by_code.setdefault(code, set()).add(cls.__name__)
    assert sorted(by_status.values(), key=sorted) == sorted(by_code.values(), key=sorted)


def test_a_domain_error_arrives_typed_and_compact(core, me):
    reply = call(core, me, "capability.describe", {"name": "physics.from_the_future"})
    error = reply["error"]
    assert error["code"] == rpc_errors.NOT_FOUND
    assert error["data"]["type"] == "UnknownCapability"
    assert error["data"]["name"] == "physics.from_the_future"
    assert "Traceback" not in json.dumps(error)
    assert len(json.dumps(error)) < 400


def test_a_validation_failure_carries_the_same_structure_as_http(core, me):
    """The issue asks that validation failures carry the same information as the HTTP `422`
    detail. That is the same pydantic list, so a caller parses one shape rather than two."""
    reply = call(core, me, "job.submit", {
        "solver": "mock.laplace2d", "geometry": AIRFOIL, "params": {"resolution": -5}
    })
    assert reply["error"]["code"] == rpc_errors.INVALID_PARAMS
    assert reply["error"]["data"]["type"] == "InvalidParams"
    assert reply["error"]["data"]["errors"][0]["loc"] == ["resolution"]


def test_a_budget_refusal_stays_distinguishable_from_malformed_input(tmp_path, me):
    """Both are `-32602`; `data.type` is what separates "you asked for too much" from "you
    asked for nonsense", and the issue requires them to stay apart."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs", max_cells=100))
    reply = call(core, me, "job.submit", {
        "solver": "mock.laplace2d", "geometry": AIRFOIL, "params": {"resolution": 256}
    })
    assert reply["error"]["data"]["type"] == "CellBudgetExceeded"
    assert reply["error"]["data"]["limit"] == 100


def test_a_quota_refusal_says_whether_waiting_helps(tmp_path):
    """The one refusal where the remedy is time, so `retry_after` rides along — and is
    absent when waiting would not help, rather than guessed at."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    principal = Principal(id="small", quotas=Quotas(jobs_per_hour=1))
    ok(core, principal, "job.submit",
       {"solver": "mock.laplace2d", "geometry": AIRFOIL, "params": FAST})
    reply = call(core, principal, "job.submit", {
        "solver": "mock.laplace2d", "geometry": AIRFOIL, "params": {**FAST, "iterations": 151}
    })
    assert reply["error"]["code"] == rpc_errors.QUOTA_EXCEEDED
    assert reply["error"]["data"]["type"] == "QuotaExceeded"
    assert reply["error"]["data"]["retry_after"] > 0


def test_submitting_a_design_and_a_geometry_at_once_is_refused(core, me):
    """Two intents in one request. Picking one silently produces a job whose inputs are not
    what the caller believes they are."""
    reply = call(core, me, "job.submit",
                 {"design": "design:d-1", "solver": "mock.laplace2d", "geometry": AIRFOIL})
    assert reply["error"]["code"] == rpc_errors.INVALID_PARAMS
    assert "not both" in json.dumps(reply["error"]["data"])


def test_an_internal_failure_does_not_put_a_traceback_on_the_wire(core, me, monkeypatch):
    """A bug here is `-32603` and a log line. A traceback would tell an attacker about the
    filesystem and tell an agent nothing it can act on."""
    def boom(*_a, **_k):
        raise RuntimeError("secret path /home/someone/keys")

    monkeypatch.setitem(METHODS, "rpc.describe", boom)
    reply = call(core, me, "rpc.describe")
    assert reply["error"]["code"] == rpc_errors.INTERNAL_ERROR
    assert "secret path" not in json.dumps(reply)


def test_one_principals_job_is_not_visible_to_another(core, me):
    """Identity is not bypassed: the local transport is a different door, not a way around."""
    async def run():
        async with Session(core, me) as session:
            return await session.solve()

    job_id = asyncio.run(run())
    stranger = Principal(id="somebody-else", quotas=Quotas())
    reply = call(core, stranger, "job.get", {"job_id": job_id})
    assert reply["error"]["data"]["type"] == "JobNotFound"


# ------------------------------------------------------------------------ operations


def test_discovery_answers_without_a_solve(core, me):
    described = ok(core, me, "capability.describe",
                   {"name": "mock.laplace2d", "sections": ["params", "metrics"]})
    assert set(described) >= {"params", "metrics"}
    assert "geometries" not in described, "an unrequested section came back"

    environment = ok(core, me, "environment.inspect")
    assert environment["capabilities"] >= 3
    assert environment["cache"]["scheme"]


def test_a_design_can_be_created_patched_and_solved(core, me):
    """The workspace's first transport, which is what #44 left it waiting for."""
    async def run():
        async with Session(core, me) as session:
            geometry = await session.ok("object.create", {"type": "geometry", "body": AIRFOIL})
            design = await session.ok("object.create", {
                "type": "design",
                "body": {"solver": "mock.laplace2d", "geometry": geometry["ref"], "params": FAST},
            })
            patched = await session.ok("object.patch", {
                "ref": design["ref"],
                "patch": [{"op": "replace", "path": "/params/resolution", "value": 32}],
            })
            assert patched["revision"] == 2

            created = await session.ok("job.submit", {"design": design["ref"]})
            await session.finished(created["job_id"])
            provenance = await session.ok("job.provenance", {"job_id": created["job_id"]})
            assert provenance["inputs"]["design"] == f"{design['ref']}@2", (
                "the solve froze the wrong revision"
            )

            # …and the relation reads backwards, from the object to what used it.
            back = await session.ok("job.for_object", {"ref": design["ref"]})
            assert [job["job_id"] for job in back["jobs"]] == [created["job_id"]]

    asyncio.run(run())


def test_the_two_transports_agree_on_a_selective_payload(core, me):
    """`capability.describe` renders identically over HTTP and over the pipe.

    The rule that unrequested sections are *absent* rather than null is declared twice — as
    `response_model_exclude_none=True` on the route, and as `Selective.wire()` for everything
    else — because FastAPI applies its own rendering to a route's return value and cannot be
    told to use the model's. Two declarations of one rule is exactly the kind of thing that
    drifts, so this compares the actual bytes rather than trusting that they agree.

    The one test in this module that starts an HTTP app, and deliberately not the acceptance
    one: that must prove a pipe needs no server, and this must prove the two say the same
    thing.
    """
    from fastapi.testclient import TestClient

    from fenixspoon.main import create_app

    over_pipe = ok(core, me, "capability.describe",
                   {"name": "mock.laplace2d", "sections": ["params", "cost"]})
    with TestClient(create_app()) as client:
        over_http = client.get(
            "/api/v1/capabilities/mock.laplace2d", params={"sections": ["params", "cost"]}
        ).json()
    assert over_pipe == over_http


def test_the_submit_reply_matches_the_http_model():
    """`job.submit` answers field-for-field what `POST /api/v1/jobs` does.

    The reply is a literal dict rather than an `api.JobCreated`, because importing that
    module would pull FastAPI into this transport. That is the right call, and it costs the
    compiler's ability to notice drift — so the check lives here instead: a field added to
    `JobCreated` without being added to the handler fails this.
    """
    from fenixspoon.api import JobCreated

    assert set(JobCreated.model_fields) == {"job_id", "status", "cached"}


def test_an_identical_resubmission_is_answered_from_the_cache(core, me):
    """#47 reaches this transport for free, because it is a property of the core rather than
    of a route. Worth pinning: `cached` is the difference between a metric that reflects the
    edit just made and one answering a question asked ten minutes ago."""
    async def run():
        async with Session(core, me) as session:
            job_id = await session.solve()
            again = await session.ok("job.submit", {
                "solver": "mock.laplace2d", "geometry": AIRFOIL, "params": FAST,
            })
            assert again["job_id"] == job_id
            assert again["cached"] is True
            assert (await session.ok("job.provenance", {"job_id": job_id}))["cached"] is True

    asyncio.run(run())


def test_events_are_pollable_by_sequence_number(core, me):
    """The replay contract the WebSocket has, on a transport with no socket: read from
    `since`, get what you have not seen, learn when it is over."""
    async def run():
        async with Session(core, me) as session:
            job_id = await session.solve()
            first = await session.ok("job.events", {"job_id": job_id, "limit": 1})
            assert len(first["events"]) == 1
            assert first["next"] == 1
            assert first["final"] is False

            rest = await session.ok("job.events", {"job_id": job_id, "since": first["next"]})
            assert rest["final"] is True, "the log never reported a terminal event"
            assert rest["events"][-1]["status"] == "done"

            beyond = await session.ok("job.events", {"job_id": job_id, "since": rest["next"]})
            assert beyond["events"] == [] and beyond["final"] is False

    asyncio.run(run())


def test_a_subscription_pushes_progress_notifications(core, me):
    """The streaming half. Notifications carry no `id`, which is how a client tells them from
    the answer to something it asked for."""
    async def run():
        async with Session(core, me) as session:
            created = await session.ok("job.submit", {
                "solver": "mock.laplace2d", "geometry": AIRFOIL,
                "params": {**FAST, "report_every": 25},
            })
            job_id = created["job_id"]
            assert (await session.ok("job.subscribe", {"job_id": job_id}))["subscribed"] is True

            deadline = asyncio.get_running_loop().time() + 30
            while not any(
                message.get("method") == "job.progress"
                and message["params"]["event"].get("status") == "done"
                for message in session.sink.messages()
            ):
                assert asyncio.get_running_loop().time() < deadline, "no terminal notification"
                await asyncio.sleep(0.02)
            return session.sink.messages()

    messages = asyncio.run(run())
    notifications = [m for m in messages if m.get("method") == "job.progress"]
    assert notifications, "subscribing produced no notifications"
    assert all("id" not in m for m in notifications), "a notification carried an id"
    assert any(m["params"]["event"]["type"] == "progress" for m in notifications)


def test_a_result_query_returns_a_number_not_an_array(core, me):
    """The token-efficiency rule, on the transport it was written for."""
    async def run():
        async with Session(core, me) as session:
            job_id = await session.solve()
            return await session.ok(
                "result.query", {"job_id": job_id, "field": "psi", "op": "max"}
            )

    answer = asyncio.run(run())
    assert isinstance(answer["result"]["value"], int | float)
    assert len(json.dumps(answer)) < 400


def test_an_artifact_resolves_to_a_path_rather_than_bytes(core, me):
    """The caller is a process on this machine — the premise of the whole transport — so it
    opens the file. Base64 through a pipe would be the same bytes plus a third."""
    async def run():
        async with Session(core, me) as session:
            created = await session.ok("job.submit", {
                "solver": "mock.laplace2d", "geometry": AIRFOIL,
                "params": {**FAST, "write_vtk": True},
            })
            await session.finished(created["job_id"])
            return await session.ok(
                "artifact.get", {"job_id": created["job_id"], "name": "solution.vtk"}
            )

    handle = asyncio.run(run())
    assert Path(handle["path"]).is_absolute(), "a relative path means nothing to another process"
    assert Path(handle["path"]).is_file()
    assert handle["size"] > 0


def test_the_default_result_answer_stays_small(core, me):
    """The same budget the HTTP compact route holds to (#46): a caller that asks for nothing
    in particular gets something it can read.

    Measured on the **encoded frame**, not on `json.dumps` with its default separators. This
    transport writes compact JSON, so the frame is what a caller is actually charged for —
    1341 bytes against 1368 for the spaced form on the same payload. Small difference, but a
    budget should measure the thing it is budgeting.

    The number moved from 1024 to match `test_the_summary_route_is_the_compact_one`, and for
    the reason recorded in full beside `test_the_default_answer_is_small_and_carries_no_arrays`
    in `test_results.py`: **the default answer grows linearly with what a capability declares**,
    and that growth is the feature. #68 took `mock.laplace2d` from two declared metrics to six
    and added a lift-consistency warning that is ~250 bytes of prose on its own. A converged
    solve is 1249 bytes now; this fixture runs 150 iterations, so it also carries the
    iteration-cap warning and reaches 1368.

    So the byte ceiling is a proxy and always was. The invariant it stands in for is the
    assertion above it — no field arrays in the default answer — which is what actually keeps
    a compact response compact, and which no number of declared metrics can break.
    """
    async def run():
        async with Session(core, me) as session:
            job_id = await session.solve()
            return await session.ok("result.get", {"job_id": job_id})

    default = asyncio.run(run())
    assert "fields" not in default
    frame = framing.encode({"jsonrpc": "2.0", "id": 1, "result": default})
    # The per-section breakdown, not just the total. Whoever sees this next has to decide
    # whether the budget should move again or whether something is genuinely wrong, and that
    # is a question about *which* section grew — the two times it has moved so far, the
    # answer was `metrics` once and `diagnostics` once. Measuring that by hand is a
    # ten-minute detour this message removes.
    breakdown = {key: len(json.dumps(value)) for key, value in default.items()}
    assert len(frame) < 1536, (
        f"the default answer is {len(frame)} bytes on the wire, over budget.\n"
        f"sections: {breakdown}\n"
        "If a capability simply declares more metrics, moving the budget is the right fix — "
        "see the docstring. If a field array got in, it is not."
    )
