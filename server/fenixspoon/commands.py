"""The operation subcommands of `fenix-spoon` (roadmap M2.5, #50).

`fenix-spoon <noun> <verb>` over the same operations every other transport exposes — useful
in CI, and useful as **the debugging surface for exactly what an agent sees**, which is the
part that decides the design:

**Every command dispatches through the JSON-RPC method table**, exactly as the MCP adapter
does. So `--json` output is not merely *comparable* to the JSON-RPC result for the same
request — it is the same value, field for field, and
`test_the_json_output_is_the_json_rpc_result` asserts equality rather than trusting it.
Running the CLI to see what an agent would get is only worth doing if the answer is the same
object.

*Value*, not bytes: this pretty-prints and the transport frames compactly, so the two encode
the same document differently. The wording said "the same bytes" until review of #50 pointed
out that it was literally false — and pretty-printing is worth keeping, because the reason
this exists is a human reading what an agent received. `json.loads` on both sides is the
comparison that means something.

Two things follow from that and are worth stating plainly.

**Human-readable output is a rendering of the JSON, never a different answer.** `--json`
switches the *presentation*; there is no command that computes something for humans that a
machine caller cannot get.

**Exit codes come from the JSON-RPC error codes**, so a shell script distinguishes the cases
the issue asks it to — invalid request, not finished, not found, over quota — without parsing
prose. The mapping is one table below.
"""

import json
import sys
from typing import Any

from .core import FenixSpoonCore, errors
from .core.identity import Principal
from .rpc import errors as rpc_errors
from .rpc.encoding import jsonable
from .rpc.methods import METHODS, MethodError

#: JSON-RPC code → process exit code. Distinct numbers for the cases a script branches on,
#: which is the whole reason the issue asks for them: `2` is argparse's usage error and stays
#: reserved for that, and `1` is the traditional "something else went wrong".
EXIT_CODES: dict[int, int] = {
    rpc_errors.INVALID_PARAMS: 3,  # invalid request: fix the arguments
    rpc_errors.INVALID_REQUEST: 3,
    rpc_errors.METHOD_NOT_FOUND: 3,
    rpc_errors.NOT_FOUND: 4,  # no such capability, job, object or artifact
    rpc_errors.CONFLICT: 5,  # not finished, already finished, nothing to patch
    rpc_errors.QUOTA_EXCEEDED: 6,  # waiting may help; the payload says whether
    rpc_errors.GONE: 7,  # the job is there and its arrays are not
}
FALLBACK_EXIT = 1

#: `<noun> <verb>` → (RPC method, positional argument names, help).
#:
#: Positional names are the *tool's* argument names, so `fenix-spoon object get design:d-1`
#: passes `{"ref": "design:d-1"}` — one table, no per-command argument plumbing, and a
#: command cannot drift from the operation it names.
COMMANDS: dict[tuple[str, str], tuple[str, list[str], str]] = {
    ("environment", "inspect"): ("environment.inspect", [], "what this installation is"),
    ("capability", "list"): ("capability.list", [], "installed capabilities, one line each"),
    ("capability", "describe"): (
        "capability.describe",
        ["name"],
        "selected sections of one capability",
    ),
    ("capability", "schema"): (
        "capability.schema",
        ["name"],
        "the full params JSON Schema for one capability",
    ),
    ("workspace", "info"): ("workspace.open", [], "where the workspace is and what is in it"),
    ("workspace", "list"): ("workspace.list", [], "objects, newest first"),
    ("object", "create"): ("object.create", ["type"], "create an object from JSON on stdin"),
    ("object", "get"): ("object.get", ["ref"], "one object revision"),
    ("object", "revisions"): ("object.revisions", ["ref"], "which revisions exist"),
    ("object", "patch"): ("object.patch", ["ref"], "apply an RFC 6902 patch from stdin"),
    ("design", "resolve"): ("design.resolve", ["ref"], "what a design resolves to"),
    ("job", "submit"): ("job.submit", [], "solve a design, or a solver + geometry"),
    ("job", "get"): ("job.get", ["job_id"], "a job's status and metrics"),
    ("job", "list"): ("job.list", [], "this principal's history"),
    ("job", "cancel"): ("job.cancel", ["job_id"], "request cooperative cancellation"),
    ("job", "events"): ("job.events", ["job_id"], "progress log from a sequence number"),
    ("job", "provenance"): ("job.provenance", ["job_id"], "what produced this answer"),
    ("result", "get"): ("result.get", ["job_id"], "the answer, at the levels asked for"),
    ("result", "query"): ("result.query", ["job_id"], "one bounded question about one field"),
    ("artifact", "get"): ("artifact.get", ["job_id", "name"], "resolve an artifact to a path"),
    ("study", "run"): ("study.run", ["ref"], "submit every variation of a study"),
    ("study", "get"): ("study.get", ["ref"], "the (variation → metric) table"),
    ("optimize", "run"): ("optimize.run", ["ref"], "search until the tolerance or the budget"),
    ("optimize", "get"): ("optimize.get", ["ref"], "the trajectory and the best point"),
}

#: Optional flags, per RPC parameter name, and how to parse them. Shared across commands
#: because the parameter names are: `--levels` means the same thing wherever it appears.
FLAGS: dict[str, tuple[str, str]] = {
    "sections": ("append", "section to include; repeat for several"),
    "levels": ("append", "result level to include; repeat for several"),
    "inline_schemas": ("flag", "return the full params schema inline"),
    "design": ("str", "a design reference to solve"),
    "solver": ("str", "a capability name, for an inline solve"),
    "type": ("str", "object type filter"),
    "label": ("str", "human label for a new revision"),
    "field": ("str", "field to query"),
    "op": ("str", "query operation"),
    "region": ("str", "named region, for `over_region`"),
    "limit": ("int", "page size"),
    "offset": ("int", "page offset"),
    "since": ("int", "sequence number to read from"),
    "count": ("int", "how many hotspots"),
    "samples": ("int", "sample budget"),
}


def add_subcommands(subparsers) -> None:
    """Register `<noun> <verb>` under the top-level parser.

    Built from :data:`COMMANDS` rather than written out, so adding an operation is one row
    and cannot produce a command whose `--json` disagrees with the method it names.
    """
    nouns: dict[str, Any] = {}
    for (noun, verb), (_method, positionals, help_text) in COMMANDS.items():
        if noun not in nouns:
            parser = subparsers.add_parser(noun, help=f"{noun} operations")
            parser.set_defaults(handler=run_command, noun=noun)
            nouns[noun] = parser.add_subparsers(dest="verb", required=True)
        command = nouns[noun].add_parser(verb, help=help_text)
        command.set_defaults(handler=run_command, noun=noun, verb=verb)
        for name in positionals:
            command.add_argument(name, help=f"{name} for this operation")
        command.add_argument(
            "--json",
            action="store_true",
            help="print the raw result — the same value JSON-RPC returns, pretty-printed",
        )
        if (noun, verb) in (("job", "submit"), ("study", "run")):
            command.add_argument(
                "--detach",
                action="store_true",
                help=(
                    "return as soon as the work is accepted instead of waiting. Only "
                    "meaningful with a worker backend (FENIXSPOON_REDIS_URL): with the "
                    "in-process one the solve dies with this command."
                ),
            )
        command.add_argument(
            "--param",
            action="append",
            metavar="KEY=VALUE",
            default=[],
            help="an extra parameter, JSON-decoded when it parses as JSON",
        )
        for flag, (kind, flag_help) in FLAGS.items():
            if flag in positionals:
                # `object create` takes `type` positionally, and `--type` is the workspace
                # listing filter. Registering both would let `object create geometry --type
                # design` build a request whose `type` disagrees with the body on stdin — a
                # command that succeeds and creates the wrong thing. Raised in review of #50.
                continue
            option = f"--{flag.replace('_', '-')}"
            if kind == "append":
                command.add_argument(option, action="append", dest=flag, help=flag_help)
            elif kind == "flag":
                command.add_argument(option, action="store_true", dest=flag, help=flag_help)
            elif kind == "int":
                command.add_argument(option, type=int, dest=flag, help=flag_help)
            else:
                command.add_argument(option, dest=flag, help=flag_help)


def collect_params(args, positionals: list[str]) -> dict[str, Any]:
    """Turn parsed arguments into the params the RPC method expects.

    Reads a JSON body from **stdin** when the operation takes one (`object create`,
    `object patch`, an inline `job submit`). A file redirect is how a shell passes a document,
    and putting a geometry on a command line is how quoting bugs happen.
    """
    params: dict[str, Any] = {name: getattr(args, name) for name in positionals}
    for flag in FLAGS:
        value = getattr(args, flag, None)
        # `False` is a real value for a `--flag`, but only when the flag was actually given;
        # argparse cannot tell those apart with `store_true`, so an unset boolean is dropped
        # and the server's default applies. Sending `false` for every flag nobody passed
        # would make the CLI's requests differ from an agent's for no reason.
        if value is None or value is False:
            continue
        params[flag] = value
    for item in args.param:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            # Silently accepting `--param foo` set an empty-named parameter to `""`, which the
            # server then refused for a reason that had nothing to do with the typo. Raised in
            # review of #50.
            raise ValueError(f"--param needs KEY=VALUE, got {item!r}")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            # Not an error: `--param field=psi` is a bare string, and quoting every one of
            # those for JSON's benefit would be a worse command line than the one it protects.
            params[key] = raw

    if _wants_stdin(args) and not sys.stdin.isatty():
        body = sys.stdin.read().strip()
        if body:
            document = json.loads(body)
            if (args.noun, args.verb) == ("object", "create"):
                params["body"] = document
            elif (args.noun, args.verb) == ("object", "patch"):
                params["patch"] = document
            else:  # job submit: the geometry, or a whole request
                if not isinstance(document, dict):
                    raise ValueError(
                        f"`job submit` expects a JSON object on stdin, got a "
                        f"{type(document).__name__}"
                    )
                params.update(document)
    return params


def _wants_stdin(args) -> bool:
    return (args.noun, args.verb) in {
        ("object", "create"),
        ("object", "patch"),
        ("job", "submit"),
    }


def run_command(args, core: FenixSpoonCore, principal: Principal) -> int:
    """Dispatch one command and print its answer. Returns the process exit code."""
    method, positionals, _help = COMMANDS[(args.noun, args.verb)]
    try:
        params = collect_params(args, positionals)
    except json.JSONDecodeError as exc:
        print(f"the JSON on stdin did not parse: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        # A malformed `--param`, or a stdin document of the wrong shape. The caller sent
        # something wrong, so it is an invalid request like any other rather than a traceback
        # about an argument it can see. Raised in review of #50.
        print(str(exc), file=sys.stderr)
        return EXIT_CODES[rpc_errors.INVALID_PARAMS]

    try:
        result = _invoke(method, core, principal, params, detach=getattr(args, "detach", False))
    except MethodError as exc:
        return _fail(exc.code, exc.message, exc.data)
    except errors.CoreError as exc:
        rendered = rpc_errors.error_object(exc)
        return _fail(rendered["code"], rendered["message"], rendered["data"])

    payload = jsonable(result)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(payload))
    return 0


def _invoke(method: str, core: FenixSpoonCore, principal: Principal, params, *, detach: bool):
    """Call one method, blocking for the solve when the process is the one running it.

    **`job submit` and `study run` wait by default**, and that is not a convenience — it is
    the only behaviour that is not broken. With the in-process backend a solve runs on this
    process's thread pool, so a command that submitted and exited would kill the work it just
    started and leave a row saying `running` that the next startup reconciles to *failed*.
    Fire-and-forget from a one-shot process is a shell script's most natural instinct and,
    here, its most reliable way to get nothing.

    `--detach` skips the wait, and it is meaningful exactly when the backend is not local —
    `FENIXSPOON_REDIS_URL` set, workers running — because then the solve outlives this
    process by design. With the local backend it does what it says and the job dies with the
    command, which the flag's help states rather than leaving to be discovered.

    `asyncio.run` per command is right *here* and a trap elsewhere: a command is a whole
    process lifetime, so nothing later needs the same loop. The Python API
    (:mod:`fenixspoon.local`) keeps one loop precisely for the case this does not have — a
    caller that submits now and waits later.
    """
    import asyncio

    handler = METHODS[method]
    waits = method in ("job.submit", "study.run") and not detach

    async def go():
        result = handler(core, principal, params)
        if hasattr(result, "__await__"):
            result = await result
        if waits:
            await _settle(core, principal, method, result)
            result = _refresh(core, principal, method, result)
        return result

    return asyncio.run(go())


def _refresh(core: FenixSpoonCore, principal: Principal, method: str, result):
    """Re-read what was waited for, so the payload is not stale by the time it is printed.

    `job.submit` answers with the status *at submission*, which over JSON-RPC is right — the
    caller polls. Here the command has already waited, so printing `queued` for a job that
    finished before the process exited would be a true statement about a moment nobody cares
    about. The shape is unchanged; only the value is current.
    """
    if method != "job.submit":
        # A `StudyRun` says what was *started* — how many rungs began, how many the cache
        # answered — and every one of those stays true after the wait. Returning a report
        # here instead would make `study run`'s output type depend on `--detach`, so a script
        # would parse a different shape depending on a flag about timing. `study get` is the
        # report.
        return result
    job = core.job(result["job_id"], principal)
    return {**result, "status": job.status}


async def _settle(core: FenixSpoonCore, principal: Principal, method: str, result) -> None:
    """Poll until the work this command started is terminal.

    On the same loop that submitted it, which is what lets it finish at all: the in-process
    backend completes a solve through callbacks on the submitting loop.
    """
    import asyncio

    from .jobs import TERMINAL

    while True:
        if method == "job.submit":
            if core.job(result["job_id"], principal).status in TERMINAL:
                return
        elif core.study_report(result.study, principal).complete:
            return
        await asyncio.sleep(0.05)


def _fail(code: int, message: str, data: dict[str, Any]) -> int:
    """Report a refusal on stderr and choose the exit code a script can branch on."""
    detail = {key: value for key, value in data.items() if key != "type"}
    print(f"{data.get('type', 'error')}: {message}", file=sys.stderr)
    if detail:
        print(json.dumps(detail, indent=2), file=sys.stderr)
    return EXIT_CODES.get(code, FALLBACK_EXIT)


# ------------------------------------------------------------------------ rendering


def render(payload: Any, indent: int = 0) -> str:
    """A readable rendering of a result — never a different answer from `--json`.

    Deliberately generic. A formatter per operation would be twenty renderings to keep in
    step with twenty payloads, and the moment one of them lags it becomes a CLI that shows
    something the protocol does not say. Lists of objects that share keys become a table
    because that is where reading plain lines actually hurts; everything else is `key: value`.
    """
    pad = " " * indent
    if isinstance(payload, dict):
        lines = []
        for key, value in payload.items():
            if isinstance(value, dict | list) and value:
                lines.append(f"{pad}{key}:")
                lines.append(render(value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
        return "\n".join(lines)
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            return _table(payload, indent)
        return "\n".join(f"{pad}- {_scalar(item)}" for item in payload)
    return f"{pad}{_scalar(payload)}"


def _table(rows: list[dict[str, Any]], indent: int) -> str:
    """Rows that share a shape, as columns. Only the keys every row has, so a ragged list
    degrades to the keys they agree on rather than to a grid full of blanks.

    A column whose value is a **flat map** is spread into one column per key, qualified as
    `parent.key`. Still generic — no operation is named here — but it is the difference
    between a table and a header: a sweep's points carry what they varied in `values` and what
    came back in `metrics`, and dropping both left six rows of job ids under the word
    `points`. Found by running `study get` on a lift polar and reading the output, which is
    the only way this kind of hole is ever found.

    Qualified names always, never the bare key: `values` and `metrics` can hold the same name,
    and a column called `alpha` that might be either an input or an answer is worse than a
    longer heading. Lists stay dropped — a curve does not become columns.
    """
    shared = [key for key in rows[0] if all(key in row for row in rows)]
    columns: list[tuple[str, tuple[str, ...]]] = []
    for key in shared:
        if all(_is_flat_map(row[key]) for row in rows):
            columns += [
                (f"{key}.{inner}", (key, inner))
                for inner in rows[0][key]
                if all(inner in row[key] for row in rows)
            ]
        elif not isinstance(rows[0][key], dict | list):
            columns.append((key, (key,)))
    if not columns:
        return "\n".join(render(row, indent) for row in rows)
    return _grid(rows, columns, indent)


def _is_flat_map(value: Any) -> bool:
    """A non-empty dict of scalars — the only shape a column can be spread into columns."""
    return (
        isinstance(value, dict)
        and bool(value)
        and not any(isinstance(item, dict | list) for item in value.values())
    )


def _grid(
    rows: list[dict[str, Any]], columns: list[tuple[str, tuple[str, ...]]], indent: int
) -> str:
    """Lay out the chosen columns, each named by a heading and read by a path into the row."""

    def cell(row: dict[str, Any], path: tuple[str, ...]) -> str:
        value: Any = row
        for step in path:
            value = value[step]
        return _scalar(value)

    widths = {
        heading: max(len(heading), *(len(cell(row, path)) for row in rows))
        for heading, path in columns
    }
    pad = " " * indent
    header = pad + "  ".join(heading.ljust(widths[heading]) for heading, _ in columns)
    rule = pad + "  ".join("-" * widths[heading] for heading, _ in columns)
    body = [
        pad + "  ".join(cell(row, path).ljust(widths[heading]) for heading, path in columns)
        for row in rows
    ]
    return "\n".join([header, rule, *body])


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        # Six significant figures: enough to compare two solves, short enough to read. The
        # full precision is one `--json` away, which is the point of having both.
        return f"{value:.6g}"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)
