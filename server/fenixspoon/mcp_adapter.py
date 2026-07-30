"""Model Context Protocol adapter — one file, no application logic (roadmap M2.5, #49).

MCP hosts get the same operations the JSON-RPC transport exposes. That is the whole design,
and the issue states the test it has to pass: *if MCP is replaced in two years, one file is
deleted.* So this is one file, and it contains no rule the rest of the server does not already
enforce.

## It maps onto the JSON-RPC method table, not onto the core

The obvious implementation binds each tool to a `FenixSpoonCore` call. This binds each tool to
an entry in :data:`fenixspoon.rpc.methods.METHODS` instead, and the difference is not
stylistic: two adapters calling the core independently is two places for the same operation to
be spelled slightly differently, and #51 exists because that divergence is expensive to find
later. Here a tool *is* an RPC method plus a schema, so "the same request over MCP and over
JSON-RPC produces the same result" is true by construction rather than by test.

The consequence worth stating: MCP inherits the parameter typing, the error mapping and the
compact-answer rules from #45 for free, and cannot drift from them.

## The vocabulary is a curated subset, not the whole table

Twenty-six RPC methods, thirteen tools. A host's tool list is something a model reads *every turn*,
so it is the one surface where "expose everything" is actively wrong — `job.for_object`,
`object.revisions` and `design.resolve` are real operations that a host can reach through the
JSON-RPC transport and that would spend context here without earning it.

**No tool per solver, and no tool per physics.** A new adapter must become usable without
touching this file, exactly as it becomes usable over HTTP without an API change. That is why
there is no `solve_magnetostatics`: the physics is a capability name inside `submit_job`.

## Artifacts are resources *and* paths

The design draft leaves this open and asks for it to be settled "against real host behavior,
not against the specification alone". This ships both and does **not** claim the question is
closed, because the deciding evidence is exactly what cannot be gathered here.

What is implemented: every artifact of every finished job is listed as an MCP resource under a
`fenix-spoon://job/<id>/<name>` URI, which is the idiomatic answer and lets a host fetch
lazily; and `get_artifact` still returns the filesystem path, which is what #45 established
and what a host with filesystem access actually wants for a 40 MB VTK file. A resource read of
a binary artifact returns its path and size rather than base64 — see :func:`read_resource`
for why that is not a cop-out.
"""

import json
from typing import Any

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)

from . import __version__
from .core import FenixSpoonCore, errors
from .core.identity import Principal
from .core.results import FieldQuery
from .rpc import errors as rpc_errors
from .rpc.encoding import jsonable
from .rpc.methods import METHODS, MethodError, SubmitParams

#: URI scheme for artifacts exposed as resources. Its own scheme rather than `file://` on
#: purpose: a host that cannot reach this machine's filesystem should fail to fetch it rather
#: than silently read some *other* file that happens to exist at the same path.
SCHEME = "fenix-spoon"


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """A JSON Schema object for a tool's arguments.

    Hand-written for the simple tools and taken from the pydantic model for the two that have
    one. Deriving all of them would mean inventing a model per tool whose only job is to be
    introspected — the RPC layer validates these arguments anyway, so a schema here is
    documentation for the host, not a second gate.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_REF = {"type": "string", "description": "A workspace reference, e.g. `design:d-1`."}
_JOB = {"type": "string", "description": "A job id, as `submit_job` returned it."}


#: The tool vocabulary: MCP tool name → (RPC method, description, input schema).
#:
#: Ordered as a host reads it — what exists, what it can do, then how to do it — because the
#: list is prose to a model as much as it is an API.
TOOLS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "inspect_environment": (
        "environment.inspect",
        "What this installation is: capabilities installed, limits, quotas and their current "
        "usage, and whether results are cached. Start here.",
        _schema({}),
    ),
    "list_capabilities": (
        "capability.list",
        "One line per installed simulation capability — name, title, physics, the geometry "
        "kinds it accepts. No schemas: use describe_capability for those.",
        _schema({}),
    ),
    "describe_capability": (
        "capability.describe",
        "Selected sections of one capability. Ask for the sections you need — `params`, "
        "`metrics`, `geometries`, `cost`, `assumptions`, `examples` — rather than all of "
        "them; omitting `sections` returns everything and is rarely what you want.",
        _schema(
            {
                "name": {"type": "string", "description": "Capability name."},
                "sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sections to include. Omit for all.",
                },
                "inline_schemas": {
                    "type": "boolean",
                    "description": "Return the full params JSON Schema inline. Costs "
                    "kilobytes; omit unless you are generating a request.",
                },
            },
            ["name"],
        ),
    ),
    "create_object": (
        "object.create",
        "Create a workspace object — `geometry`, `material`, `design`, or `study` — and get "
        "back a stable reference to name it by later.",
        _schema(
            {
                "type": {"type": "string", "description": "Object type."},
                "body": {"type": "object", "description": "The object itself."},
                "label": {"type": "string", "description": "Optional human label."},
            },
            ["type", "body"],
        ),
    ),
    "get_object": (
        "object.get",
        "One workspace object, body included. A reference may pin a revision: "
        "`geometry:g-1@3`.",
        _schema({"ref": _REF}, ["ref"]),
    ),
    "patch_object": (
        "object.patch",
        "Apply an RFC 6902 JSON Patch and write the next revision. This is how you iterate: "
        "send the one edit, not the whole object.",
        _schema(
            {
                "ref": _REF,
                "patch": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "RFC 6902 operations, e.g. "
                    '[{"op":"replace","path":"/params/resolution","value":128}].',
                },
                "label": {"type": "string"},
            },
            ["ref", "patch"],
        ),
    ),
    "submit_job": (
        "job.submit",
        "Solve a design by reference, or an inline solver + geometry for one-shot use. "
        "Returns immediately; poll with inspect_job. An identical solve that already ran is "
        "returned rather than recomputed, and says so.",
        SubmitParams.model_json_schema(),
    ),
    "inspect_job": (
        "job.get",
        "A job's status, and its metrics once it has finished.",
        _schema({"job_id": _JOB}, ["job_id"]),
    ),
    "get_result": (
        "result.get",
        "A finished job's answer at the levels you ask for. The default omits the field "
        "arrays, which is what keeps it readable — ask for `fields` only if you truly need "
        "megabytes of numbers.",
        _schema(
            {
                "job_id": _JOB,
                "levels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Any of `status`, `metrics`, `diagnostics`, `provenance`, "
                    "`fields`, `artifacts`.",
                },
            },
            ["job_id"],
        ),
    ),
    "query_result": (
        "result.query",
        "Ask one bounded question of one result field — max, min, mean, integral, value at a "
        "point, statistics over a named region, a section, a decimated sample, or hotspots. "
        "The answer is a number and a coordinate; the array stays on the server.",
        {**FieldQuery.model_json_schema(), "properties": {
            **FieldQuery.model_json_schema()["properties"],
            "job_id": _JOB,
        }},
    ),
    "get_artifact": (
        "artifact.get",
        "Resolve a job's artifact to a path on this machine. Artifacts are also listed as "
        "resources, so a host that prefers to fetch lazily can read them that way instead.",
        _schema(
            {"job_id": _JOB, "name": {"type": "string", "description": "Artifact filename."}},
            ["job_id", "name"],
        ),
    ),
    "run_study": (
        "study.run",
        "Run a study — a base design, one parameter, a ladder of values. Submits every rung "
        "and returns how many started and how many the cache answered for free.",
        _schema({"ref": _REF}, ["ref"]),
    ),
    "get_study": (
        "study.get",
        "A study's table: per rung the value, the job and its metrics; per metric where it "
        "settled. This is the whole answer — it does not need a follow-up per rung.",
        _schema({"ref": _REF}, ["ref"]),
    ),
}

# Deliberately absent, and each for a reason rather than by oversight. `job.list`,
# `job.for_object`, `object.revisions` and `design.resolve` are real operations reachable over
# JSON-RPC; here they would spend a model's context every turn to answer questions it rarely
# has. `job.cancel` is absent because an MCP host has no stable handle on a job it started in
# a previous turn and cancelling the wrong one is worse than waiting. `job.subscribe` has no
# meaning without a connection to push into.


def tool_list() -> list[Tool]:
    return [
        Tool(name=name, description=description, input_schema=schema)
        for name, (_method, description, schema) in TOOLS.items()
    ]


async def call_tool(
    core: FenixSpoonCore, principal: Principal, name: str, arguments: dict[str, Any] | None
) -> CallToolResult:
    """Run one tool by delegating to the JSON-RPC handler behind it.

    Errors come back as `isError` with the same structured `data` the JSON-RPC transport
    sends, rather than as a protocol-level failure. That is the MCP convention and it is also
    the useful one: a model that asked for a capability that does not exist needs to read the
    list of ones that do, not to see the connection fault.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return _error(
            {"type": "UnknownTool", "tool": name, "tools": sorted(TOOLS)},
            f"unknown tool {name!r}",
        )
    method, _description, _schema_ = entry

    try:
        result = METHODS[method](core, principal, arguments or {})
        if hasattr(result, "__await__"):
            result = await result
    except MethodError as exc:
        return _error(exc.data, exc.message)
    except errors.CoreError as exc:
        rendered = rpc_errors.error_object(exc)
        return _error(rendered["data"], rendered["message"])

    payload = jsonable(result)
    return CallToolResult(
        # Both forms. `structuredContent` is what a host that understands it should read;
        # `content` is the text fallback, and hosts still differ on which they use. Emitting
        # only one would make this adapter's usefulness depend on the host's version.
        content=[TextContent(type="text", text=json.dumps(payload, indent=None))],
        structured_content=payload if isinstance(payload, dict) else {"result": payload},
    )


def _error(data: dict[str, Any], message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"error": message, **data}))],
        is_error=True,
    )


def artifact_resources(core: FenixSpoonCore, principal: Principal) -> list[Resource]:
    """Every artifact of every finished job this principal owns.

    Scoped to the principal for the reason every other listing is: a job id is itself a
    disclosure, and a resource list is a job listing wearing a different name.
    """
    resources = []
    jobs, _total = core.history(principal, limit=100)
    for job in jobs:
        for entry in job.artifacts:
            resources.append(
                Resource(
                    uri=f"{SCHEME}://job/{job.id}/{entry['name']}",
                    name=f"{job.id} · {entry['name']}",
                    description=f"{entry['name']} from {job.solver_name}",
                    mimeType=entry.get("content_type", "application/octet-stream"),
                    size=int(entry.get("size", 0)),
                )
            )
    return resources


def read_resource(core: FenixSpoonCore, principal: Principal, uri: str) -> ReadResourceResult:
    """Resolve an artifact URI.

    **Text artifacts are returned inline; binary ones are described, not base64-encoded.**
    That is a deliberate refusal rather than a missing feature. A legacy-VTK field export is
    tens of megabytes; base64 makes it a third larger again, and delivering that to a host
    whose budget is a context window is not "supporting binary resources", it is destroying
    the session to prove a point. The path is on this machine — the premise of a local
    transport — so what comes back is where to find it and how big it is.
    """
    job_id, _, name = uri.removeprefix(f"{SCHEME}://job/").partition("/")
    handle = core.artifact(job_id, name, principal)
    text = handle.content_type.startswith("text/") or handle.path.suffix in (".vtk", ".json")
    if text and handle.size <= 64_000:
        body = handle.path.read_text(errors="replace")
    else:
        body = json.dumps(
            {
                "path": str(handle.path),
                "size": handle.size,
                "content_type": handle.content_type,
                "note": (
                    "not inlined: read it from `path`. Inlining would cost a third more than "
                    "the file and land in a context window that cannot use it."
                ),
            }
        )
    return ReadResourceResult(
        contents=[TextResourceContents(uri=uri, mimeType=handle.content_type, text=body)]
    )


def build_server(core: FenixSpoonCore, principal: Principal) -> Server:
    """An MCP server over this core. Handlers are closures; there is no adapter state."""

    async def on_list_tools(_context, _params) -> ListToolsResult:
        return ListToolsResult(tools=tool_list())

    async def on_call_tool(_context, params) -> CallToolResult:
        return await call_tool(core, principal, params.name, params.arguments)

    async def on_list_resources(_context, _params) -> ListResourcesResult:
        return ListResourcesResult(resources=artifact_resources(core, principal))

    async def on_read_resource(_context, params) -> ReadResourceResult:
        try:
            return read_resource(core, principal, str(params.uri))
        except errors.CoreError as exc:
            # A resource read has no `isError` channel, so a missing artifact has to be an
            # exception. The message is the domain one rather than a traceback.
            raise ValueError(exc.detail) from exc

    return Server(
        name="fenix-spoon",
        version=__version__,
        instructions=(
            "Simulation capabilities are declarative: pick one with list_capabilities, read "
            "its parameters with describe_capability, then submit_job. Keep designs in the "
            "workspace and iterate with patch_object rather than resending geometry. Results "
            "are compact by default — use query_result for a number, and only ask for the "
            "`fields` level if you genuinely need the arrays."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )


async def serve_stdio(core: FenixSpoonCore, principal: Principal) -> None:
    """Run the MCP server on this process's own pipes."""
    server = build_server(core, principal)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="fenix-spoon",
                server_version=__version__,
                capabilities=server.get_capabilities(),
            ),
        )
