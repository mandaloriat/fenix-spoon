# Local agent interface — design draft

!!! success "Implemented — M2.5 is complete"

    This was written as a design draft, so that [M2.5](03-roadmap.md)'s issues could be
    written against something concrete. **The milestone has now shipped**, so where an example
    below differs from what exists, the implementation is the accurate one — the differences
    are noted inline, and the transports have reference pages of their own:
    [JSON-RPC over stdio](08-json-rpc.md), [MCP adapter](09-mcp.md) and
    [CLI and Python API](10-cli-and-python.md). The HTTP contract remains the
    [wire protocol](04-wire-protocol.md).

    The draft is kept rather than rewritten because its value now is the *reasoning* — what
    each open question was weighed against, and what settled it — which a tidied-up
    specification would delete.

    **What landed:** the transport-neutral core §11 depends on
    ([#42](https://github.com/mandaloriat/fenix-spoon/issues/42)); capability discovery §6 —
    `environment.inspect`, `capability.list`, `capability.describe`, bound to HTTP in protocol
    1.2 ([#43](https://github.com/mandaloriat/fenix-spoon/issues/43)); the workspace §8 —
    typed versioned objects, RFC 6902 patches, submission by design reference, reachable
    through the core only ([#44](https://github.com/mandaloriat/fenix-spoon/issues/44)); and
    compact results §10 — the five levels, metric values, diagnostics and the nine bounded
    field queries, bound to HTTP as protocol 1.3
    ([#46](https://github.com/mandaloriat/fenix-spoon/issues/46)); and the **content-addressed
    cache** §9 — identity, reuse and provenance, protocol 1.4
    ([#47](https://github.com/mandaloriat/fenix-spoon/issues/47)); and the **JSON-RPC over
    stdio transport** §11 — twenty-four methods, no port, no FastAPI
    ([#45](https://github.com/mandaloriat/fenix-spoon/issues/45)), documented at
    [JSON-RPC over stdio](08-json-rpc.md); and the **study abstraction** §7 —
    `mesh_convergence` first ([#48](https://github.com/mandaloriat/fenix-spoon/issues/48)) and
    `sweep` since ([#21](https://github.com/mandaloriat/fenix-spoon/issues/21)), both
    orchestrated through object references and compact results; and the **MCP adapter**
    §11 — thirteen tools over the JSON-RPC method table, an optional extra
    ([#49](https://github.com/mandaloriat/fenix-spoon/issues/49)), documented at
    [MCP adapter](09-mcp.md); and the **CLI and Python API** §11
    ([#50](https://github.com/mandaloriat/fenix-spoon/issues/50)), documented at
    [CLI and Python API](10-cli-and-python.md); and the **conformance suite and vertical
    slice** ([#51](https://github.com/mandaloriat/fenix-spoon/issues/51)), the exit criterion —
    the loop in §14 runs as a test against both the mock and the FEniCSx solver, from a process
    with no web framework imported.

    Six of §15's open questions are settled as a result and are marked there; a seventh is
    partly settled and says which part. What is still open is not M2.5 work: the MCP
    resources-versus-paths question, which wants a real host to watch rather than more
    argument. *"The study kinds beyond the first" was the other one, and #21 answered it by
    adding one — no new operation, no new binding, and the ladder untouched, which is the
    evidence the abstraction was drawn in the right place rather than merely early.*

## 1. Motivation

Fenix Spoon exists because putting FEniCSx behind a web page means hand-rolling the same stack
every time. That framing is still true and still the entry point — but it describes one *client*,
not the product. What the project actually builds is a **queryable runtime and protocol for
constructing, running and automating FEniCSx-based engineering simulation workflows**. The browser
is one consumer of it.

The consumer this document is about is a program on the same machine: a script, a CI step, a
notebook, or a software agent. Such a caller already has FEniCSx available (in a conda environment
or a container) and does not want a web application; it wants a structured way to ask *what can
this environment simulate*, *run this*, *how did it come out*, and *give me the file*.

Today that caller has three bad options:

1. **Drive the HTTP API.** Workable, and after M3 it is a genuinely solid server — but it forces a
   process and a port for what is a local, single-user interaction, and the answers are shaped for
   a viewer widget: a `grid2d` result is tens of thousands of floats, exactly the wrong payload for
   a caller with a bounded context window.
2. **Import `fenixspoon` and call internals.** Closer than it used to be: M3 pulled execution,
   persistence, event delivery and identity out of the API layer, so `ExecutionBackend`,
   `JobStore`, `EventBus` and `Principal` are callable objects. But the *request* path never
   moved — solver lookup, geometry-kind checking, params validation, the cell budget, quota checks
   and error mapping are route bodies in `api.py` that raise `HTTPException`. There is no
   supported way to submit a validated job without FastAPI.
3. **Write dolfinx code and run it.** This is what people do, and it is precisely the failure mode
   the project's [security posture](02-architecture.md#security-posture-why-solvers-are-declarative)
   rejects for clients: unvalidated arbitrary execution, no discoverable schema, no provenance, no
   cache identity, nothing reusable between runs.

The gap is not "AI support". It is that the request half of the application is reachable through
exactly one transport, and that its answers are sized for pixels rather than for decisions.

## 2. Use cases

- **Agent-driven design iteration.** An agent is asked to thicken an airfoil until a lift proxy
  stops improving. It discovers the potential-flow capability, creates a design, solves, reads
  three scalars, patches one geometry parameter, solves again, and compares — with only the
  scalars and object ids ever entering its context.
- **Mesh convergence as a checked routine.** A script runs the same design at increasing mesh
  resolution and reports where the metric of interest stabilizes, reusing cached results for the
  resolutions already computed.
- **Batch regression in CI.** A repository of designs is re-solved on every solver change; the job
  compares metrics against stored baselines and fails on drift. No server, no browser.
- **Interactive shell use.** An engineer runs `fenix-spoon capability describe
  dolfinx.potential_flow2d --section params` and `fenix-spoon result query result:r-105 --op max
  --field speed` while debugging, hitting the same code path an agent would.
- **Notebook / Python scripting.** A user imports the Python API in a Jupyter kernel that already
  has dolfinx, and gets the same objects, validation and caching as every other transport.
- **MCP host integration.** A desktop assistant with an MCP client connects to a local Fenix Spoon
  and gets a handful of stable tools rather than a Python sandbox.

## 3. Principles

1. **The protocol is the product.** Unchanged from the [architecture](02-architecture.md): the
   value is the contract, not any one implementation of a client.
2. **Solvers stay named adapters with typed parameters.** The local interface adds no way to
   describe *how* to solve, only *what*, from a server-defined menu.
3. **The core is transport-neutral.** HTTP/WebSocket, JSON-RPC over stdio, CLI, Python and MCP are
   adapters over one application core with one set of models and one set of errors.
4. **It must work entirely locally.** No network port required, no Redis, no API keys, no shared
   volume. The base local transport is a child process speaking over pipes.
5. **It must work without FEniCSx.** Mock solvers keep every operation exercisable in a plain
   virtualenv, exactly as they do for the browser path today.
6. **It must use the FEniCSx that is there.** When dolfinx imports, the real adapters register
   themselves and the same operations run the real solve — no separate execution path, no
   installation step, no container orchestration.
7. **Reuse the M3 seams, don't fork them.** `ExecutionBackend`, `EventBus`, `JobStore` and
   `Principal` already exist and already cross process boundaries. The local interface is a new
   caller of them, not a second implementation.
8. **Answers are compact by default.** Scalars and diagnostics first; fields by reference or by
   query. A caller asks for volume, it never arrives unrequested.
9. **Discovery is progressive.** Ask for the section you need. Full schemas are fetched by
   reference or on explicit request.
10. **State lives in a workspace, not in messages.** Objects have stable ids; iterations send
    patches and references, not whole geometries.
11. **No arbitrary execution.** Engineering operations and domain objects only.

## 4. Non-goals

Explicitly out of scope for this interface — not "later", but *not this*:

- **Arbitrary Python, UFL or shell execution.** No `run_python(code)`, `run_ufl(source)`,
  `execute_shell(command)`. Sandboxed opt-in arbitrary UFL remains a separate M5 experiment (#24)
  with its own threat model, and would not be reachable through this vocabulary.
- **Client-requested package installation** (`install_package(name)`). The environment is
  inspected and reported, never mutated.
- **Starting arbitrary container images** (`start_container(image)`). Deployment chooses the
  runtime; the interface describes it.
- **A remote notebook or REPL.** This is not a code-execution channel with extra steps.
- **Replacing the HTTP API.** The browser path is a first-class consumer and keeps its contract.
- **A mandatory MCP dependency.** MCP is one adapter; the core must build, test and run without it.
- **General-purpose HPC orchestration.** No cluster scheduling, no job arrays across nodes, no
  resource brokering. MPI-capable solvers may *report* that capability; running them across a
  cluster is somebody else's tool.
- **A universal optimizer.** M2.5 ships a study abstraction and one small study kind. Optimization
  loops stay M5 (#22).
- **Autonomous interpretation of unstructured engineering requirements inside the core.** The core
  does not read "make it lighter but keep the safety factor" and decide what to do. Agents
  translate human goals into typed requests; the core validates and executes defined engineering
  operations, and says no when a request does not typecheck.

## 5. Object model

The workspace holds typed objects with stable identifiers of the form `<type>:<id>`:

| Type | Holds | Notes |
|---|---|---|
| `geometry` | a payload of a protocol geometry kind (`domain2d`, `regions2d`, …) | validated by the existing pydantic models |
| `material` | named scalar properties (`mu_r`, `k`, `rho`, …) | the open-dict convention of `regions2d.material`, promoted to a reusable object |
| `boundary_condition` | a named condition bound to a boundary tag | still thin: what this was reaching for turned out to be `load_case`, and nothing reads this type |
| `load_case` | what happens on each boundary the geometry names — `{"root": {"fixed": 1}}` | validated since #85; the keys are open per capability, and each capability declares the ones it reads |
| `design` | a geometry reference + material/BC/load-case references + solver params | the unit an iteration patches |
| `study` | a study kind + its base design + a variation spec | orchestrates several jobs |
| `job` | one submitted solve | **already exists and is already durable** (`JobRecord` in `store.py`) |
| `result` | metrics, diagnostics, provenance, references to fields and artifacts | the compact object an agent reads; the payload itself is already stored on disk |
| `artifact` | a file produced by a solve (VTK, VTU, mesh, log) | **already exists** per job, under the data directory |

Identifiers look like `geometry:g-42`, `design:d-18`, `study:s-9`, `result:r-105`, `job:j-8f3adc…`
(job ids keep their current format). Every operation that would otherwise take a payload accepts a
reference instead:

```json
{"method": "job.submit", "params": {"design": "design:d-18", "solver": "mock.laplace2d"}}
```

The last three rows matter for scope: jobs, results and artifacts already have durable storage,
retention and ownership. The workspace **extends `JobStore` with the new object types**; it does
not open a second database next to `jobs.db` with its own lifetime rules.

Objects are **versioned, not mutated in place**: `object.patch` produces a new revision and returns
its id or revision tag, so a result's provenance can name exactly what it was computed from. The
patch format is expected to be [JSON Patch (RFC 6902)](https://datatracker.ietf.org/doc/html/rfc6902)
or an equivalent standardized mechanism — see the open questions.

## 6. Capability discovery

!!! success "Implemented (#43), with two departures from the draft below"

    Both are worth recording. **The metrics section declares, it does not compute** — a caller
    learns that `mock.heat2d` reports `t_max` before running anything, and the values arrive
    with the result levels of [#46](https://github.com/mandaloriat/fenix-spoon/issues/46). To
    stop that being an aspirational list, a metric that is a reduction of a result field names
    the field and the reduction, and a test runs a real solve and fails if the field is not
    there. **And the `params` section's flat list is not smaller than the schema it summarises**
    — measured, it is marginally larger on today's adapters. What it removes is `$ref`
    indirection; the size win is `capability.list` and not sending unrequested sections.

A *capability* is what a solver adapter offers, described in sections so a caller can ask for the
part it needs. Today `GET /api/v1/solvers` returns everything about every solver, full JSON Schemas
included — right for a form generator, wasteful for a caller that wants to know whether
magnetostatics is available at all.

Three operations:

- **`environment.inspect`** — what this installation *is*: Fenix Spoon version, protocol version,
  whether dolfinx and gmsh imported and at what versions, MPI availability, execution backend
  (in-process pool or workers), store backend, workspace location, configured limits (job timeout,
  cell budget, TTL, quotas), cache state. A few hundred bytes, no schemas.
- **`capability.list`** — the installed capabilities as identity plus one line each: name, title,
  physics tag, accepted geometry kinds, availability (`mock` / `fenicsx`). No schemas.
- **`capability.describe`** — one capability, with a `sections` argument selecting from
  `geometries`, `params`, `metrics`, `artifacts`, `cost` (does it implement `estimate_cells`, and
  what does the server's budget allow), `features` (sweep / gradient / MPI support),
  `requirements`, `examples`. Unspecified sections are omitted; large schemas are returned as a
  reference (`schema:params/dolfinx.potential_flow2d`) that a further call resolves, or inline only
  when explicitly requested.

```json
{"method": "capability.describe",
 "params": {"capability": "dolfinx.potential_flow2d", "sections": ["metrics", "cost"]}}
```

The `metrics` section is new and load-bearing: it is how a caller learns that this capability can
report `speed_max`, `circulation`, `lift_proxy` — with unit and meaning — *before* running
anything. Solver adapters declare their metrics the way they declare `Params` today. The `cost`
section is nearly free: `Solver.estimate_cells` already exists for the submit-time budget check,
and exposing it lets a caller size a request instead of discovering the limit by being refused.

## 7. Minimum operation set

The whole vocabulary should stay small enough to hold in one page. A first cut:

| Operation | Purpose |
|---|---|
| `environment.inspect` | what this installation can do at all |
| `capability.list` | installed capabilities, one line each |
| `capability.describe` | selected sections of one capability |
| `workspace.open` | open/create a workspace at a path, return its id and summary |
| `workspace.list` | objects in the workspace, filtered by type |
| `object.create` | create a typed object, return its id |
| `object.get` | fetch an object (optionally a section of it) |
| `object.patch` | apply a patch, return the new revision |
| `job.submit` | submit a solve for a design (or an inline geometry + params) |
| `job.get` | status, and metrics once finished |
| `job.events` | progress stream (or poll `since`, see transports) |
| `job.cancel` | cooperative cancellation |
| `result.query` | compact queries over a result's fields |
| `artifact.get` | resolve an artifact reference to a path or bytes |
| `study.run` | run a study over a design *(implemented — `mesh_convergence` #48, `sweep` #21)* |
| `study.get` | study status and its per-job result references *(implemented, #48)* |

Deliberately absent: anything that takes code, a command line, a package name or an image
reference. Deliberately *not* per-physics: there is no `solve_magnetostatics` — the physics is a
capability name, a parameter set and declared metrics, which is what makes a new solver adapter
automatically available to every caller, exactly as
[writing an adapter](start-write-a-solver.md) makes it available to the HTTP API today.

### Study kinds

Two operations, two kinds, and the second is the reason the first was written as a *study*
rather than as a mesh ladder.

| Kind | The question | The answer |
|---|---|---|
| `mesh_convergence` (#48) | one parameter, a ladder of values | the table, plus where each metric stopped moving |
| `sweep` (#21) | one or more parameters, a grid or a list of points | the table, plus a response curve per metric |

A **sweep** carries either `axes` — a full factorial, last axis varying fastest, first axis the
abscissa of the curves — or explicit `points`, which is how a Latin hypercube or any other
design of experiments arrives. Generating those here was considered and refused: **a randomized
design generated server-side breaks the property that defines a study**, that its job list is a
pure function of its object revision, unless the seed is frozen in the body — at which point the
caller is specifying the design anyway. So the caller generates and the revision freezes.

A sweep is bounded at 64 points. The bound is on the *answer*, not on the queue — every point
goes through `job.submit` and is governed by the cell budget and the quota like any other job —
and it exists because a grid **multiplies**: four axes of four values is 256 solves from a body
that fits on one line, and the typo that adds a fifth axis is invisible.

The curves come back as `Series1DData`, the same model a `series1d` result carries, so anything
that draws a curve draws these. Each trace brings its own abscissa rather than sharing the
series-level one, because a point with no answer has no legal encoding against a shared axis —
it would have to become a zero, or shift every later value onto the wrong abscissa. A partial
sweep says exactly what it knows.

**What neither kind does is choose the next point.** That is an optimizer, it is
[#22](https://github.com/mandaloriat/fenix-spoon/issues/22), and the boundary is §15's: given
`study:s-1@2` you can say which solves it implies without running any of them.

## 8. Workspace

!!! success "Implemented (#44) — core only, no HTTP binding"

    Six object types under ids like `geometry:g-1`, versioned rather than mutated, patched
    with RFC 6902, and a `job.submit` that takes a design reference and freezes the revisions
    it resolved. The four questions this section left open are answered in
    [§15](#15-open-questions) with the evidence that decided each one.

    Reachable through :class:`FenixSpoonCore` and not over HTTP: the workspace's first
    transport is the JSON-RPC adapter (#45), and binding it to HTTP now would mean designing
    an object API that JSON-RPC might want a different shape for. `/api/v1` is unchanged.

A workspace is a directory the caller names (default: `./.fenix-spoon`, or the existing
`FENIXSPOON_DATA_DIR` when one is configured). It holds the object store, the result cache and the
artifact files, and it must be:

- **Inspectable** — a human can look at it, diff it, and put it in version control if they choose.
- **Reopenable** — closing the process and reopening the workspace restores every object and
  result; ids stay valid. Jobs and results already behave this way; the new object types must join
  them rather than living somewhere with different rules.
- **Local and single-user** — no locking protocol beyond SQLite's, no new durability semantics.
  Multi-user persistence is the `JobStore` interface and is already solved.

Whether the new objects live in the existing SQLite store, in a separate database, or in a
manifest-plus-files layout is an open question below. What is fixed is the contract: stable ids, a
listable index, patchable objects, results that name their inputs, and one retention story rather
than two.

## 9. Job lifecycle

Unchanged in substance from the HTTP path — the same execution backend, the same statuses
(`queued` → `running` → `done` | `failed` | `cancelled`), the same cooperative cancellation,
wall-clock timeout, cell budget and sequence-numbered event replay. What changes for a local
caller:

- **Submission takes references.** `job.submit` with a `design` id resolves geometry, materials,
  load cases and params from the workspace; an inline geometry — with an inline `conditions` map
  beside it, since #85 — is still accepted for one-shot use.
- **Progress is opt-in.** An agent that does not want twenty progress ticks in its context submits,
  then polls `job.get`, or subscribes with a coarser cadence. The stream exists; it is not pushed
  at a caller that did not ask.
- **Completion produces a `result` object**, not a payload. `job.get` on a finished job returns
  status, the result id, its metrics and diagnostics — not fields.
- **Equivalent jobs may not run at all.** *(Implemented, #47.)* With a content-addressed identity
  (geometry + solver + its declared version + params + load case + environment), a resubmission of something
  already computed returns the job that already ran and says so in the provenance
  (`cached: true`). This is the single biggest lever on both compute cost and context cost in an
  iterative loop. Two departures from the sketch above, both deliberate: **caching is opt-in per
  adapter**, because a solver that cannot promise reproducibility must not be cached at all; and
  a hit returns *the earlier job* rather than a new one pointing at it, which is what keeps
  retention trivial — the entry is the job.
- **Ownership and limits still apply.** A local caller resolves to a `Principal` like any other;
  its jobs are owned, counted against quotas if any are configured, and swept by the same retention
  policy. The local transport is a different door, not a bypass.

## 10. Result queries

!!! success "Implemented (#46), as protocol 1.3"

    Levels, metric values, diagnostics and all nine query operations, with two departures
    worth recording. **The HTTP binding is a separate route, not a query parameter on
    `/result`** — two payload shapes on one path, chosen by a parameter, is not something a
    typed client can describe, so `/summary` is the compact form and `/result` keeps its
    arrays. And **`over_region` needs the geometry**, which a result does not carry: it
    resolves through the job's workspace provenance (#44), so a job submitted with an inline
    geometry is told that rather than handed an empty region. Persisting the geometry with
    every job would remove the limitation and belongs with provenance in #47.

Five response levels, requested independently:

| Level | Content | Size |
|---|---|---|
| `status` | terminal status, timing, error | tens of bytes |
| `metrics` | declared scalar engineering quantities | hundreds of bytes |
| `diagnostics` | cells, dofs, iterations, seconds, convergence, warnings | hundreds of bytes |
| `fields` | full nodal/cell arrays | megabytes — never a default |
| `artifacts` | references to files (VTK/VTU/mesh/log) | reference only |

**Diagnostics already half exist.** Every result carries `stats` — `cells`, `dofs`, `iterations`,
`seconds`, whichever the adapter knows. That is the cost of the solve, and it is the right seed for
this level; what is missing is convergence state and warnings, which today live only in progress
events or nowhere. Formalize `stats`, do not duplicate it.

**Metrics are the part that does not exist yet**, and they are what the caller actually reasons
about: mass, maximum displacement, maximum stress, safety factor, strain energy, force, inductance,
peak temperature — each declared by the capability with a name, a unit and a description, so the
meaning does not have to be inferred. The three mock solvers plus their FEniCSx counterparts
already compute fields from which a first set falls out directly (peak speed, peak flux density,
peak temperature).

**Selective field queries** cover the cases where a scalar is needed but was not pre-declared.
`result.query` takes a field name and an operation:

| Operation | Returns |
|---|---|
| `max` / `min` | extremum, with its location |
| `mean` | area/volume-weighted average |
| `integral` | integral over the domain or a named region |
| `at_point` | value at a coordinate (interpolated) |
| `over_region` | statistics restricted to a named region |
| `section` | values along a line/plane, at a requested sample count |
| `sample` | decimated sampling of the field at a requested budget |
| `hotspots` | the N most extreme locations, clustered |

Every one returns a bounded payload; `section` and `sample` take an explicit budget and the server
caps it. Full arrays are reached exactly one way: fetch the artifact.

## 11. Transports

!!! success "JSON-RPC over stdio implemented (#45)"

    Twenty-four methods over the same core, no port and no FastAPI. The framing question this
    section left open is answered — **newline-delimited JSON written, either framing
    accepted** — with the evidence in [§15](#15-open-questions). Two things went differently
    from the sketch below: **batches are refused** rather than supported, and **progress is
    both pollable and streamable** rather than one or the other. The reference page is
    [JSON-RPC over stdio](08-json-rpc.md).

| Transport | Status | Role |
|---|---|---|
| HTTP + WebSocket (`/api/v1`) | shipped | browsers, remote clients, the multi-user deployment |
| JSON-RPC 2.0 over stdio | **shipped (#45)** | **the base local transport** |
| CLI (`fenix-spoon …`, JSON output) | **shipped (#50)** | shells, CI, reproducible scripting, debugging |
| Python API | **shipped (#50)** | notebooks and in-process embedding |
| MCP | **shipped (#49)**, thin | MCP hosts; an adapter over the same operations |

**JSON-RPC 2.0 over stdio.** The agent spawns the process (`fenix-spoon rpc --stdio` is the
expected entrypoint), writes requests to its stdin and reads responses from its stdout. No port is
opened, no origin policy applies, and the process dies with its parent. Requirements: documented
framing, typed compact errors (a code, a message, and structured `data` — not a stack trace and not
an HTML error page), asynchronous jobs for anything long-running so the channel is never blocked by
a solve, and identical behavior against mock and FEniCSx solvers.

**MCP is layered on this, not underneath it.** The MCP adapter maps a small stable tool set —
inspect environment, list capabilities, describe capability, create or patch object, submit job,
inspect job, query result, run study — onto the same core calls, and exposes artifacts as MCP
resources. It contains no application logic, no per-solver tools, and no physics vocabulary of its
own. If MCP is replaced by something else in two years, one adapter is deleted.

**Conformance across transports** is a deliverable, not a hope: shared fixtures assert that an
equivalent request over HTTP and JSON-RPC produces the same semantic result, that a validation
failure is the same failure on both, that mock and FEniCSx honor the same envelopes, that schemas
do not diverge, and that a compact response never carries a full numeric array. This extends the
existing `protocol/fixtures/` corpus that pytest and vitest already share, and the generated
[protocol reference](reference-protocol.md) whose staleness CI already fails on.

## 12. Security

The local interface's threat model is *not* the web API's. The caller is a process on the same
machine, running as the same user, which in most cases could already run a shell. Security here is
therefore mostly about **not creating new capability** and about **keeping the surface safe to
expose later**:

- **No arbitrary execution, ever, on this surface.** The reasons are in the
  [security posture](02-architecture.md#security-posture-why-solvers-are-declarative): an interface
  made of `run_python` has no schema, no validation, no cache identity, no provenance, and cannot
  be exposed over a network without becoming remote code execution. The local transport is not a
  loophole in the declarative rule; it is the same rule with a different pipe.
- **No listening socket by default.** stdio means there is nothing to reach from outside the
  machine, and nothing to misconfigure. Exposing JSON-RPC over a network socket is out of scope
  for M2.5; anything networked goes through the existing auth path, not around it.
- **Identity is not bypassed.** Jobs are owned by a `Principal` and quotas key off it. A local
  caller authenticates by being able to start the process, and resolves to a principal like any
  other; it does not get an unowned job or an unmetered one.
- **Path confinement.** Artifacts must already be bare filenames under the job directory
  (`SolverContext.artifact` enforces it); workspace objects and artifact resolution must be
  confined to the workspace root the same way. A capability that reads a file the caller names
  would need its own review — none is planned.
- **Environment is reported, never mutated.** `environment.inspect` tells the caller what is
  installed. Nothing in this vocabulary installs, upgrades, or launches anything.
- **Resource limits still apply.** The cell budget, the wall-clock timeout and the retention sweep
  are properties of the job path, so a local caller inherits them: an agent that asks for an absurd
  resolution gets a validation error, not a wedged machine.

## 13. Token efficiency

For an agent, payload size is not a performance concern but a correctness one: a result that does
not fit is a result that cannot be reasoned about. Design rules, enforced by tests rather than by
good intentions:

- **A discovery call answers in kilobytes, not tens of kilobytes.** `capability.list` should stay
  under a couple of kilobytes for a realistic installation; `capability.describe` returns only the
  requested sections.
- **Schemas travel by reference by default.** A full `params_schema` is fetched when a caller is
  actually generating a request, not as a side effect of asking what exists.
- **A finished job's default answer is status + metrics + diagnostics + artifact references.**
  Fields are opt-in and are the one thing that can be large.
- **Bounded queries.** `section`, `sample` and `hotspots` take explicit budgets and are capped
  server-side, so no query degenerates into "the whole field, spelled differently".
- **Iterations exchange ids and patches.** Re-solving with one changed parameter costs a patch and
  a reference, not a re-transmitted geometry — and if the result is cached, it costs a cache hit.
- **A conformance test asserts the compact envelopes contain no long numeric arrays**, so an
  innocent-looking refactor cannot silently start returning nodal data.

## 14. Vertical slice

The milestone's exit criterion, spelled out. From a local agent process, with **no HTTP server
started**:

1. `environment.inspect` — see whether this machine has dolfinx or only the mock solvers.
2. `capability.list` → `capability.describe` for the potential-flow capability, sections
   `geometries`, `params`, `metrics`.
3. `workspace.open` on a directory.
4. `object.create` a `geometry` (an airfoil `domain2d`) and a `design` referencing it with solver
   params.
5. `job.submit` for that design → `job.get` / progress → terminal status.
6. Read metrics and diagnostics from the result; run one `result.query` (say, `max` of `speed` with
   its location).
7. `artifact.get` the VTK file by reference — a path, not bytes in the context.
8. `object.patch` one geometry parameter → `job.submit` again → the second result comes back with
   provenance showing which objects were reused and whether the solve was recomputed or served from
   cache.
9. Compare the two results' metrics.

The solenoid (`regions2d`, magnetostatics) and the heat sink are equally valid subjects and
exercise materials and regions more heavily; whichever is chosen, the slice must run in both mock
and FEniCSx environments.

**The milestone is not complete because JSON-RPC answers.** It is complete when that loop —
discover, build, solve, read compactly, fetch by reference, patch, re-solve reusing state — runs
end to end, and the same operations are reachable from CLI, Python and MCP.

## 15. Open questions

Deliberately unresolved. Each needs a decision recorded with its reasoning, and none should be
settled by picking whatever is fashionable.

**Six are now settled** — four by #44, the framing question by #45 and the study/optimizer boundary by #48 — and are struck through below with what decided them. The
reasoning is preserved rather than replaced: a decision whose grounds are deleted is
indistinguishable from a preference, and the grounds are what a future reader needs in order to
know whether the decision still holds.

| Question | Options | How to decide |
|---|---|---|
| ~~**JSON-RPC framing**~~ **→ NDJSON written, both accepted** | newline-delimited JSON vs `Content-Length` headers (LSP/MCP style) | decided on the two tests the question named. **Can a payload contain a raw newline?** No, and not by luck: `json.dumps` escapes U+000A inside strings, and ASCII escaping also escapes U+2028 and U+2029 — the characters Python's `readline` does not treat as terminators but several other languages' line splitters do. So one frame is one line under *any* reader's definition of a line, which is what a newline delimiter needs and what `test_no_encodable_value_can_break_the_frame` asserts. **What does the MCP adapter need?** MCP's stdio transport is itself newline-delimited, so header framing here would make #49 a re-framing layer rather than the thin one it is supposed to be. Supporting both on input was indeed cheap — about fifteen lines, detected per message — so a caller built against an LSP-style client does not have to care. The cost paid is ASCII escaping inflating non-ASCII text; this vocabulary is numbers, identifiers and English, and compactness that could corrupt a frame in someone else's line reader is not a trade worth making. |
| ~~**Where workspace objects live**~~ **→ JSON files under the data directory** | extend the SQLite `JobStore` schema vs a second store vs manifest files on disk | the store already exists, has WAL, retention and an interface with two implementations — extending it keeps one retention story and one thing to back up. Against that: designs and studies are a different lifetime from jobs, and files are diffable and git-friendly in a way a database is not. Decide by whether a workspace is meant to be committed to a repository. **It is** — §2's own use cases include "a repository of designs is re-solved on every solver change", and §8 asks for something a human can diff. `git diff` on a SQLite file says `Binary files differ`; on `objects/geometry/g-1/00002.json` it shows the two coordinates that moved. One directory to back up survives, because the files live inside `FENIXSPOON_DATA_DIR` beside `jobs.db`. |
| ~~**Patch format**~~ **→ JSON Patch (RFC 6902)** | JSON Patch vs JSON Merge Patch vs a domain-specific patch | decided on the named test case, moving one control point: `[{"op":"replace","path":"/obstacle/points/1","value":[0.35,0.14]}]`. Merge patch has no operation for one array element — it replaces arrays whole, so the same edit carries every point, which is the cost this milestone exists to remove. A domain patch would be friendlier to generate but needs a new verb for every edit anyone wants, where RFC 6902 already has them and has implementations in every client language. |
| ~~**Identifiers**~~ **→ readable, with revisions** | readable short ids (`g-42`) vs UUIDs vs content hashes | `geometry:g-1`, and `geometry:g-1@3` to pin a revision. Readable won on the cost that matters here — an agent carries these strings through every turn — and the type is in the id so a mismatched prefix is caught at parse rather than by a lookup that finds nothing. Ids are allocated by `mkdir` of the next free number, which is atomic, so concurrent creates cannot collide. UUIDs remain the answer if workspaces are ever merged; content hashes are #47's identity and are a different question from *naming*. |
| ~~**Object lifetime and GC**~~ **→ objects are never swept** | inherit `FENIXSPOON_JOB_TTL` vs a separate policy vs explicit deletion | no object TTL, and the job TTL does not reach them. The asymmetry is the answer rather than a longer number: a job is a computation and losing it costs a re-run, an object is something a person authored and losing it is data loss. That inverts the dangling-reference worry — inputs outlive results, not the other way round — so "what does a result mean when its objects are gone" does not arise, and what survives is enough to recompute. Explicit deletion is still unimplemented; nothing yet needs it. |
| **Artifact binary format** | keep VTK legacy vs VTU/VTKHDF vs a compact internal format for queries | `result.query` implies something the server can index efficiently; ParaView compatibility implies VTK-family output. These may be two different files with different jobs. |
| **MCP resource exposure** *(both shipped, question still open)* | artifacts as MCP resources vs tool-returned paths vs both | #49 ships **both** and deliberately does not close this. The deciding evidence this row asks for — real host behaviour — is precisely what a test suite cannot produce, so closing it on the specification alone would be answering a different question. What is settled is one sub-case, on grounds that do not need a host: **a large artifact is described, not base64-encoded**. Base64 makes a file a third larger and delivers it into a context window that cannot use it, so a resource read returns path, size and content type; small text artifacts (<64 kB) arrive inline. What remains open is whether hosts in practice reach for the resource or the path, and that wants a host to watch. |
| ~~**Study vs optimization boundary**~~ **→ a study's job list is a pure function of its object revision** | how much belongs in the M2.5 study service | settled by the first study kind, as this row asked. The test is not "does it enumerate" — that is a description, and descriptions blur — but a property you can check: given `study:s-1@2` you can say which solves it implies **without running any of them**. An optimizer cannot promise that, because its second point depends on the first one's answer. The line lands where this row wanted it: everything an external driver would otherwise reimplement (fan-out, cache reuse, per-job budget and quota, collecting compact results) is inside, and choosing the next point is outside. It also does real work — it is why a study stays reproducible under §8's rules while overriding a design's parameters, which `job.submit` may not do: the override is `values[i]` applied to `parameter`, both frozen in the study revision, so a rung's parameters are a pure function of *(study revision, rung index)*. |

Two further questions were worth naming even though they were not blocking: how a capability
declares its metrics without every adapter reimplementing the plumbing, and whether
`boundary_condition` and `load_case` can be schematized generically or must stay solver-specific
for now. Both said they would be answered by the next physics capability that had to be modelled,
and both were: #46 for the metrics, and the elasticity pair (#81) for the load case, whose
conditions were the first that could not be inferred from the shape.

The answer to the second is a split rather than a yes or a no. The **structure** generalises — a
map of boundary name to scalars, resolved against boundaries the geometry names — and the
**vocabulary** does not, so it stays per capability and each one declares its own keys (#85). A
typed enum of condition kinds would have been the generic schema this row was asking after, and
it would have made every new physics a protocol change.
