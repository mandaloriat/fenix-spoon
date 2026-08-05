# Architecture

Fenix Spoon is three loosely-coupled layers joined by one contract (the
[wire protocol](04-wire-protocol.md)).

## Today

```mermaid
flowchart TB
    subgraph client["Client layer (npm packages, M2)"]
        direction LR
        GEO["@fenix-spoon/geometry-2d<br/>parametric profile editor"]
        SDK["@fenix-spoon/client<br/>JS SDK: jobs, WS events"]
        VIEW["@fenix-spoon/viewer<br/>canvas field viewer"]
    end
    subgraph proto["Wire protocol (JSON over REST + WebSocket)"]
        P1["geometry schema · job lifecycle · progress events · field results"]
    end
    subgraph server["Server layer (Python package `fenixspoon`)"]
        API["FastAPI app<br/>REST + WS + OpenAPI"]
        JOBS["Job manager<br/>(thread pool, or arq workers over Redis)"]
        REG["Solver registry"]
        subgraph adapters["Solver adapters"]
            MOCK["mock.laplace2d<br/>(NumPy, always available)"]
            DFX["dolfinx.*<br/>(FEniCSx + Gmsh, in Docker)"]
        end
    end
    client --> proto --> server
    API --> JOBS --> REG --> adapters
```

## Where this is going (M2.5)

The browser is an important client, not the only one. A machine that has FEniCSx and local agents
should be able to use Fenix Spoon as a structured interface to its own compute environment,
through typed calls and compact answers. That turns the project from "a toolkit for putting
FEniCSx behind a web app" into **a queryable runtime and protocol for building, running and
automating FEniCSx-based engineering simulation workflows**.

Structurally it is one change: HTTP stops being the domain and becomes one adapter among several,
over a core that knows nothing about transports.

```mermaid
flowchart TB
    subgraph adapters2["Transport adapters"]
        direction LR
        HTTP["HTTP + WS<br/>(browser, SDK)"]
        RPC["JSON-RPC<br/>over stdio"]
        CLI["CLI / Python"]
        MCP["MCP hosts"]
    end
    CORE["Transport-neutral core<br/>capabilities · workspace · jobs · results · studies"]
    ENG["Solver registry → FEniCSx / mock solvers"]
    adapters2 --> CORE --> ENG
```

**The core now exists** (#42). `fenixspoon/core/` holds the capability catalog, the job
operations, identity and quotas; `api.py` is an adapter that reads a request, calls one core
method, and shapes the reply. The transports beside it — JSON-RPC, CLI, MCP — are not built yet,
but the layer they attach to is. The properties it has, and must keep:

- **Transport-neutral core** *(done)*. `FenixSpoonCore` raises domain errors, never
  `HTTPException`, and never builds a URL — `result()` returns artifact *paths*, and the HTTP
  adapter turns those into routes it serves. Guarded by a test that imports the core in a
  subprocess and fails if FastAPI appears in `sys.modules`: the first version of this leaked
  through `core.service` → `auth` → `fastapi`, which no other test could have caught, since they
  all run with FastAPI already loaded. The fix split the identity *rule* (`core/identity.py`)
  from its HTTP *binding* (`auth.py`).
- **HTTP is an adapter, not the domain** *(done)*. Fourteen `raise HTTPException` sites became
  one table in `http_errors.py`, so the status codes the wire protocol promises are visible in a
  single place. Routes are now two or three lines each, and the `/api/v1` contract is unchanged —
  the existing API suite passed the refactor without edits, which was the acceptance criterion.
- **A local interface.** JSON-RPC 2.0 over stdio is the base local transport: the agent starts the
  process as a child, no port is opened, and long solves stay asynchronous jobs on the same
  execution backend. MCP is a thin adapter on top of the same operations, never a dependency of
  the core.
- **Progressive discovery.** Capabilities are described in sections on request (geometries,
  params, metrics, artifacts, environment requirements) rather than as one payload containing
  every schema. Extended schemas are fetched by reference.
- **Object references.** Geometries, materials, designs, studies and results live in a workspace
  under stable identifiers, so an iteration sends a patch and an id instead of a whole geometry —
  and a second run can reuse what did not change. That workspace extends the existing `JobStore`
  rather than becoming a second store.
- **Compact results.** `status`, `metrics`, `diagnostics`, `fields` and `artifacts` are distinct
  response levels. The default answer to "how did the solve go" is scalars and diagnostics — the
  result's `stats` are already the beginning of that — while nodal arrays are artifacts retrieved
  by reference or queried selectively (max, integral, value at a point, section, hotspots).
- **No arbitrary execution.** The local interface exposes engineering operations and domain
  objects. It does not accept Python, UFL, shell commands or container images — see the security
  posture below.

Everything above without *(done)* is still to build; the workspace, result-query and study
services (#44, #46, #48) go into `core/` beside what is there now.

## Design principles

1. **The protocol is the product.** Widgets and server are replaceable; the JSON contract for
   geometry, jobs, events, and fields is what makes the ecosystem composable. It must stay
   language-neutral and versioned (`/api/v1`).
2. **Solvers are plug-ins.** The server core knows nothing about FEniCSx. A solver is any class
   implementing the `Solver` protocol (`name`, `describe()`, `solve(geometry, params, progress)`),
   registered in the registry. FEniCSx adapters register themselves only when dolfinx imports
   successfully, so the same codebase runs in a plain Python venv (mock solver) or in the dolfinx
   Docker image (real solvers).
3. **Front-end development must never require FEniCSx.** The mock solver (pure NumPy potential
   flow) returns the same result schema as the real adapters. Widget CI runs against it.
4. **Small results travel inline, large results travel by reference.** v0 returns 2D grid fields
   as JSON typed arrays (fine up to ~1M values). Binary payloads (VTU/XDMF, later glTF) travel as
   `artifacts` fetched by URL. For non-human consumers the same principle is sharpened at M2.5:
   the default answer is scalar metrics and diagnostics, and fields are fetched or queried
   explicitly.
5. **Transports are adapters, the core is the product** (landed, M2.5). HTTP/WebSocket,
   JSON-RPC over stdio, CLI, Python and MCP all map onto one application core with one set of
   models. An operation added to the core is available everywhere; a shared conformance suite
   keeps the adapters from drifting apart.
6. **Thin about physics, thick about claims.** The protocol adds no physical semantics FEniCS
   does not have, and carries everything a capability asserts or refuses. Where the two pull
   apart, an addition earns its place by making something *refusable or declarable* that would
   otherwise live only inside an adapter's source — which is why `axisymmetric2d` is a kind and
   a hypothetical `beam1d` would not be. The test, and the case that looks like a
   counter-example, are [ADR 0005](adr/0005-thin-about-physics-thick-about-claims.md).

## Component decisions and trade-offs

### API server: FastAPI
Async-native (WS streaming falls out for free), pydantic models double as protocol schema source,
OpenAPI docs for free. Alternative considered: trame — rejected as the *core* because it owns the
whole page and is hard to embed into an existing product front-end; it remains a fine consumer of
the same protocol.

### Job execution: staged approach
- **M0 (now):** in-process `asyncio` job manager running solves in a thread pool. Zero
  infrastructure, fine for demos and single-user tools. Progress callbacks are marshalled from the
  worker thread onto the event loop and fanned out to WebSocket subscribers; events are replayed to
  late subscribers.
- **M3 (landed):** resource limits, job persistence, API-key auth and per-principal quotas, and
  a pluggable execution backend. `FENIXSPOON_REDIS_URL` switches solving from a bounded thread
  pool in the API process to worker containers draining an arq queue; the API layer is unchanged
  either way, which is what the job-manager interface was shaped for.
- **M2.5 (landed):** the manager surface (submit / get / cancel / subscribe) was lifted into a job
  *service* in the transport-neutral core, so a local process drives jobs without an HTTP server.
  The `ExecutionBackend` split above had already put the seam in the right place — what M2.5
  added is a caller that is not FastAPI, plus a content-addressed result identity so an equivalent
  resubmission hits the cache instead of recomputing. A layering change, not a second job system,
  and the vertical slice asserts the "no HTTP server" half by running in a subprocess that fails
  if FastAPI was imported.

  [Load testing](06-load-test.md) made the worker case concretely, and it is M3's rather than this
  one's: one API process handles 50 concurrent clients without dropping a stream, but every
  in-process solve shares the interpreter with the event loop, so a Python-heavy solver's
  throughput *falls* as concurrency rises. Workers remove that ceiling, make a per-job memory
  limit expressible at last, and let the API scale separately from the solving.

### Distributed execution: three things cross the boundary, and only three
The API and its workers share a job store and a Redis, nothing else. What moves between them:

1. **The job**, as JSON on an arq queue — geometry and params, revalidated worker-side against
   the solver's own schema so a version skew fails at validation rather than reaching a solver
   as nonsense.
2. **Progress**, over Redis pub/sub. Pub/sub is lossy by design, which is fine because it is only
   the live edge: a subscriber attaches to the channel first, then replays the durable log from
   the store, and reconciles the overlap by sequence number. Neither path has to be reliable
   alone.
3. **Cancellation**, as a Redis flag the running solve polls. Still cooperative — nothing kills a
   solve mid-iteration — so the semantics match the in-process backend exactly.

Everything else (results, artifacts, status, history) goes through the store, which is why the
data directory has to be one shared volume. Redis holds no state worth keeping: lose it and
queued work is lost, but nothing that already happened is.

The honest gap is worker death. Nothing heartbeats, so a job whose worker is SIGKILLed stays
`running` until retention removes it — and the API deliberately does *not* fail running jobs on
startup in this mode, because in a healthy deployment those jobs really are still being solved.

### Persistence: live state in memory, everything else in a store
Subscriber queues, the cancel event and the running future cannot be serialized, so they stay in
the process. Everything a client can still ask for afterwards — metadata, the event log, the
result payload, the artifact list — goes to a `JobStore`. SQLite is the default backend and the
data directory is the durable unit: metadata and events in `jobs.db`, result payloads and
artifacts in `<data-dir>/<job-id>/`. Mount that directory and a restarted server answers for
jobs the previous one ran.

Result payloads live on disk rather than in the database on purpose: a 512×341 grid is several
megabytes of JSON, the data directory is already the durable-storage contract for artifacts, and
keeping them together makes one job's bytes one directory you can copy, delete or mount.

That only pays if nothing quietly keeps a second copy, which is a discipline rather than a
guarantee, and it has to hold in two places. The live cache is **for jobs that are being solved**:
once a job is terminal its entry is dropped, because the only things it still held that the store
does not were the cancel handle and a status that had stopped moving — plus the payload. And a
store read fetches **only what the caller asked for**: a status poll, a cancel and an artifact
download take metadata alone, so `GET /jobs/{id}` does not pay for a multi-megabyte read to answer
with six fields, and a history page does not pay for it once per row.

Restarting introduces a state the in-process manager never had: a job the store believes is
`running` that nothing is solving. Startup reconciliation fails those explicitly — a status
stream that can never terminate is worse than a job that admits it was lost.

### Geometry: parametric JSON, meshed server-side
Clients send parametric descriptions (v0: `polygon2d` obstacle in a rectangular domain; since
1.13 also `axisymmetric2d`, a meridian (r, z) section of a body of revolution; later: spline
profiles, CSG of primitives). The server meshes with Gmsh
(OpenCascade kernel) and imports via `dolfinx.io.gmshio`. In-browser CAD kernels (OpenCascade.js)
are deliberately out of the core: heavy, and the mesh must be produced server-side anyway.

### Visualization: client-side rendering, on canvas rather than vtk.js
The plan here was vtk.js; M2 shipped canvas instead, and the reason is the embed footprint. Every
*field* result kind is 2D — `grid2d` and `mesh2d`, both scalar — so a multi-megabyte WebGL
toolkit would have dominated the download for capability nothing yet uses.
`@fenix-spoon/viewer` draws both kinds, with colormaps, a colorbar, iso-contours and a hover
probe, in a fraction of that.

It also *explores* them: pointer-anchored zoom, drag and pinch pan, fit-domain and
fit-geometry, a probe and a section that sample the arrays already in the page, a pinnable
colour scale for comparing two solves, vector glyphs, and integral curves of a vector field.
**None of that touched the wire protocol**, which is the interesting part of the decision and
is recorded in [ADR 0001](adr/0001-explorable-viewer.md): every one of those operations is a
function of data `grid2d` and `mesh2d` have carried since protocol 1.1, so a result from an
older server explores exactly as well as a new one, and no solver adapter acquired a view on
how a browser draws its output. What the viewer deliberately does *not* do is infer: it will
not build a vector field out of a scalar one to draw curves, and it will not derive a geometry
outline from the `grid2d` mask — the application supplies the geometry, and an unavailable
tool carries a sentence saying why rather than quietly drawing nothing.

Protocol 1.4's `series1d` is the first kind that is not a field at all, and it did not get a
renderer. A curve wants axes, a legend, a hover readout and — for `C_p` — an inverted `y`, none
of which is a *mode* of a field viewer; it is a second small widget, and the protocol carrying the
shape is what makes writing one a contained job rather than a convention each consumer invents.
`<fs-viewer>` refuses a `series1d` and says so rather than drawing it badly.

The drawing surface is isolated, so a WebGL backend can land with the first result kind that needs
it — 3D (#25). The rest of the widget is arranged for that: the coloured field is rasterised once
per (field, colormap, range) and blitted thereafter, contours and integral curves are computed in
domain coordinates and cached, and a pan or a zoom only re-projects them. That is what makes
60 fps navigation over a 175k-point grid a matter of one `drawImage` rather than of the
rendering backend.

Server-side rendering (trame-style image streaming) remains an escape hatch for huge models, not
the default: client-side keeps interaction latency low and the server stateless between jobs.

### Deployment: one Docker image
`server/Dockerfile` builds `FROM dolfinx/dolfinx:stable` (overridable via `BASE_IMAGE` build arg to
a plain `python:3.12-slim` for mock-only deployments). One image serves both roles — which one a
container is depends on its command, not its build. `docker-compose.yml` runs the API alone;
`docker-compose.workers.yml` layers on Redis and N worker containers.

## Security posture (why solvers are declarative)

A naive "FEniCS as a service" accepts Python/UFL code from the client — that's remote code
execution by design. Fenix Spoon's protocol instead exposes **named solvers with typed parameters**:
the client chooses *what* to solve from a server-defined menu, never *how*. Custom physics are
added by deploying a new adapter server-side. If arbitrary-UFL mode ever becomes a feature, it must
be opt-in and sandboxed (gVisor/firejail + resource limits), and that is explicitly out of scope
until M5.

Around that core sit the guardrails a multi-user deployment needs: a submit-time cell budget, a
cooperative wall-clock timeout, optional API-key auth with per-principal job isolation, and
per-principal quotas. All are off or unlimited by default, because the dev experience this
project exists to enable — clone, run, open a browser — must not require configuring an identity
provider. [Deployment](05-deployment.md) is the recipe for turning them on.

Identity is one replaceable object. `app.state.auth` resolves a presented credential to a
`Principal`; API keys are the implementation shipped, and OIDC or a trusted-proxy header is a
subclass. Everything downstream keys off `Principal.id`, so job ownership and quotas work
unchanged whatever produces it — including a future local transport, which authenticates by being
able to start the process and resolves to a principal like any other caller rather than bypassing
ownership.

**The declarative rule holds for the local agent interface too** (M2.5). It is tempting to hand an
agent `run_python(code)` or `execute_shell(command)` and call it a day — on a local machine the
agent often *can* run a shell anyway, so it feels free. It isn't: an interface made of arbitrary
execution has no schema to discover, no validation, no cache identity, no provenance, and cannot
later be exposed over a network without becoming remote code execution. The local interface
therefore exposes the same vocabulary as the HTTP API — named capabilities, typed parameters,
domain objects, engineering operations — and no `run_python`, `execute_shell`, `run_ufl`,
`install_package` or `start_container`. Agents translate human intent into typed requests; the
core validates and executes defined engineering operations.

One limit is deliberately absent: a per-job memory ceiling. Solves run on threads in the API
process and a memory limit is a property of a process, so the honest enforcement points today
are the cell budget and the container's own limit. Per-job ceilings arrive with the worker
backend, where each solve is a process.

## Package layout (server)

```
server/fenixspoon/
├── main.py            # app factory, CORS, static demo mount
├── api.py             # /api/v1 routes: solvers, jobs, events WS, results, objects, studies
├── jobs.py            # JobManager: submit/status/events/result, retention, reconciliation
├── store.py           # JobStore: durable job metadata, event log and result payloads
├── objects.py         # ObjectFileStore: workspace objects as diffable JSON, by revision
├── cache.py           # content-addressed result identity and the environment fingerprint
├── execution.py       # run_solve: the one solve path, shared by pool and worker
├── backends.py        # ExecutionBackend: in-process pool, or arq over Redis
├── events.py          # EventBus: in-process fan-out
├── redis_bus.py       # EventBus over Redis pub/sub, for the worker deployment
├── worker.py          # arq entry point: `arq fenixspoon.worker.WorkerSettings`
├── auth.py            # Principal resolution (API keys), quotas, CORS policy
├── geometry.py        # pydantic models for the geometry schema (protocol source of truth)
├── boundaries.py      # a named boundary resolved to one predicate both halves of a pair use
├── protocol.py        # pydantic models for requests, events and result envelopes
├── series.py          # the `series1d` curve models, below protocol so both can import them
├── frames.py          # the time index: which instants a solve stored, and where each lives
├── modes.py           # the mode index: its twin over an ordering that is not time
├── fields.py          # the bounded field queries `result.query` answers
├── core/              # the transport-neutral application core (see above)
├── rpc/               # JSON-RPC over stdio: method table, framing, error codes
├── mcp_adapter.py     # the MCP tool surface, bound to the RPC method table
└── solvers/
    ├── base.py        # Solver protocol, SolverContext, ProgressEvent, SolverResult
    ├── registry.py    # name → solver class, availability-aware
    ├── declarations.py # the metric/assumption/condition sets an adapter pair shares
    ├── _gmsh.py       # shared Gmsh meshing helpers for the FEniCSx adapters
    ├── mock_*.py      # seven NumPy stand-ins — potential flow, magnetostatics, steady and
    │                  # transient conduction, elasticity, axisymmetric electrostatics, modes
    └── dolfinx_*.py   # the seven FEniCSx twins, each registering only if dolfinx imports
```

**The adapters are listed by pattern rather than by name**, and that is a statement about the
shape rather than brevity: every physics here ships *twice*, once in NumPy and once on a mesh,
and the pair shares its declarations, its material handling and its post-processing. A single
`mock_*.py` with no twin would be the thing worth naming.

The FEniCSx adapters register only when dolfinx imports, so the same codebase runs in a plain
venv (mock solvers only) or in the dolfinx image (both).

`geometry.py`, `protocol.py`, `solvers/`, and the M3 modules (`store.py`, `backends.py`,
`events.py`, `execution.py`, `auth.py`) know nothing about FastAPI. `api.py` is where the remaining
coupling sits: request validation, budget and quota checks, artifact URLs and error mapping are
expressed as route bodies and `HTTPException`s, so no other caller can reuse them. M2.5 moves that
logic into a `core/` package (capability catalog, workspace, object store, job service, result
queries, studies) and leaves `api.py` as the HTTP adapter over it; the local transports live beside
it as peers rather than as clients of the web server.
