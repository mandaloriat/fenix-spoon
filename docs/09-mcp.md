# MCP adapter

A [Model Context Protocol](https://modelcontextprotocol.io) server over the same operations
the [JSON-RPC transport](08-json-rpc.md) exposes. An adapter and nothing more: it maps tool
calls onto the JSON-RPC method table and formats errors, and it contains no rule the rest of
the server does not already enforce.

Added in [issue #49](https://github.com/mandaloriat/fenix-spoon/issues/49).

```console
$ pip install 'fenixspoon[mcp]'
$ fenix-spoon mcp --stdio
```

**MCP is an optional extra.** The HTTP API, the JSON-RPC transport and the whole test suite
build and pass without it — that is a requirement of the issue rather than a preference, and
`test_nothing_but_the_adapter_imports_mcp` checks it in a subprocess so the property survives
an environment that happens to have the package installed.

Configure a host to spawn it the way it spawns any other stdio server:

```json
{"mcpServers": {"fenix-spoon": {
  "command": "fenix-spoon", "args": ["mcp", "--stdio"],
  "env": {"FENIXSPOON_DATA_DIR": "/path/to/workspace"}}}}
```

## It maps onto JSON-RPC, not onto the core

The obvious implementation gives each tool its own call into `FenixSpoonCore`. This one binds
each tool to an entry in the JSON-RPC method table instead, and the difference is not
stylistic: two adapters calling the core independently is two places for one operation to be
spelled slightly differently, and finding that divergence later is what
[#51](https://github.com/mandaloriat/fenix-spoon/issues/51) exists for.

Here a tool **is** an RPC method plus a schema. So "the same request over MCP and over
JSON-RPC produces the same result" is true by construction, and MCP inherits the parameter
typing, the error mapping and the compact-answer rules for free.

## Thirteen tools, not twenty-six

The vocabulary is a curated subset. A host's tool list is something a model reads *every
turn*, which makes this the one surface where exposing everything is actively wrong.

| Tool | What it does |
|---|---|
| `inspect_environment` | what this installation is — capabilities, limits, quotas, cache |
| `list_capabilities` | one line per capability, no schemas |
| `describe_capability` | selected sections of one capability |
| `create_object` | create a geometry, material, load case, design or study |
| `get_object` | one object revision |
| `patch_object` | apply an RFC 6902 patch, write the next revision |
| `submit_job` | solve a design, or an inline solver + geometry + load case |
| `inspect_job` | status, and metrics once finished |
| `get_result` | the answer at the levels you ask for |
| `query_result` | one bounded question about one field |
| `get_artifact` | resolve an artifact to a path |
| `run_study` | submit every variation of a study — a ladder or a sweep |
| `get_study` | the (variation → metric) table, and a sweep's response curves |

Absent on purpose, each for a reason: `job.list`, `job.for_object`, `object.revisions` and
`design.resolve` are real operations reachable over JSON-RPC that would spend a model's
context every turn to answer questions it rarely has; `job.cancel` because a host has no
stable handle on a job it started in a previous turn, and cancelling the wrong one is worse
than waiting; `job.subscribe` because it has no meaning without a connection to push into.

**No tool per solver, and no tool per physics.** A new solver adapter becomes usable without
touching the MCP surface, exactly as it becomes usable over HTTP without an API change. There
is no `solve_magnetostatics` — the physics is a capability name inside `submit_job`. A test
asserts this against the *installed* capabilities rather than a hard-coded list, so writing
such a tool would fail the build.

## Errors are tool results, not protocol faults

A domain failure comes back with `isError` set and the same structured `data` the JSON-RPC
transport sends:

```json
{"error": "unknown solver: 'nope'", "type": "UnknownCapability", "name": "nope"}
```

That is the MCP convention and it is also the useful one: a model that asked for a capability
that does not exist needs to read the list of ones that do, not to see the connection fault.

Every result carries **both** `content` (text) and `structuredContent`. Hosts still differ on
which they read, and emitting only one would make this adapter's usefulness depend on the
host's version.

## Artifacts: resources *and* paths

Every artifact of every finished job is listed as an MCP resource under a
`fenix-spoon://job/<id>/<name>` URI — its own scheme rather than `file://`, so a host that
cannot reach this machine's filesystem fails to fetch it instead of silently reading some
*other* file at the same path. Resource listings are scoped to the principal, because a
resource list is a job listing wearing a different name.

`get_artifact` also still returns the filesystem path, which is what a host with filesystem
access actually wants for a 40 MB VTK file.

**A large artifact is described, not base64-encoded.** Reading one as a resource returns its
path, size and content type rather than its bytes. That is a deliberate refusal: base64 makes
a file a third larger and delivers it into a context window that cannot use it. Small text
artifacts (under 64 kB) do arrive inline.

Whether resources, paths, or both is the right long-term answer is
[still open](07-local-agent-interface.md#15-open-questions). The design draft asks for it to be
settled against real host behaviour rather than against the specification, and that evidence
is exactly what a test suite cannot produce — so both ship, and the question stays open
honestly rather than being closed by assertion.

## Identity

An MCP host is a caller like any other. Jobs and objects are owned by a `Principal`, quotas
apply, and one principal's work is not visible to another. `FENIXSPOON_RPC_PRINCIPAL` names
the principal, so two hosts sharing a data directory get separate histories, quotas and
caches. Everything under [deployment](05-deployment.md) applies unchanged.
