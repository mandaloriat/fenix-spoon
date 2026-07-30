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

- [x] **Transport-neutral application core.** `fenixspoon/core/` holds the capability catalog,
      job operations, identity and quotas; `api.py` is an adapter, and the fourteen
      `HTTPException`s became one status table. The `/api/v1` contract is unchanged — the existing
      API suite passed without edits. A test imports the core in a subprocess and fails if FastAPI
      is reachable, which caught a real leak (`core.service` → `auth` → `fastapi`) that no
      in-process test could see. The workspace, result-query and study services land with #44,
      #46 and #48. (#42)
- [x] **Progressive capability discovery.** `environment.inspect`, `capability.list`,
      `capability.describe` with eight selectable sections, in `core/discovery.py` and bound to
      HTTP as protocol 1.2 — nine since #70 added `assumptions`. `GET /solvers` keeps its
      payload exactly — it is the right answer for a form generator — and `capability.list` is
      the compact one beside it: 0.5 kB against 4.0 kB on the three mock adapters. An
      unrequested section is *absent*, not null, and an unknown section name is refused rather
      than ignored, because a caller that misspells `metrics` and gets a payload without them
      would conclude the capability has none. Solver adapters gained a declaration — physics,
      availability, metrics, assumptions, artifacts, features, requirements, examples — all
      defaulted, so an adapter written against 1.1 keeps working. *Two things the implementation
      had to be honest about: **metrics are declared, not computed** (the values are #46), so a
      metric that reduces a result field names the field and a test runs a solve and checks it
      is really emitted; and the flattened `params` list is **flatter, not smaller** — measured,
      marginally larger than the schema, and what it buys is that no `$ref` has to be resolved.*
      (#43)
- [x] **Local workspace and object references.** Six object types under stable ids
      (`geometry:g-1`, `design:d-18`), versioned rather than mutated, with `workspace.open`,
      `workspace.list`, `object.create`, `object.get`, `object.patch` and a `job.submit` that
      takes a design reference. A job records the exact revisions it ran on, so the
      design → job → result relation is written down at the one moment it is knowable.
      Four decisions the issue asked to be *recorded*, each with a test that fails if it is
      quietly reversed:
      *Storage is **JSON files** under the data directory, not rows.* The criterion in the
      design draft was whether a workspace is meant to be committed to a repository, and the
      draft's own use cases say yes ("a repository of designs is re-solved on every solver
      change") — so a geometry edit has to show up in a pull request as the two coordinates
      that moved, which `git diff` on a SQLite file cannot do. Still one directory to mount
      and back up.
      *Patches are **JSON Patch (RFC 6902)**.* Decided on the case the issue named: moving one
      control point. Merge patch replaces arrays wholesale, so the same edit resends every
      point — the exact cost this milestone exists to remove.
      *Objects are **not swept by `FENIXSPOON_JOB_TTL`**.* A job is a computation and losing it
      costs a re-run; an object is authored input. The consequence is the good direction — the
      workspace outlives the results computed from it, and keeps enough to recompute.
      *`boundary_condition`, `load_case` and `study` stay **thin and unvalidated**.* No shipped
      solver has a boundary condition separable from its params, so a generic schema today
      would generalise from zero examples. Said out loud rather than invented.
      *Scope: core only, no HTTP routes.* The workspace's first transport is #45; binding it to
      HTTP now would mean designing an object API that JSON-RPC may want differently, so
      `/api/v1` is unchanged apart from `environment.inspect` gaining the workspace path #43
      had specified and could not yet fill in. (#44)
- [x] **JSON-RPC 2.0 over stdio.** `fenix-spoon rpc --stdio`, twenty-four methods over the same
      core the HTTP API uses, with no port opened and no FastAPI imported. Documented in
      [JSON-RPC over stdio](08-json-rpc.md).
      *Framing: **newline-delimited JSON written, either accepted**.* The issue left this open
      and asked it be decided on whether a payload can contain a raw newline. It cannot, and
      that is a property of the encoder rather than a hope — ASCII escaping also escapes
      U+2028/U+2029, so one frame is one line under any reader's definition of a line, not just
      Python's. MCP's stdio transport is newline-delimited too, and #49 is meant to be a thin
      layer over this rather than a re-framing of it.
      *Batches are **refused, by name**.* On a channel that already accepts pipelined requests a
      batch buys nothing but questions the spec does not settle — ordering, atomicity, partial
      failure — and inventing those answers would make them ours.
      *Params are **by name only**.* Positional binding across methods with five optional
      arguments turns "I omitted `sections`" into "I passed `sections` where `inline_schemas`
      goes".
      *Progress is **pollable and streamable**.* `job.events` reads the stored, sequence-numbered
      log from `since`; `job.subscribe` pushes `job.progress` notifications. An agent that does
      not want twenty ticks in its context polls.
      *Errors mirror the HTTP table.* Two errors sharing a status share a code, asserted by a
      test — so a caller writing one handler for both transports is not relying on a coincidence.
      *One rule moved out of the routes.* "An unrequested section is absent, not null" lived only
      in `response_model_exclude_none=True` on two FastAPI routes; a second transport would have
      had to remember it. It is now `Selective.wire()` on the models, with a conformance test
      comparing the two renderings byte for byte. (#45)
- [x] **Compact results: metrics, diagnostics and selective field queries.** Five response
      levels in `core/results.py`, bound to HTTP as protocol 1.3. The default omits `fields`, so
      a finished solve answers in **686 bytes against 529 kB** for the full payload — and the
      compact levels never open the payload file at all, because metrics and diagnostics are
      database columns. `result.query` adds the nine bounded operations over
      `fenixspoon/fields.py`: max/min with location, area-weighted mean, integral, interpolated
      point value, region statistics, section, decimated sample, and clustered hotspots.
      *`t_max` and `t_rise` moved out of `stats` into `metrics`* — they were never costs, and
      #43 had already written down that this is where the conflation gets undone. Diagnostics
      grew out of `stats` rather than beside it, gaining the three things a `dict[str, float]`
      could not hold: a convergence flag, a residual, and warnings.
      *Two things worth recording. A metric declared as a reduction of a field is computed
      **generically** from the #43 declaration, so four adapters do not each write the same
      `float(T.max())`; only the derived ones (`t_rise` needs `t_ambient`, `cp_min` needs
      `u_inf`) are adapter code. And writing the closed-form test for `integral` found a real
      10% bias — summing grid points and multiplying by the cell area over-counts by
      `(n/(n-1))²`, because a lattice has one more point per axis than it has intervals. It is
      trapezoidal now, and exact for linear fields.* (#46)
- [x] **Content-addressed cache and provenance.** A solve's identity is a 128-bit digest over
      its solver, that adapter's declared `version`, the *validated* geometry and params, and the
      versions of the packages it depends on. Validated rather than as-submitted is what makes it
      hit at all: an omitted default and an explicit one are different JSON and the same solve.
      An identical resubmission returns the job that already ran — a lookup, not a copy — and
      `provenance.cached` says so. A `queued` or `running` match is a hit too, so two identical
      submissions attach to one solve instead of racing.
      *Caching is **opt-in per adapter**: `Solver.deterministic` defaults to false, because
      serving a cached answer for a solver that does not reproduce is a wrong answer delivered
      fast, while a missed hit is merely a solve. `environment.inspect` lists which capabilities
      qualify, since "why did my resubmission not hit" is otherwise buried in an adapter's source.*
      *Retention answered by construction rather than by policy: **a cache entry is its job**, so
      the job TTL is the only lifetime and sweeping a job simply makes the next identical
      submission recompute. Nothing dangles.*
      *A hit costs no quota, because it costs no compute — which is why the quota and history
      tests had to start submitting genuinely different work; nine of them were counting one job
      several times.*
      *The `design → job → result` relation is queryable from either end: provenance names the
      pinned object revisions, and `jobs_for_object` reads it backwards. Study joins the chain
      when #48 writes one; nothing here needs to change for it to.* (#47)
- [x] **Study abstraction.** A `study` object — kind, base design, the parameter to vary, the
      ladder of values, a tolerance — plus `study.run` and `study.get`. **One kind,
      `mesh_convergence`**, which is what the issue asked for: enough to prove several jobs can
      be orchestrated through object references and compact results, not a framework with one
      user. `study.get` returns the (variation → metric) table *and* the sentence it is for —
      the value from which every later change stays under the tolerance.
      *A study may override a design's parameters where `job.submit` may not*, and that is not
      an exception to #44's rule but the same rule: the override is `values[i]` applied to
      `parameter`, both frozen in the study revision, so a rung's parameters are a pure function
      of *(study revision, rung index)* — as reproducible as a design revision, without filling
      a design's history with machine-generated revisions nobody authored.
      *The study/optimizer boundary is settled with a property rather than a description* — a
      study's job list is a pure function of its object revision, so you can say which solves it
      implies without running any. An optimizer cannot promise that. Recorded in
      [§15](07-local-agent-interface.md#15-open-questions).
      *There is no per-run record.* The study is the question, the jobs are the answer, and the
      relation is queried the way #47 made `design → job → result` readable backwards. A rung
      resolves to its job by **cache key** — which is also how a rung answered by a standalone
      solve from last week is found, something no `inputs` lookup could do.
      *The one guard that earns its keep*: a solver's `Params` ignores unknown fields, so a
      study varying `resolutoin` would submit every rung identically, the cache would collapse
      them onto one job, and the table would show a perfectly converged answer that is entirely
      fabricated. A parameter the capability does not have is refused. (#48)
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

### Reported back from the application side

Three gaps that a consumer of M2.5 found by trying to build on it, filed against this repository
from [`physics-lab`](https://github.com/mandaloriat/physics-lab), and all three protocol 1.4:

- [x] **Circulation and lift on the potential-flow adapters.** The streamfunction solve carried
      whatever circulation the body's arbitrary streamline constant implied, so the most famous
      output of the model was meaningless and nothing said so — which is why #21 and #22 had both
      written *"lift **proxy**"* into their acceptance criteria. The problem is linear in that
      constant, so the fix is a superposition rather than a new solver: solve once with the free
      stream and the body at zero, once with the body at one, and the Kutta condition picks the
      combination in a single division. `circulation`, `c_l`, `c_m_c4` and `x_cp` now come back
      from both adapters, and there is a lift to optimise. *Verified against closed forms rather
      than against itself: circulation to 0.006% on a lifting cylinder, `Gamma = 4 pi U R
      sin(alpha + beta)` to 0.1% on the Joukowski condition, exactly zero drag (d'Alembert), and
      a NACA 2412 zero-lift angle of -2.34° against the tabulated -2.1°. The two independent
      routes to lift — Kutta-Joukowski and an integral of the surface pressure — are compared on
      every run and a disagreement is reported rather than averaged.* (#68)
- [x] **A result kind for 1-D data.** Both existing kinds were fields over a 2-D domain, and a
      great deal of engineering output is a curve: a surface `C_p`, a sweep, a convergence
      history. `series1d` is the third member of the union, with a `series` key so a field result
      can carry curves beside it — one solve legitimately answers both. Bounded three ways, so a
      curve cannot become the field arrays under a different key, and outside the default
      response level for exactly that reason. *No curve widget yet: the protocol carries the
      shape and each consumer still draws it.* (#69)
- [x] **Declared assumptions.** #43 gave `capability.describe` eight sections and every one
      described what a capability *does*. None said what its model *assumes*, which is what a
      caller needs before trusting a number. `excludes` is the field that earns it: a caller
      asking *"can this tell me about drag?"* gets a definite no rather than a plausible zero.
      Where an assumption has a numeric edge it carries the limit, so the honest sentence that
      was buried in `mock.magnetostatics2d`'s `b_max` description — *"past roughly 1.5 T the
      permeability collapses and a linear solve stops describing the device"* — is now a number a
      caller can act on. (#70)

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
- [x] Vector fields on the wire and in the viewer (#62) — *protocol 1.1 adds `vector_fields` to
      `grid2d` and `point_vector_fields` to `mesh2d`, indexed like the scalar maps, so a velocity
      is one named thing rather than two conventions apart. `<fs-viewer vectors="velocity">` draws
      arrow glyphs on a lattice sized by the `glyphs` attribute rather than by the data, because
      one arrow per grid point is unreadable at 512×341 and sparse at 16×16. The first additive
      bump under the rule #58 wrote down.*
- [x] An explorable viewer: viewport navigation, generic overlays and capability-driven tools
      — *`<fs-viewer>` stopped being a renderer and became something an application can offer
      as a preview: pointer-anchored zoom, drag and pinch pan, fit-domain and fit-geometry, a
      probe and a section sampled from the arrays already in the page, a pinnable colour scale
      for comparing two solves, configurable vector density and scale, and integral curves of a
      vector field with adjustable seeding. **No protocol change**: all of it is a function of
      data `grid2d` and `mesh2d` have carried since 1.1, which is the decision recorded in
      [ADR 0001](adr/0001-explorable-viewer.md) along with the three things the viewer refuses
      to infer — a vector field from a scalar one, a geometry outline from the `grid2d` mask,
      and a physical name for an integrated curve. Navigation is opt-in and the gesture meaning
      is an explicit `mode`, so the airfoil demo's layered geometry editor keeps working and
      can now coordinate with the viewer rather than merely sit on top of it.*
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
