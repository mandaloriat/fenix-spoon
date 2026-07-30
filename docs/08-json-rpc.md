# JSON-RPC over stdio

The local transport. An agent spawns `fenix-spoon rpc --stdio`, writes JSON-RPC 2.0 requests
to its stdin and reads responses from its stdout. **No port is opened, no origin policy
applies, and the child dies with its parent.** It is an adapter over the same application
core the [HTTP API](04-wire-protocol.md) uses, so the two answer the same questions with the
same models — and neither is privileged.

Added in [issue #45](https://github.com/mandaloriat/fenix-spoon/issues/45). The design it
implements is [§11 of the local agent interface](07-local-agent-interface.md#11-transports).

```console
$ printf '{"jsonrpc":"2.0","id":1,"method":"capability.list"}\n' | fenix-spoon rpc --stdio
{"jsonrpc":"2.0","id":1,"result":[{"name":"mock.laplace2d", ...}]}
```

`python -m fenixspoon rpc --stdio` runs the same thing. A caller spawning this as a child
usually wants that spelling: it cannot pick up a `fenix-spoon` from some other environment
that happens to be earlier on `PATH`.

## Framing

**Written: newline-delimited JSON.** One message, one line, UTF-8.

**Read: either that or LSP-style `Content-Length` headers**, detected per message, so the two
can even be mixed on one stream. `--framing headers` switches what is *written*, for a client
whose reader cannot do NDJSON.

A newline delimiter is only safe if no message can contain one, and that is a property of the
encoder rather than a hope: `json.dumps` escapes U+000A inside strings, and ASCII escaping
also escapes U+2028 and U+2029 — the two characters Python's `readline` does not treat as
line terminators but several other languages' line splitters do. So one frame is one line
under *any* reader's definition of a line. The test that asserts this is
`test_no_encodable_value_can_break_the_frame`; without it the choice would be a preference
rather than a decision.

The other half of the choice is MCP: its stdio transport is newline-delimited, and the MCP
adapter ([#49](https://github.com/mandaloriat/fenix-spoon/issues/49)) is meant to be a thin
layer over this one rather than a re-framing of it.

## What it refuses

**Batches.** The specification has them; this does not, and says so rather than answering
with a parse error you have to interpret. On a channel that already accepts pipelined
requests, a batch buys nothing but questions the spec does not settle — is it ordered, is it
atomic, what does a partial failure return — and inventing those answers here would make
them ours rather than the protocol's. MCP dropped batching for the same reason.

**Positional params.** Params are an object with named fields. Most of these methods have
optional arguments and several have five or more, so positional binding turns "I omitted
`sections`" into "I passed `sections` where `inline_schemas` goes".

**Params of the wrong JSON type.** A `limit` that arrives as `{}`, a `since` of `-1`, an
`inline_schemas` of `"false"` — each is a `-32602` naming the parameter, not a coerced value
and not an internal error. `bool("false")` is `True` in Python and `int(True)` is `1`, so
coercion here would silently give a caller the opposite of what it asked for. Whole numbers
sent as JSON floats (`50.0`) *are* accepted: JSON has one number type, so an encoder emitting
that is being correct rather than sloppy.

**An `id` the specification does not allow.** It is String, Number or Null; an object, array
or boolean is refused with `id: null` rather than echoed, because echoing it would make the
*response* non-conformant too — the one error a client would use to find its bug would itself
be malformed.

**Anything that takes code.** No `run_python`, no command line, no package name, no image
reference — the
[security posture](02-architecture.md#security-posture-why-solvers-are-declarative) is not
relaxed by being on a pipe. Physics is a capability name, a parameter set and declared
metrics.

## Concurrency

Requests are dispatched concurrently, so **responses can arrive out of order**. JSON-RPC
correlates by `id`, so this is legal, and it is the point: a solve takes seconds to minutes,
and a channel that answered `job.get` only after the solve it started had finished would make
asynchronous jobs a fiction. A client that wants ordering gets it by waiting for each
response, which is what a synchronous client does anyway.

Solves run on the same bounded thread pool the HTTP server uses, so the event loop stays free
to answer while one is running.

## Methods

`rpc.describe` returns the list at runtime, which is the authoritative answer for a given
installation. It is small on purpose — names, protocol version, framing — so discovering the
vocabulary costs a few hundred bytes rather than every method's schema.

| Method | What it does |
|---|---|
| `rpc.describe` | protocol version, framing, and every callable method |
| `environment.inspect` | what this installation is, for the principal asking |
| `capability.list` | one line per installed capability |
| `capability.describe` | selected sections of one capability |
| `capability.schema` | the full params JSON Schema for one capability |
| `workspace.open` | where the workspace is and what is in it |
| `workspace.list` | objects, optionally filtered by type |
| `object.create` | create a typed object, return its reference |
| `object.get` | one object revision |
| `object.revisions` | which revisions exist |
| `object.patch` | apply an RFC 6902 patch, return the new revision |
| `design.resolve` | what a design resolves to, without submitting |
| `job.submit` | solve a design, or an inline solver + geometry |
| `job.get` | status, and metrics once finished |
| `job.list` | this principal's history |
| `job.cancel` | cooperative cancellation |
| `job.events` | progress log from a sequence number |
| `job.subscribe` / `job.unsubscribe` | progress as `job.progress` notifications |
| `job.provenance` | what produced this answer, and whether it was cached |
| `job.for_object` | every solve that used a workspace object |
| `result.get` | a finished job's answer, at the levels asked for |
| `result.query` | one bounded question about one field |
| `artifact.get` | resolve an artifact to a path on this machine |

`study.run` and `study.get` are in the design draft's table and are deliberately absent: the
study object does not exist yet ([#48](https://github.com/mandaloriat/fenix-spoon/issues/48)).
Binding a method to nothing so the vocabulary looks complete would be a caller asking what
exists and being told about something that does not.

### Progress: poll or subscribe

Both, because a caller with a context window and a caller with a socket want different
things.

**Poll** `job.events` with `since` and get back the events after that sequence number, plus a
`next` to pass in next time and a `final` telling you when it is over. The log is stored and
sequence-numbered, so this misses nothing — it is the same replay the WebSocket route does.

**Subscribe** with `job.subscribe` and receive `job.progress` notifications until the job
ends. Notifications carry no `id`, which is how a client tells them from the answer to
something it asked for.

An agent that does not want twenty progress ticks in its context submits and polls. The
stream exists; it is not pushed at a caller that did not ask.

### Artifacts are paths, not bytes

`artifact.get` returns an absolute path. The caller is a process on this machine — that is
the premise of the transport — so it opens the file with the tools that read VTK files.
Base64 through a pipe would be the same bytes plus a third, landing in a context window that
cannot use them.

## Errors

Typed and compact: a code, a message, and structured `data`. Never a stack trace.

```json
{"jsonrpc":"2.0","id":3,"error":{
  "code":-32602,
  "message":"job would use about 262,144 cells, over this server's limit of 100000...",
  "data":{"type":"CellBudgetExceeded","estimate":262144,"limit":100000}}}
```

`data.type` is the error's class name — the stable key to switch on when the code's
granularity is not enough, and what makes a budget refusal distinguishable from malformed
input even though both are `-32602`. The rest of `data` is the error's own structure:
pydantic's error list for a validation failure (the same list the HTTP `422` detail carries),
`retry_after` on a quota refusal where waiting actually helps.

| Code | Meaning |
|---|---|
| `-32700` | the frame did not parse — or was a batch |
| `-32600` | not a JSON-RPC request (no `jsonrpc: "2.0"`, no string `method`) |
| `-32601` | unknown method; `data.methods` lists the ones that exist |
| `-32602` | the arguments are wrong: schema, geometry kind, budget, unknown section or level |
| `-32603` | a bug in the server. The traceback goes to its log, not to you |
| `-32001` | not found: a capability, job, object or artifact |
| `-32002` | a conflict with current state: already finished, not finished, patch changed nothing |
| `-32003` | a quota refusal |
| `-32004` | the job is there and its field arrays are not |

The reserved codes are used where they genuinely mean what happened — params that fail a
schema really are "invalid params", and pretending otherwise would make a generic client's
error handling wrong. Everything else comes from the implementation-defined server range,
grouped by what the caller should *do* rather than by which class was raised.

The grouping deliberately mirrors [the HTTP status table](04-wire-protocol.md): two errors
that share a status share a code. A test asserts that, so a caller writing one error handler
for both transports is not relying on a coincidence.

## Identity and limits

A local caller resolves to a `Principal` like any other. Being able to start the process is
the authentication — the child runs as the same user and could have run the solver directly —
but it is not a bypass: jobs are owned, counted against whatever quotas are configured,
answered from the per-principal result cache, and swept by the same retention policy. One
principal's job is not visible to another.

`FENIXSPOON_RPC_PRINCIPAL` names the principal, so two agents sharing a data directory get
separate histories, quotas and caches rather than reading each other's jobs.

`FENIXSPOON_DATA_DIR` (or `--data-dir`) chooses the workspace. Everything else the server
reads from the environment — `FENIXSPOON_MAX_CELLS`, `FENIXSPOON_JOB_TIMEOUT`,
`FENIXSPOON_JOB_TTL`, `FENIXSPOON_CACHE` — applies identically here; see
[deployment](05-deployment.md).

## stdout belongs to the protocol

Logging goes to stderr, at `WARNING` by default, because anything written to stdout corrupts
the stream. `--log-level` and `FENIXSPOON_LOG_LEVEL` change it. A solver adapter that
`print()`s would break the channel, which this cannot prevent — only state.
