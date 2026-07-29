# Architecture

Fenix Spoon is three loosely-coupled layers joined by one contract (the
[wire protocol](04-wire-protocol.md)).

## Today (M2)

```mermaid
flowchart TB
    subgraph client["Client layer (npm packages, M2)"]
        direction LR
        GEO["@fenix-spoon/geometry-2d<br/>parametric profile editor"]
        SDK["@fenix-spoon/client<br/>JS SDK: jobs, WS events"]
        VIEW["@fenix-spoon/viewer<br/>vtk.js field viewer"]
    end
    subgraph proto["Wire protocol (JSON over REST + WebSocket)"]
        P1["geometry schema · job lifecycle · progress events · field results"]
    end
    subgraph server["Server layer (Python package `fenixspoon`)"]
        API["FastAPI app<br/>REST + WS + OpenAPI"]
        JOBS["Job manager<br/>(in-process asyncio → Celery/arq at scale)"]
        REG["Solver registry"]
        subgraph adapters["Solver adapters"]
            MOCK["mock.laplace2d<br/>(NumPy, always available)"]
            DFX["dolfinx.*<br/>(FEniCSx + Gmsh, in Docker)"]
        end
    end
    client --> proto --> server
    API --> JOBS --> REG --> adapters
```

## Where this is going (M2.5, planned)

The browser is an important client, not the only one. A machine that has FEniCSx and local agents
should be able to use Fenix Spoon as a structured interface to its own compute environment,
through typed calls and compact answers. That turns the project from "a toolkit for putting
FEniCSx behind a web app" into **a queryable runtime and protocol for building, running and
automating FEniCSx-based engineering simulation workflows**.

Structurally that is one change: HTTP stops being the domain and becomes one adapter among
several, over a core that knows nothing about transports.

```text
Browser / SDK ─────┐
HTTP clients ──────┤
CLI / Python ──────┤
JSON-RPC stdio ────┼── Transport adapters
MCP hosts ─────────┘
                         │
                         ▼
               Transport-neutral core
                         │
                         ▼
         solver registry / jobs / results
                         │
                         ▼
              FEniCSx / mock solvers
```

Nothing below the adapter line is implemented yet — today the equivalent logic lives inside
`api.py`. The milestone that builds it is [M2.5](03-roadmap.md#m25--local-automation-and-agent-interface)
and the design specification is the [local agent interface](05-local-agent-interface.md). The
properties that layer is required to have:

- **Transport-neutral core.** Capability catalog, workspace, object store, job service, result
  query service and study service are plain Python objects with typed inputs and outputs. They
  raise domain errors, not `HTTPException`, and they never build URLs.
- **HTTP is an adapter, not the domain.** `api.py` becomes a mapping from routes to core calls
  and from domain errors to status codes. The `/api/v1` contract does not change; what changes is
  that JSON-RPC, CLI, Python and MCP can reach the same operations without going through FastAPI
  or a network port.
- **A local interface.** JSON-RPC 2.0 over stdio is the base local transport: the agent starts the
  process as a child, no port is opened, and long solves stay asynchronous jobs. MCP is a thin
  adapter on top of the same operations, never a dependency of the core.
- **Progressive discovery.** Capabilities are described in sections on request (geometries,
  params, metrics, artifacts, environment requirements) rather than as one payload containing
  every schema. Extended schemas are fetched by reference.
- **Object references.** Geometries, materials, designs, studies and results live in a local
  workspace under stable identifiers, so an iteration sends a patch and an id instead of a whole
  geometry — and a second run can reuse what did not change.
- **Compact results.** `status`, `metrics`, `diagnostics`, `fields` and `artifacts` are distinct
  response levels. The default answer to "how did the solve go" is scalars and diagnostics; nodal
  arrays are artifacts retrieved by reference or queried selectively (max, integral, value at a
  point, section, hotspots).
- **No arbitrary execution.** The local interface exposes engineering operations and domain
  objects. It does not accept Python, UFL, shell commands or container images — see the security
  posture below.

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
   as JSON typed arrays (fine up to ~1M values). The protocol reserves an `artifacts` field for
   URLs to binary payloads (glTF, VTU/VTKHDF) for M1+. For non-human consumers the same principle
   is sharpened at M2.5: the default answer is scalar metrics and diagnostics, and fields are
   fetched or queried explicitly.
5. **Transports are adapters, the core is the product** (planned, M2.5). HTTP/WebSocket,
   JSON-RPC over stdio, CLI, Python and MCP all map onto one application core with one set of
   models. An operation added to the core is available everywhere; a shared conformance suite
   keeps the adapters from drifting apart.

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
- **M2.5:** the manager surface (submit / get / cancel / subscribe) is lifted into a job *service*
  in the transport-neutral core, so a local process can drive jobs without an HTTP server, and
  results gain a content-addressed identity that lets an equivalent resubmission hit a local
  cache instead of recomputing. Execution stays in-process; this is a layering change, not a
  second job system.
- **M3:** pluggable backend with a Celery (or arq) implementation for multi-user deployments —
  worker containers with dolfinx, Redis broker, job persistence, resource limits (mesh size caps,
  wall-clock timeouts), and auth. The job-manager interface is written so this swap doesn't touch
  the API layer — and, after M2.5, so that the queued backend implements the same job-service
  interface every transport already speaks.

### Geometry: parametric JSON, meshed server-side
Clients send parametric descriptions (v0: `polygon2d` obstacle in a rectangular domain; later:
spline profiles, axisymmetric sections, CSG of primitives). The server meshes with Gmsh
(OpenCascade kernel) and imports via `dolfinx.io.gmshio`. In-browser CAD kernels (OpenCascade.js)
are deliberately out of the core: heavy, and the mesh must be produced server-side anyway.

### Visualization: client-side rendering first
v0 renders 2D fields with raw canvas (demo) and M2 wraps vtk.js for a real viewer widget
(unstructured meshes, contours, vectors, 3D). Server-side rendering (trame-style image streaming)
is an escape hatch for huge models, not the default: client-side keeps interaction latency low and
the server stateless between jobs.

### Deployment: one Docker image
`server/Dockerfile` builds `FROM dolfinx/dolfinx:stable` (overridable via `BASE_IMAGE` build arg to
a plain `python:3.12-slim` for mock-only deployments). `docker-compose.yml` runs the API; M3 adds
worker + Redis services.

## Security posture (why solvers are declarative)

A naive "FEniCS as a service" accepts Python/UFL code from the client — that's remote code
execution by design. Fenix Spoon's protocol instead exposes **named solvers with typed parameters**:
the client chooses *what* to solve from a server-defined menu, never *how*. Custom physics are
added by deploying a new adapter server-side. If arbitrary-UFL mode ever becomes a feature, it must
be opt-in and sandboxed (gVisor/firejail + resource limits), and that is explicitly out of scope
until M5.

**The same rule holds for the local agent interface.** It is tempting to hand an agent
`run_python(code)` or `execute_shell(command)` and call it a day — on a local machine the agent
often *can* run a shell anyway, so it feels free. It isn't: an interface made of arbitrary
execution has no schema to discover, no validation, no cache identity, no provenance, and cannot
later be exposed over a network without becoming remote code execution. Fenix Spoon's local
interface therefore exposes the same declarative vocabulary as the HTTP API — named capabilities,
typed parameters, domain objects, engineering operations — and no `run_python`, `execute_shell`,
`run_ufl`, `install_package` or `start_container`. Agents translate human intent into typed
requests; the core validates and executes defined engineering operations.

## Package layout (server)

```
server/fenixspoon/
├── main.py            # app factory, CORS, static demo mount
├── api.py             # /api/v1 routes: solvers, jobs, events WS, results
├── jobs.py            # JobManager: submit/status/events/result
├── geometry.py        # pydantic models for the geometry schema (protocol source of truth)
└── solvers/
    ├── base.py        # Solver protocol, ProgressEvent, SolverResult
    ├── registry.py    # name → solver class, availability-aware
    ├── mock_laplace.py    # NumPy potential-flow solver (reference implementation)
    └── dolfinx_poisson.py # FEniCSx + Gmsh adapter (registers only if dolfinx imports)
```

Of these, `geometry.py`, `protocol.py` and `solvers/` are already transport-neutral — they know
nothing about FastAPI. What is still entangled lives in `api.py`: solver lookup, geometry-kind
checking, params validation, artifact-URL construction and error mapping are expressed as route
bodies and `HTTPException`s, so no other caller can reuse them. M2.5 moves that logic into a
`core/` package (capability catalog, workspace, object store, job service, result queries,
studies) and leaves `api.py` as the HTTP adapter over it; the local transports live beside it as
peers rather than as clients of the web server.
