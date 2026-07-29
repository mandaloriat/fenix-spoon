# Roadmap

Milestones are ordered so that every one ends with something demonstrable. Every unchecked item
is tracked as a GitHub issue, and each milestone has a tracking issue with the suggested order:
[M1](https://github.com/mandaloriat/fenix-spoon/issues/26) ·
[M2](https://github.com/mandaloriat/fenix-spoon/issues/27) ·
[M2.5](https://github.com/mandaloriat/fenix-spoon/issues/52) ·
[M3](https://github.com/mandaloriat/fenix-spoon/issues/28) ·
[M4](https://github.com/mandaloriat/fenix-spoon/issues/29) ·
[M5](https://github.com/mandaloriat/fenix-spoon/issues/30)

M0–M2 build the browser path: geometry in, job out, field rendered. M3 takes that path to
production. M2.5 is a different axis: the same core, reachable from a *local* process — a script,
a CLI, an agent — without a web application. Its number records a dependency, not a date: it sits
on the M2 core and needs nothing from M3, and was rebased onto M3 once that landed.

## M0 — Kickstart (this repository state) ✅

Goal: a working vertical slice with zero heavy dependencies, plus the planning documents.

- [x] State-of-the-art survey, architecture, wire protocol v0 draft
- [x] FastAPI server: job submission, WebSocket progress streaming, result retrieval
- [x] Solver adapter protocol + registry
- [x] Mock solver: 2D potential flow (Laplace) around a polygon, pure NumPy
- [x] Browser demo: draggable airfoil editor + live field rendering (no build step)
- [x] Test suite green without FEniCSx; CI workflow
- [x] Docker scaffolding (dolfinx base image + compose)

## M1 — Real FEniCSx path ✅

Goal: the demo runs on an unstructured FEniCSx solve inside Docker, same UX.

- [x] Harden `dolfinx_poisson` adapter: Gmsh meshing of `domain2d` geometry, validated against the
      mock solver on coincident cases (#1)
- [x] CI job that runs the FEniCSx adapter tests inside the `dolfinx/dolfinx` image (#2)
- [x] Result serialization for unstructured meshes: triangles + node values (protocol `mesh2d`
      kind) — emitted by both the mock solver and the FEniCSx adapter (#3)
- [x] Artifact channel: results downloadable as VTK; inline JSON kept for small fields (#4).
      *XDMF/VTU via dolfinx I/O remains a follow-up.*
- [x] Second physics example: 2D magnetostatics of a solenoid cross-section, exercising
      material regions in the geometry schema (#5) — added the `regions2d` geometry kind,
      `mock.magnetostatics2d` and `dolfinx.magnetostatics2d`, and `examples/solenoid-2d/`.
      *Axisymmetric (A-φ) formulation deferred: the planar cut is what the demo needs, and
      axisymmetry belongs with a dedicated `axisymmetric2d` geometry kind.*
- [x] Mesh-size/quality parameters exposed through solver params, with server-side caps (#6)
      — wall-clock timeout, cancellation, and a submit-time cell budget (`FENIXSPOON_MAX_CELLS`)
      refusing over-sized jobs with an actionable 422 before they start. Every solve reports
      what it cost (`stats`: cells, dofs, iterations, seconds), surfaced in both demos.

## M2 — Embeddable client widgets ✅

Goal: `npm install` two widgets and build the demo page in ten lines.

- [x] `@fenix-spoon/client` — typed JS/TS SDK for the wire protocol (fetch + WS, reconnection),
      plus runtime validators mirroring the server's pydantic rules (#7)
- [x] `@fenix-spoon/geometry-2d` — framework-agnostic (custom element) parametric 2D profile
      editor: draggable points, polygon and centripetal Catmull-Rom spline modes, undo/redo,
      full keyboard operation, JSON in/out per the geometry schema (#8).
      *Constraint solving (symmetry, tangency) deferred — it wants its own design pass.*
- [x] `@fenix-spoon/viewer` — field viewer custom element: `grid2d` and `mesh2d`, colormaps,
      colorbar, iso-contours, hover probe (#9).
      *Built on canvas rather than vtk.js: every result kind is 2D today, and a multi-megabyte
      WebGL toolkit would blow the embed footprint. The rendering surface is isolated so a WebGL
      backend can land with the first 3D result kind (#25). Vector glyphs await a vector field.*
- [x] Rebuild `examples/airfoil-2d` on the widgets; keep the zero-dependency version as
      reference (#10) — the server now serves built packages at `/packages/`, and the page
      loads them through an import map. *Rebuilding it caught a browser-only SDK bug: an
      unbound `globalThis.fetch` throws "Illegal invocation" in a page, which Node never
      reproduces. That is the payoff of this item.*
- [x] Versioned protocol conformance tests shared between server (pytest) and SDK (vitest) —
      both sides read `protocol/fixtures/` and CI runs both (#11)

## M2.5 — Local automation and agent interface

Goal: *a local agent can inspect the installed engineering capabilities, create or update a design,
submit a simulation, query compact metrics and diagnostics, and retrieve artifacts without starting
a web application or receiving full field payloads in its context.*

Everything needed to drive Fenix Spoon from a process on the same machine — single user,
in-process jobs, local workspace, compact answers. It requires nothing that M3 added: no Redis, no
API keys, no worker containers, no shared volume. What M3 built it should *reuse* rather than
duplicate — the `JobStore`, the `ExecutionBackend`, the `EventBus` and the `Principal` are already
the seams a second caller needs. The design specification is
[docs/07-local-agent-interface.md](07-local-agent-interface.md), which this milestone implements.

- [ ] **Transport-neutral application core.** M3 already lifted execution, persistence and event
      delivery out of the API layer. What is still route-shaped is the *request* logic: solver
      lookup, geometry-kind checking, params validation, cell-budget and quota checks, artifact-URL
      construction and error mapping all live in `api.py` as `HTTPException`s. Extract that into an
      application layer — capability catalog, workspace service, object store, job service, result
      query service, study service — and make `api.py` an adapter over it, with the HTTP behavior,
      paths and status codes unchanged. (#42)
- [ ] **Progressive capability discovery.** `environment.inspect`, `capability.list`,
      `capability.describe` with section selection (geometries, params, metrics, artifacts, cost
      estimation, sweep / gradient / MPI support, environment requirements). Full JSON Schemas are
      fetched by reference or on request, never dumped on every call — `GET /solvers` returns every
      schema for every solver today, which is right for a form generator and wrong for a caller
      that only wants to know what is installed. (#43)
- [ ] **Local workspace and object references.** An inspectable, reopenable workspace holding
      `geometry`, `material`, `boundary_condition`, `load_case`, `design`, `study`, `result` and
      `artifact` objects under stable identifiers (`geometry:g-42`, `design:d-18`, `study:s-9`,
      `result:r-105`). Iterations reference objects instead of resending geometry; incremental
      edits go through a standard patch mechanism (JSON Patch is the leading candidate). Jobs,
      results and artifacts are already durable in the `JobStore` — the workspace extends that
      store, it does not open a second one beside it. (#44)
- [ ] **JSON-RPC 2.0 over stdio.** A local transport with no mandatory network port: the agent
      spawns `fenix-spoon rpc --stdio` as a child process and speaks structured messages over its
      pipes — documented framing, typed compact errors, long solves as asynchronous jobs, working
      identically against mock solvers and real FEniCSx. This is the base local transport; MCP
      is layered on it, not the other way round. (#45)
- [ ] **Compact results: metrics, diagnostics and selective field queries.** Separate the response
      levels `status` / `metrics` / `diagnostics` / `fields` / `artifacts`. Diagnostics already
      half exist as the result's `stats` (cells, dofs, iterations, seconds) — formalize that rather
      than inventing a parallel channel, and add the part that is missing: declared scalar
      engineering metrics (mass, maximum displacement, maximum stress, safety factor, strain
      energy, force, inductance, peak temperature). Full fields travel as artifacts by reference,
      with selective queries for max, min, mean, integral, point value, region value, section,
      decimated sampling and hotspot location. (#46)
- [ ] **Content-addressed cache and provenance.** Deterministic hashing of geometry, solver,
      parameters and environment; local result cache; deduplication of equivalent jobs; full
      provenance on every result and an explicit design → study → job → result relation. The cache
      cuts both compute cost and the amount an agent has to re-exchange. (#47)
- [ ] **Study abstraction.** One vocabulary under which parameter sweeps, mesh-convergence studies,
      material comparisons, load-case comparisons, parametric optimization and model calibration
      can be expressed. The first slice implements a single small, controlled study kind —
      enough to prove that several jobs can be orchestrated through object references and
      synthetic results. No universal optimizer here (that stays M5, #22). (#48)
- [ ] **MCP adapter.** A thin Model Context Protocol server over the same core and the same
      operations as JSON-RPC: a small stable tool vocabulary (inspect environment, list
      capabilities, describe capability, create or patch object, submit job, inspect job, query
      result, run study), progressive discovery, artifacts exposed as resources. No tool per
      solver, no application logic in the adapter, no MCP dependency in the core. (#49)
- [ ] **CLI and Python adapters.** `fenix-spoon <command> --json` for shells and reproducible
      scripting, plus a direct in-process Python API — the same models and semantics as HTTP,
      JSON-RPC and MCP, and the debugging surface for everything an agent can do. (#50)
- [ ] **Cross-transport conformance and the vertical slice.** Shared fixtures asserting that an
      equivalent request over HTTP and over JSON-RPC yields the same semantic result, that
      validation errors are represented consistently, that mock and FEniCSx honor the same
      envelopes, that schemas do not diverge between adapters, and that compact responses never
      leak full numeric arrays. (#51)

**Exit criterion (demonstrable).** From a local agent process, with no HTTP server running:
discover the available potential-flow capability, create an airfoil design in a workspace, submit
a solve (mock or FEniCSx), receive progress, query compact field metrics, retrieve the VTK
artifact by reference, patch one geometry parameter, and run a second solve that reuses the
unchanged objects and reports what it took from cache. The solenoid and the heat sink are equally
acceptable subjects. The milestone is *not* done when JSON-RPC merely answers — it is done when
that iterative loop runs end to end.

## M3 — Production job execution

Goal: multi-user deployments are safe and boring. *Everything below has landed except the Helm
chart, which is deliberately unwritten — see #15.*

Scope note: M3 is the *multi-user, distributed* milestone — queue, separate workers, Redis,
server-side persistence, authentication, per-user quotas, deployment and load testing. M2.5
depends on none of it and must not grow a second parallel job system: the job service it extracts
wraps the same `ExecutionBackend` interface the arq backend already implements.

Scope note: M3 is the *multi-user, distributed* axis — queue, separate workers, Redis,
server-side persistence, authentication, per-user quotas, object storage, deployment and load
testing. M2.5 requires none of it at runtime and must not grow a second job system beside the
`ExecutionBackend` and `JobStore` this milestone built.

- [x] Pluggable job backend: Celery or arq implementation (Redis), worker containers with
      dolfinx (#12) — an `ExecutionBackend` with an in-process pool (the default) and an
      arq/Redis backend, an `EventBus` with in-process and Redis pub/sub implementations,
      a worker entry point, cross-process cancellation, and a compose override for
      API + Redis + N workers. *arq over Celery: async-native, and used purely as a
      dispatcher so the job store stays the single source of truth — reasoning in
      [architecture](02-architecture.md) and [backends.py](https://github.com/mandaloriat/fenix-spoon/blob/main/server/fenixspoon/backends.py).
      Not done: heartbeats, so a job whose worker is killed stays `running` until the
      retention sweep. Postgres would let several API replicas share state; SQLite on a
      shared volume already supports API + N workers.*
- [x] Job persistence (SQLite/Postgres) and artifact storage (filesystem/S3-compatible) (#13)
      — a `JobStore` interface with SQLite (default) and in-memory backends: metadata and the
      event log in the database, result payloads and artifacts on disk under the data
      directory. Adds `GET /jobs` history with pagination, a configurable retention sweep
      (`FENIXSPOON_JOB_TTL`), and startup reconciliation so a job orphaned by a dead process
      fails instead of hanging its client. *Postgres and S3 are left to the interface: an
      untested backend for a database nobody has run against would be pretend infrastructure.
      A Postgres store implements the same six methods.*
- [x] Auth hooks (API keys / OIDC middleware), per-user quotas, wall-clock and memory limits (#14)
      — API keys with anonymous still the dev default, per-principal job isolation and quotas
      (concurrent, per hour, artifact bytes), CORS that stops defaulting to `*` once keys are
      configured, and [a deployment recipe](05-deployment.md). *No per-job memory ceiling: solves
      run on threads, and a memory limit is a property of a process. The cell budget is the proxy
      and the container limit is the backstop until the worker backend (#12) makes each solve its
      own process.*
- [ ] Helm chart / compose profiles for API + workers + Redis (#15) — *partially done: the
      compose side ships as `docker-compose.workers.yml` (an override rather than a profile,
      because a profile cannot change the API's environment, and the API must switch backend
      and event bus together), and GHCR publishing builds both image variants with a startup
      smoke test. The Helm chart is deliberately not written: Helm is not installable in the
      environment this was developed in, so it could not be linted, let alone installed —
      and an untested deployment recipe reads as supported when it is not.*
- [x] Load test: N concurrent solves with progress streaming (#16) — `make loadtest` plus
      [documented results](06-load-test.md). 50 concurrent clients and 100 live WebSockets
      with zero failures and zero dropped streams. It found the concurrency knob nobody had
      set: solves ran on asyncio's shared default executor, so they competed for threads
      with everything else the server hands off. They now get a bounded pool of their own
      (`FENIXSPOON_MAX_WORKERS`), and the measurements show why the default is the core
      count — FEniCSx parallelizes, the pure-Python mock solvers get *slower* with it.

## M4 — Gallery and docs site

Goal: adoption. People find, run, and copy examples.

- [x] Docs site (mkdocs-material) with protocol reference generated from pydantic models (#17)
      — published to GitHub Pages on merge, with per-audience getting-started paths (embed
      the widgets / deploy the server / write a solver adapter). The protocol reference is
      generated into the repository and CI fails on a stale page, so a field description
      cannot change without the docs changing in the same commit. Writing the generator
      exposed 54 model fields with no description at all; filling them in improved the
      OpenAPI page at `/docs` as much as the site.
- [x] Example gallery: potential flow, solenoid magnetostatics and a heat sink, each a
      copy-paste-able app, with a [gallery index](gallery.md) (#18) — *the heat sink ships two
      adapters, `mock.heat2d` and `dolfinx.heat2d`, cross-validated against each other, and its
      demo page builds its controls from `params_schema`.*
- [ ] Vector fields on the wire and in the viewer (#62) — *split out of #18. Navier–Stokes was
      blocked rather than unwritten: `fields` and `point_fields` are maps of name → scalar, so a
      velocity cannot be expressed as one thing and the viewer has no glyphs. Both potential-flow
      adapters already ship `|v|` for exactly this reason. Unblocks flux and current-density
      rendering too, which three adapters currently flatten to a magnitude.*
- [ ] "Deploy to Fly.io/Render/self-host" one-clickish guides (#19)
- [x] A protocol version on the wire, a compatibility rule, and a bump procedure (#58) —
      `GET /api/v1/version`, the one route outside the auth gate, because a client has to
      know whether it can talk to a server before it can sensibly send anything. An
      *operation* rather than a header or a payload field, so the M2.5 transports — which
      have neither headers nor paths — can answer the same question. MAJOR is mirrored in
      the path; the [breaking-vs-additive rule](04-wire-protocol.md#what-is-a-breaking-change)
      and the bump checklist are written down, and one version number lives in three places
      with each side asserting against the shared fixture so none can move quietly.
- [ ] Announce: FEniCS Discourse, r/CFD, Hacker News (#20)

## M5 — Advanced / exploratory

Advanced and experimental capabilities, each standing alone. Where an item overlaps the study
vocabulary introduced in M2.5, M2.5 provides the abstraction and M5 provides the depth.

- [ ] Parameter sweeps and design-of-experiments API (N jobs, one submission) (#21) — *builds on
      the M2.5 study abstraction rather than introducing a second one: DOE designs, fan-out
      through the execution backend, and the HTTP surface for sweeps.*
- [ ] Optimization loop hooks (dolfinx-adjoint / scipy.optimize driving the geometry params) (#22)
      — *stays here: M2.5 explicitly does not ship an optimizer, and where the study service ends
      and an optimization service begins is an open question (see
      [docs/07-local-agent-interface.md](07-local-agent-interface.md)).*
- [ ] Offline/degraded mode: scikit-fem under Pyodide behind the same JS SDK interface (#23)
- [ ] Sandboxed arbitrary-UFL mode (explicitly opt-in; see security posture in architecture
      doc) (#24)
- [ ] 3D geometry input (STEP upload, OpenCascade.js editor widget) (#25)

## Non-goals

- Reimplementing a CAD kernel or a mesher — Gmsh/OpenCascade do this.
- Competing with ParaView for post-processing depth; the viewer targets *embedded app* use cases.
- A hosted SaaS. Fenix Spoon is a toolkit; hosting it is the user's business (or a future separate
  project).
- Arbitrary execution on behalf of a client or an agent: no `run_python`, `execute_shell`,
  `run_ufl`, `install_package` or `start_container` operation, on any transport. Sandboxed
  arbitrary UFL remains a separate, opt-in M5 experiment (#24). The full list of local-interface
  non-goals is in [docs/07-local-agent-interface.md](07-local-agent-interface.md).
