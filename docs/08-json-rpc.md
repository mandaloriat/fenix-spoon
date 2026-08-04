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
| `job.submit` | solve a design, or an inline solver + geometry + load case |
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
| `study.run` | submit every variation of a study |
| `study.get` | the (variation → metric) table, and where each metric settled |
| `optimize.run` | search for the value that hits an objective — runs the solves |
| `optimize.get` | the trajectory, the best point, and the bracket it narrowed to |

That is the design draft's operation table in full, plus one pair the draft did not have.
`study.run` and `study.get` were absent until
[#48](https://github.com/mandaloriat/fenix-spoon/issues/48) gave the study object a body —
binding a method to nothing so the vocabulary looks complete would be a caller asking what
exists and being told about something that does not. `optimize.*` arrived with
[#22](https://github.com/mandaloriat/fenix-spoon/issues/22) as **separate methods rather than
a third study kind**, which is the boundary #48 drew showing up in the vocabulary: a study
enumerates, an optimizer chooses, and a caller reading this table should be able to tell which
one it is asking for.

### Studies

A study **enumerates** a variation space over a base design. Two kinds are implemented and the
object says which.

**`mesh_convergence`** ([#48](https://github.com/mandaloriat/fenix-spoon/issues/48)) — one
parameter, a ladder of values, read for where the metrics stop moving:

```json
{"kind": "mesh_convergence", "design": "design:d-1", "parameter": "resolution",
 "values": [24, 32, 48, 64], "tolerance": 0.01}
```

**`sweep`** ([#21](https://github.com/mandaloriat/fenix-spoon/issues/21)) — one or more
parameters, read for what the metrics *do* across them. Either `axes`, a full factorial with
the last axis varying fastest and the first as the abscissa of the response curves, or explicit
`points` for a design a grid cannot express:

```json
{"kind": "sweep", "design": "design:d-1",
 "axes": [{"parameter": "alpha", "values": [-6, -3, 0, 3, 6, 9]}],
 "metrics": ["c_l", "c_m_c4"]}
```

`study.run` submits each variation and returns immediately with how many started, how many the
result cache answered for free, and how many the server refused. `study.get` returns the table:
per variation the values it set, the job id, the status and the metrics. A ladder adds, per
metric, the values up the rungs, the relative change between them, and **the value from which
every later change stays under the tolerance** — the sentence a convergence study exists to
produce. A sweep adds a **response curve** per metric as `Series1DData`, the same model a
`series1d` result carries, so anything that draws a curve draws these.

Two bounds a sweep has and a ladder does not. It carries at most **64 points**, counted from
the axis lengths and never enumerated — a grid multiplies, and four axes of four values is 256
solves from a body that fits on one line. And its **first axis needs at least two values**,
because it is the abscissa and a curve needs two points.

Four things worth knowing before you write one:

- **A name the study gets wrong is refused, not tabulated.** Both the parameter and the
  metric names are checked against what the capability declares, on the run path and the read
  path alike. A metric column of nulls would read as "this capability reports `c_1` and the
  solve did not produce it" — confident and wrong — where a refusal naming `c_l` sends you to
  the typo.
- **The parameter is named, not inferred.** No capability declares which of its parameters
  controls the mesh, and guessing from the name would be wrong for the first adapter that
  spells it differently. A name the capability does not have is refused — a solver's params
  model ignores unknown fields, so an unchecked typo would submit every rung identically, the
  cache would collapse them onto one job, and the table would show a *perfectly converged*
  answer that is entirely fabricated. Over a grid the same typo is worse: it collapses every
  point that differs only along that axis, and the table reads as a parameter with no effect.
- **Each variation is an ordinary submission.** It obeys the cell budget and the quota per job,
  not per study, and an already-computed one costs nothing — which is why widening a sweep from
  six angles to eight costs two solves. One the server refuses does not fail the study: rung 4
  exceeding the budget says nothing about rungs 1–3.
- **No randomized design is generated here.** A Latin hypercube or a Sobol sequence arrives as
  explicit `points`, because generating one server-side would break the property below unless
  the seed were frozen in the body — at which point the caller is specifying the design anyway.
- **It does not choose the next point.** That is an optimizer and it is M5
  ([#22](https://github.com/mandaloriat/fenix-spoon/issues/22)). The line is drawn at a
  concrete place: a study's job list is a pure function of its object revision, so you can say
  which solves it implies without running any of them. An optimizer cannot promise that.

### Optimization

Where a study **enumerates** a variation space, an optimization **searches** one: it chooses
its next point from what the last one answered. That is the whole difference, and it is why
these are separate methods — given `study:s-1@2` you can say which solves it implies without
running any of them, and no optimization can promise that.

```json
{"design": "design:d-1", "parameter": "alpha", "bounds": [-10, 10],
 "objective": {"metric": "c_l", "sense": "target", "target": 0.0},
 "max_evaluations": 12, "tolerance": 0.02}
```

`sense` is `minimize`, `maximize` or `target`; all three become one minimisation internally —
negated, or squared distance from the target — so the method never learns what the caller
meant. The objective is a **declared metric**, refused if the capability does not declare it,
for the reason a study refuses an unknown column.

Four things worth knowing before you write one:

- **`optimize.run` waits.** It is the only method whose duration is the work. `study.run`
  hands back every job id immediately because it knows them all; a search cannot name its
  second point until the first is answered, so this returns the finished trajectory rather
  than a receipt for one.
- **Every evaluation is an ordinary submission.** Cell budget, quota and result cache apply
  per job exactly as they do for a rung — which is why running the same optimization twice
  costs nothing. The second pass replays the identical sequence and every point is a cache
  hit, and that is also how `optimize.get` recovers a trajectory nobody stored.
- **An evaluation with no answer stops the search.** A study tabulates what it has and marks
  rung 4 refused; here the next point is a function of the missing value, so there is nowhere
  to continue from. `stopped` says which of `converged`, `budget`, `stalled` or `not_run`.
- **`bracket` is not `best`.** The best evaluation is where the lowest value was *seen*; the
  bracket is where the minimum is *known to be*. A search that stopped on its budget has a
  bracket wider than its tolerance, and reading only the best point would take that for a
  located answer.

The method is bounded scalar golden-section search, which assumes the objective is
**unimodal** on the bracket. Given two minima it converges to one of them and says nothing
about the other — an assumption the caller checks, not a detail.

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
