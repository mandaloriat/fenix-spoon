# Local agent interface — design draft

> **Status: preliminary design specification, nothing here is implemented.** This document
> describes the interface [M2.5](03-roadmap.md#m25-local-automation-and-agent-interface) is meant
> to build, so that the milestone's issues can be written against something concrete. Names,
> payload shapes and operation sets *will* change during implementation; treat every example as
> illustrative, not as a stable API. The shipped contract today is the HTTP/WebSocket
> [wire protocol](04-wire-protocol.md).

## 1. Motivation

Fenix Spoon exists because putting FEniCSx behind a web page means hand-rolling the same stack
every time. That framing is still true and still the entry point — but it describes one *client*,
not the product. What the project actually builds is a **queryable runtime and protocol for
constructing, running and automating FEniCSx-based engineering simulation workflows**. The browser
is one consumer of it.

The consumer this document is about is a program on the same machine: a script, a CI step, a
notebook, or a software agent. Such a caller already has FEniCSx available (in a conda
environment or a container) and does not want a web application; it wants a structured way to ask
*what can this environment simulate*, *run this*, *how did it come out*, and *give me the file*.

Today that caller has three bad options:

1. **Drive the HTTP API.** Workable, but it forces a server process and a port for something that
   is a local, single-user, one-shot interaction, and the answers are shaped for a viewer widget:
   a `grid2d` result is tens of thousands of floats, which is exactly the wrong payload for a
   caller with a bounded context window.
2. **Import `fenixspoon` and call internals.** There is no supported surface: the useful logic —
   solver lookup, geometry-kind checking, params validation, artifact URLs — lives inside FastAPI
   route bodies (`server/fenixspoon/api.py`) and raises `HTTPException`.
3. **Write dolfinx code and run it.** This is what people do, and it is precisely the failure mode
   the project's [security posture](02-architecture.md#security-posture-why-solvers-are-declarative)
   rejects for clients: unvalidated arbitrary execution, no discoverable schema, no provenance, no
   cache identity, nothing reusable between runs.

The gap is not "AI support". It is that the application logic is not reachable except through one
transport, and that its answers are sized for pixels rather than for decisions.

## 2. Use cases

- **Agent-driven design iteration.** An agent is asked to thicken an airfoil until a lift proxy
  stops improving. It discovers the potential-flow capability, creates a design, solves, reads
  three scalars, patches one geometry parameter, solves again, and compares — with only the
  scalars and object ids ever entering its context.
- **Mesh convergence as a checked routine.** A script runs the same design at increasing mesh
  resolution and reports where the metric of interest stabilizes, reusing cached results for the
  resolutions already computed.
- **Batch regression in CI.** A repository of designs is re-solved on every solver change; the
  job compares metrics against stored baselines and fails on drift. No server, no browser.
- **Interactive shell use.** An engineer runs `fenix-spoon capability describe dolfinx.potential_flow2d
  --section params` and `fenix-spoon result query result:r-105 --op max --field speed` while
  debugging, hitting the same code path an agent would.
- **Notebook / Python scripting.** A user imports the Python API in a Jupyter kernel that already
  has dolfinx, and gets the same objects, validation and caching as every other transport.
- **MCP host integration.** A desktop assistant with an MCP client connects to a local Fenix Spoon
  server and gets a handful of stable tools rather than a Python sandbox.

## 3. Principles

1. **The protocol is the product.** Unchanged from the [architecture](02-architecture.md): the
   value is the contract, not any one implementation of a client.
2. **Solvers stay named adapters with typed parameters.** The local interface adds no way to
   describe *how* to solve, only *what*, from a server-defined menu.
3. **The core is transport-neutral.** HTTP/WebSocket, JSON-RPC over stdio, CLI, Python and MCP are
   adapters over one application core with one set of models and one set of errors.
4. **It must work entirely locally.** No network port required, no external services, no
   multi-user infrastructure. The base local transport is a child process speaking over pipes.
5. **It must work without FEniCSx.** Mock solvers keep every operation exercisable in a plain
   virtualenv, exactly as they do for the browser path today.
6. **It must use the FEniCSx that is there.** When dolfinx imports, the real adapters register
   themselves and the same operations run the real solve — the local interface introduces no
   separate execution path, no installation step, no container orchestration.
7. **Answers are compact by default.** Scalars and diagnostics first; fields by reference or by
   query. A caller asks for volume, it never arrives unrequested.
8. **Discovery is progressive.** Ask for the section you need. Full schemas are fetched by
   reference or on explicit request.
9. **State lives in a workspace, not in messages.** Objects have stable ids; iterations send
   patches and references, not whole geometries.
10. **No arbitrary execution.** Engineering operations and domain objects only.

## 4. Non-goals

Explicitly out of scope for this interface — not "later", but *not this*:

- **Arbitrary Python, UFL or shell execution.** No `run_python(code)`, `run_ufl(source)`,
  `execute_shell(command)`. Sandboxed opt-in arbitrary UFL remains a separate M5 experiment
  (#24) with its own threat model, and would not be reachable through this vocabulary.
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
| `material` | named scalar properties (`mu_r`, `E`, `rho`, …) | the open-dict convention of `regions2d.material`, promoted to a reusable object |
| `boundary_condition` | a named condition bound to a boundary tag | today implicit in each solver; needs a schema before it can be an object |
| `load_case` | a set of sources/loads applied to a design | groups current densities, inlet velocities, thermal loads |
| `design` | a geometry reference + material/BC/load-case references + solver params | the unit an iteration patches |
| `study` | a study kind + its base design + a variation spec | orchestrates several jobs |
| `job` | one submitted solve | already exists (`Job` in `jobs.py`), gains a workspace identity |
| `result` | metrics, diagnostics, provenance, references to fields and artifacts | the compact object an agent reads |
| `artifact` | a file produced by a solve (VTK, VTU, mesh, log) | already exists per job; gains an id and a lifetime |

Identifiers look like `geometry:g-42`, `design:d-18`, `study:s-9`, `result:r-105`, `job:j-8f3adc…`
(job ids keep their current format). Every operation that would otherwise take a payload accepts a
reference instead:

```json
{"method": "job.submit", "params": {"design": "design:d-18", "solver": "mock.laplace2d"}}
```

Objects are **versioned, not mutated in place**: `object.patch` produces a new revision and returns
its id or revision tag, so a result's provenance can name exactly what it was computed from. The
patch format is expected to be [JSON Patch (RFC 6902)](https://datatracker.ietf.org/doc/html/rfc6902)
or an equivalent standardized mechanism — see the open questions.

## 6. Capability discovery

A *capability* is what a solver adapter offers, described in sections so a caller can ask for the
part it needs. Today `GET /api/v1/solvers` returns everything about every solver, including full
JSON Schemas — fine for a form generator, wasteful for a caller that wants to know whether
magnetostatics is available at all.

Three operations:

- **`environment.inspect`** — what this installation *is*: Fenix Spoon version, protocol version,
  whether dolfinx and gmsh imported and at what versions, MPI availability, workspace location,
  configured limits (job timeout, artifact caps), cache state. A few hundred bytes, no schemas.
- **`capability.list`** — the installed capabilities as identity plus one line each: name, title,
  physics tag, accepted geometry kinds, availability (`mock` / `fenicsx`). No schemas.
- **`capability.describe`** — one capability, with a `sections` argument selecting from:
  `geometries`, `params`, `metrics`, `artifacts`, `features` (sweep / gradient / MPI support),
  `requirements` (what the environment must provide), `examples`. Unspecified sections are omitted;
  large schemas are returned as a reference (`schema:params/dolfinx.potential_flow2d`) that a
  further call resolves, or inline only when explicitly requested.

```json
{"method": "capability.describe",
 "params": {"capability": "dolfinx.potential_flow2d", "sections": ["metrics", "features"]}}
```

The `metrics` section is new and load-bearing: it is how a caller learns that this capability can
report `speed_max`, `circulation`, `lift_proxy` — with unit and meaning — *before* running
anything. Solver adapters declare their metrics the way they declare `Params` today.

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
| `study.run` | run a study over a design |
| `study.get` | study status and its per-job result references |

Deliberately absent: anything that takes code, a command line, a package name or an image
reference. Deliberately *not* per-physics: there is no `solve_magnetostatics` — the physics is a
capability name, a parameter and a set of declared metrics, which is what makes a new solver
adapter automatically available to every caller.

## 8. Workspace

A workspace is a directory the caller names (default: `./.fenix-spoon` or an XDG state path). It
holds the object store, the result cache and the artifact files, and it must be:

- **Inspectable** — a human can look at it, diff it, and put it in version control if they choose.
- **Reopenable** — closing the process and reopening the workspace restores every object and
  result; ids stay valid.
- **Local and single-user** — no locking protocol beyond what one process plus a stray second one
  needs, no server-side persistence semantics. Multi-user persistence is M3 (#13).

Whether that store is SQLite or a manifest-plus-files layout is an open question below. What is
fixed is the contract: stable ids, a listable index, patchable objects, and results that name their
inputs.

## 9. Job lifecycle

Unchanged in substance from the HTTP path — the same job service, the same statuses
(`queued` → `running` → `done` | `failed` | `cancelled`), the same cooperative cancellation and
wall-clock timeout, the same event replay so a late subscriber sees the full history. What changes
for a local caller:

- **Submission takes references.** `job.submit` with a `design` id resolves geometry, materials and
  params from the workspace; an inline geometry is still accepted for one-shot use.
- **Progress is opt-in.** An agent that does not want twenty progress ticks in its context
  submits, then polls `job.get`, or subscribes with a coarser cadence. The stream exists; it is
  not pushed at a caller that did not ask.
- **Completion produces a `result` object**, not a payload. `job.get` on a finished job returns
  status, the result id, its metrics and diagnostics — not fields.
- **Equivalent jobs may not run at all.** With a content-addressed identity (geometry + solver +
  params + environment), a resubmission of something already computed returns the cached result
  and says so in the provenance (`cached: true`). This is the single biggest lever on both compute
  cost and context cost in an iterative loop.

## 10. Result queries

Five response levels, requested independently:

| Level | Content | Size |
|---|---|---|
| `status` | terminal status, timing, error | tens of bytes |
| `metrics` | declared scalar engineering quantities | hundreds of bytes |
| `diagnostics` | mesh cells/dofs, iterations, residual, convergence flag, warnings | hundreds of bytes |
| `fields` | full nodal/cell arrays | megabytes — never a default |
| `artifacts` | references to files (VTK/VTU/mesh/log) | reference only |

**Metrics** are what the caller actually reasons about: mass, maximum displacement, maximum
stress, safety factor, strain energy, force, inductance, peak temperature — each declared by the
capability with a name, a unit and a description, so the meaning does not have to be inferred.

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

Every one of these returns a bounded payload; `section` and `sample` take an explicit budget and
the server caps it. Full arrays are reached exactly one way: fetch the artifact.

## 11. Transports

| Transport | Status | Role |
|---|---|---|
| HTTP + WebSocket (`/api/v1`) | shipped | browsers, remote clients, the M3 production path |
| JSON-RPC 2.0 over stdio | planned, M2.5 | **the base local transport** |
| CLI (`fenix-spoon …`, JSON output) | planned, M2.5 | shells, CI, reproducible scripting, debugging |
| Python API | planned, M2.5 | notebooks and in-process embedding |
| MCP | planned, M2.5, thin | MCP hosts; an adapter over the same operations |

**JSON-RPC 2.0 over stdio.** The agent spawns the process (`fenix-spoon rpc --stdio` is the
expected entrypoint), writes requests to its stdin and reads responses from its stdout. No port is
opened, no origin policy applies, and the process dies with its parent. Requirements: documented
framing, typed compact errors (a code, a message, and structured `data` — not a stack trace and
not an HTML error page), asynchronous jobs for anything long-running so the channel is never
blocked by a solve, and identical behavior against mock and FEniCSx solvers. Framing choice is
open (below).

**MCP is layered on this, not underneath it.** The MCP adapter maps a small stable tool set —
inspect environment, list capabilities, describe capability, create or patch object, submit job,
inspect job, query result, run study — onto the same core calls, and exposes artifacts as MCP
resources. It contains no application logic, no per-solver tools, and no physics vocabulary of its
own. If MCP is replaced by something else in two years, one adapter is deleted.

**Conformance across transports** is a deliverable, not a hope: shared fixtures assert that an
equivalent request over HTTP and JSON-RPC produces the same semantic result, that a validation
failure is the same failure on both, that mock and FEniCSx honor the same envelopes, that schemas
do not diverge, and that a compact response never carries a full numeric array. This extends the
existing `protocol/fixtures/` corpus, which pytest and vitest already share.

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
  for M2.5.
- **Path confinement.** Artifacts already must be bare filenames under the job directory
  (`SolverContext.artifact` enforces it); workspace objects and artifact resolution must be
  confined to the workspace root the same way. A capability that reads a file the caller names
  would need its own review — none is planned.
- **Environment is reported, never mutated.** `environment.inspect` tells the caller what is
  installed. Nothing in this vocabulary installs, upgrades, or launches anything.
- **Resource limits still apply.** The wall-clock timeout and the future mesh-size caps (#6) are
  properties of the job service, so a local caller inherits them; an agent that asks for an
  absurd resolution gets a validation error, not a wedged machine.
- **Authentication is out of scope here** and stays in M3 (#14). A local single-user process
  authenticates by being able to start the process. Any networked exposure of these operations
  must go through the M3 auth path, not around it.

## 13. Token efficiency

For an agent, payload size is not a performance concern but a correctness one: a result that does
not fit is a result that cannot be reasoned about. Design rules, to be enforced by tests rather
than by good intentions:

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
6. Read metrics and diagnostics from the result; run one `result.query` (say, `max` of `speed`
   with its location).
7. `artifact.get` the VTK file by reference — a path, not bytes in the context.
8. `object.patch` one geometry parameter → `job.submit` again → the second result comes back with
   provenance showing which objects were reused and whether the solve was recomputed or served
   from cache.
9. Compare the two results' metrics.

The solenoid (`regions2d`, `mock.magnetostatics2d` / `dolfinx.magnetostatics2d`) is an equally
valid subject and exercises materials and regions more heavily; whichever is chosen, the slice
must run in both mock and FEniCSx environments.

**The milestone is not complete because JSON-RPC answers.** It is complete when that loop —
discover, build, solve, read compactly, fetch by reference, patch, re-solve reusing state — runs
end to end, and the same operations are reachable from CLI, Python and MCP.

## 15. Open questions

Deliberately unresolved. Each needs a decision recorded with its reasoning, and none should be
settled by picking whatever is fashionable.

| Question | Options | How to decide |
|---|---|---|
| **JSON-RPC framing** | newline-delimited JSON vs `Content-Length` headers (LSP/MCP style) | NDJSON is trivial to implement and debug by hand; `Content-Length` is what MCP hosts already speak and is robust to embedded newlines. Decide by what the MCP adapter needs and by whether any payload can contain a raw newline. Supporting both is cheap and may be the answer. |
| **Workspace store** | SQLite vs manifest files on disk | SQLite gives queries, transactions and concurrent-read safety; files give diffability, git-friendliness and zero opacity. Decide by whether the first study kind needs real queries, and by how much a human is expected to inspect the workspace by hand. |
| **Patch format** | JSON Patch (RFC 6902) vs JSON Merge Patch vs a domain-specific patch | JSON Patch is standard, precise about arrays (point lists!), and already has implementations everywhere; a domain patch (`set_param`, `move_point`) is friendlier to generate and easier to validate. Decide by looking at real geometry edits — moving one control point is the test case. |
| **Identifiers** | readable short ids (`g-42`) vs UUIDs vs content hashes | readable ids are far cheaper in an agent's context and in a CLI; UUIDs avoid collisions across merged workspaces; content hashes unify identity with caching but change on every edit. Note these can coexist: a readable id naming a content hash. |
| **Object lifetime and GC** | keep everything vs TTL vs explicit deletion vs cache eviction by size | artifacts are the space cost, objects are cheap. Decide by measuring a realistic sweep, and beware breaking provenance: a result must not outlive the objects it names without at least recording them. |
| **Artifact binary format** | keep VTK legacy vs VTU/VTKHDF vs a compact internal format for queries | `result.query` implies something the server can index efficiently; ParaView compatibility implies VTK-family output. These may be two different files with different jobs. Ties into M1 #4. |
| **MCP resource exposure** | artifacts as MCP resources vs tool-returned paths vs both | resources are the idiomatic MCP answer and let a host fetch lazily; paths are simpler and work for hosts with filesystem access. Decide against real MCP host behavior, not against the specification alone. |
| **Study vs optimization boundary** | how much belongs in the M2.5 study service | a study that enumerates a variation space is clearly in; a study that *chooses* the next point is an optimizer and is M5 (#22). The line should be drawn where an external driver would otherwise have to reimplement job orchestration. |

Two further questions are worth naming even though they are not blocking: how a capability declares
its metrics without every adapter reimplementing the plumbing, and whether `boundary_condition` and
`load_case` can be schematized generically or must stay solver-specific for now. Both are answered
by the second physics capability that has to be modelled, not by argument.
