# Roadmap

Milestones are ordered so that every one ends with something demonstrable. Every unchecked item
is tracked as a GitHub issue, and each milestone has a tracking issue with the suggested order:
[M1](https://github.com/mandaloriat/fenix-spoon/issues/26) ·
[M2](https://github.com/mandaloriat/fenix-spoon/issues/27) ·
[M2.5](https://github.com/mandaloriat/fenix-spoon/issues/52) ·
[M3](https://github.com/mandaloriat/fenix-spoon/issues/28) ·
[M4](https://github.com/mandaloriat/fenix-spoon/issues/29) ·
[M5](https://github.com/mandaloriat/fenix-spoon/issues/30)

M0–M2 build the browser path: geometry in, job out, field rendered. M2.5 turns the same core into
something a *local* process — a script, a CLI, an agent — can drive without a web application.
M3 takes the web path to production. The two are independent: M2.5 is single-user and in-process,
M3 is multi-user and distributed.

## M0 — Kickstart (this repository state) ✅

Goal: a working vertical slice with zero heavy dependencies, plus the planning documents.

- [x] State-of-the-art survey, architecture, wire protocol v0 draft
- [x] FastAPI server: job submission, WebSocket progress streaming, result retrieval
- [x] Solver adapter protocol + registry
- [x] Mock solver: 2D potential flow (Laplace) around a polygon, pure NumPy
- [x] Browser demo: draggable airfoil editor + live field rendering (no build step)
- [x] Test suite green without FEniCSx; CI workflow
- [x] Docker scaffolding (dolfinx base image + compose)

## M1 — Real FEniCSx path

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
- [ ] Mesh-size/quality parameters exposed through solver params, with server-side caps (#6)
      — *partially done: wall-clock timeout + cancellation shipped; cell-count caps pending.*

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

Scope in one line: everything needed to drive Fenix Spoon from a process on the same machine —
single user, in-process jobs, local workspace, compact answers. Nothing here needs a queue, a
broker, or auth; those stay in M3. The design specification is
[docs/05-local-agent-interface.md](05-local-agent-interface.md), which this milestone implements.

- [ ] **Transport-neutral application core.** Extract from the FastAPI routes a reusable
      application layer — capability catalog, workspace service, object store, job service,
      result query service, study service — and make `api.py` an adapter over it. The HTTP API
      keeps its current behavior, paths and status codes; only the layering moves. (#42)
- [ ] **Progressive capability discovery.** `environment.inspect`, `capability.list`,
      `capability.describe` with section selection (geometries, params, metrics, artifacts, sweep
      / gradient / MPI support, environment requirements). Full JSON Schemas are fetched by
      reference or on request, never dumped on every call. (#43)
- [ ] **Local workspace and object references.** An inspectable, reopenable workspace holding
      `geometry`, `material`, `boundary_condition`, `load_case`, `design`, `study`, `result` and
      `artifact` objects under stable identifiers (`geometry:g-42`, `design:d-18`, `study:s-9`,
      `result:r-105`). Iterations reference objects instead of resending geometry; incremental
      edits go through a standard patch mechanism (JSON Patch is the leading candidate). (#44)
- [ ] **JSON-RPC 2.0 over stdio.** A local transport with no mandatory network port: the agent
      spawns `fenix-spoon rpc --stdio` as a child process and speaks structured messages over its
      pipes — documented framing, typed compact errors, long solves as asynchronous jobs, working
      identically against mock solvers and real FEniCSx. This is the base local transport; MCP
      is layered on it, not the other way round. (#45)
- [ ] **Compact results: metrics, diagnostics and selective field queries.** Separate the response
      levels `status` / `metrics` / `diagnostics` / `fields` / `artifacts`. Agents get scalar
      engineering metrics (mass, maximum displacement, maximum stress, safety factor, strain
      energy, force, inductance, peak temperature) and solve diagnostics by default; full fields
      travel as artifacts by reference, with selective queries for max, min, mean, integral,
      point value, region value, section, decimated sampling and hotspot location. (#46)
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
unchanged objects and reports what it took from cache. The solenoid is an equally acceptable
subject. The milestone is *not* done when JSON-RPC merely answers — it is done when that
iterative loop runs end to end.

## M3 — Production job execution

Goal: multi-user deployments are safe and boring.

Scope note: M3 is the *multi-user, distributed* milestone — queue, separate workers, Redis,
server-side persistence, authentication, per-user quotas, object storage, deployment and load
testing. M2.5 deliberately depends on none of it, and must not grow a second parallel job system:
the job service it extracts is the same interface the Celery/arq backend implements below.

- [ ] Pluggable job backend: Celery or arq implementation (Redis), worker containers with
      dolfinx (#12)
- [ ] Job persistence (SQLite/Postgres) and artifact storage (filesystem/S3-compatible) (#13)
- [ ] Auth hooks (API keys / OIDC middleware), per-user quotas, wall-clock and memory limits (#14)
- [ ] Helm chart / compose profiles for API + workers + Redis (#15)
- [ ] Load test: N concurrent solves with progress streaming (#16)

## M4 — Gallery and docs site

Goal: adoption. People find, run, and copy examples.

- [ ] Docs site (mkdocs-material) with protocol reference generated from pydantic models (#17)
- [ ] Example gallery: airfoil potential flow → incompressible Navier–Stokes; solenoid
      magnetostatics; heat sink; each as a copy-paste-able app (#18)
- [ ] "Deploy to Fly.io/Render/self-host" one-clickish guides (#19)
- [ ] Announce: FEniCS Discourse, r/CFD, Hacker News (#20)

## M5 — Advanced / exploratory

Advanced and experimental capabilities, each standing alone. Where an item overlaps the study
vocabulary introduced in M2.5, M2.5 provides the abstraction and M5 provides the depth.

- [ ] Parameter sweeps and design-of-experiments API (N jobs, one submission) (#21) — *builds on
      the M2.5 study abstraction rather than introducing a second one: DOE designs, fan-out
      through the M3 job backend, and the HTTP surface for sweeps.*
- [ ] Optimization loop hooks (dolfinx-adjoint / scipy.optimize driving the geometry params) (#22)
      — *stays here: M2.5 explicitly does not ship an optimizer, and where the study service ends
      and an optimization service begins is an open question (see
      [docs/05-local-agent-interface.md](05-local-agent-interface.md)).*
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
  non-goals is in [docs/05-local-agent-interface.md](05-local-agent-interface.md).
