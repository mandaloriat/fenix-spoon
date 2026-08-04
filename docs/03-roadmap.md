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

## M2.5 — Local automation and agent interface ✅

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
      would generalise from zero examples. Said out loud rather than invented. (Two of the
      three have since left that list, each when a capability arrived that needed it: `study`
      in #48, `load_case` in #85. `boundary_condition` is still thin — and now the type with
      no user, since a load case is what it was reaching for.)
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
      user. *The second kind arrived with #21 under M5 below, and cost no new operation — which
      is the claim this entry was making and the diff that had to answer for it.* `study.get` returns the (variation → metric) table *and* the sentence it is for —
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
- [x] **MCP adapter.** Thirteen tools over the same core, as one file (`mcp_adapter.py`) with
      no application logic in it — the issue's own test being that if MCP is replaced in two
      years, one file is deleted. Documented in [MCP adapter](09-mcp.md).
      *It binds to the **JSON-RPC method table**, not to the core.* Two adapters calling the
      core independently is two places for one operation to be spelled slightly differently,
      which is the divergence #51 exists to find. Here a tool *is* an RPC method plus a
      schema, so "the same request over MCP and over JSON-RPC produces the same result" holds
      by construction, and MCP inherits the parameter typing, error mapping and compact-answer
      rules for free.
      *Fifteen tools, not twenty-eight.* A host's tool list is read every turn, so this is the
      one surface where exposing everything is actively wrong. `job.list`, `job.for_object`,
      `object.revisions` and `design.resolve` stay JSON-RPC-only; `job.cancel` is absent
      because a host has no stable handle on a job from a previous turn; `job.subscribe` has
      no meaning without a connection to push into.
      *No tool per solver or per physics*, asserted against the **installed** capabilities
      rather than a hard-coded list — so writing `solve_magnetostatics` fails the build.
      *An optional extra, and the suite proves it both ways*: 527 tests pass with `mcp`
      installed, 509 pass with it uninstalled, and a subprocess test fails if importing the
      server ever pulls MCP in.
      *Artifacts ship as resources **and** paths, and the open question stays open.* The
      design draft asks for it to be settled against real host behaviour, which a test suite
      cannot produce. One sub-case is settled on grounds that need no host: a large artifact
      is described, not base64-encoded — base64 makes a file a third larger and lands it in a
      context window that cannot use it. (#49)
- [x] **CLI and Python adapters.** `fenix-spoon <noun> <verb>` for shells and CI, and
      `fenixspoon.local` for notebooks and scripts. Documented in
      [CLI and Python API](10-cli-and-python.md).
      *The CLI dispatches through the **JSON-RPC method table***, like the MCP adapter, so
      `--json` is not comparable to what an agent gets — it is the same bytes, asserted by a
      test. That is what makes the CLI worth using as the debugging surface for what an agent
      sees; one that reformatted anything would be showing something the protocol does not
      say. Human output is a *rendering* of that JSON, never a different answer.
      *`job submit` waits by default*, and that is the only behaviour that is not broken: with
      the in-process backend a command that submitted and exited would kill the solve it just
      started and leave a row saying `running` that the next startup reconciles to failed.
      `--detach` exists for a worker backend and says plainly what it costs without one.
      *Exit codes come from the JSON-RPC codes* — invalid request, not found, conflict, quota,
      gone — so the distinction a shell sees is the distinction an agent sees.
      *The Python API owns one event loop on a background thread.* A coroutine-per-method API
      would make "solve something and print a number" require `asyncio.run`; a per-call
      `asyncio.run` is the trap the test suites hit three times, because the in-process
      backend completes a solve on the loop that submitted it — so it would work for
      everything except finishing a job. One loop makes submit-now-wait-later, the notebook
      shape, an ordinary blocking call. (#50)
- [x] **Cross-transport conformance and the vertical slice.** The milestone's exit criterion,
      and the guard that keeps five adapters from drifting.
      *The corpus grew a `protocol/fixtures/errors.json`* naming every domain error and how each
      transport represents it. Until it existed the transports agreed on the strength of
      *per-pair* tests, which is a shape that decays quietly: add an error, map it on one
      transport, and the others stay green and wrong. The fixture is the source of truth for the
      **partition** — which errors are alike — with three tables asserted against it and one test
      that fails when the corpus and the code disagree about which errors exist.
      *The two non-pydantic refusals are checked by name*, as the issue asks: a cell-budget and a
      quota refusal must carry the same structure as one a validator produced, because a caller
      cannot tell where a refusal was computed and should not have to.
      *One request, five renderings* — HTTP, JSON-RPC, MCP, the CLI as a subprocess, and the
      Python API — compared as payloads rather than by inspecting the call, because "it calls the
      same function" is what the implementation says and the payload is what a caller sees.
      ***The compact-answer size budgets finally have the assertion they were approximating.***
      Those ceilings moved twice in one day — once for four more declared metrics, once for a
      solver warning — and both moves were correct, because a byte count cannot tell "this
      capability reports more" from "somebody put the nodal data back". `assert_no_numeric_arrays`
      walks a payload and fails on any run of numbers longer than 32, wherever it hides; it is
      checked against itself first, since a walker that quietly matched nothing would pass every
      test while checking nothing.
      *The vertical slice runs in both environments*, parametrised over `mock.laplace2d` and
      `dolfinx.potential_flow2d`: discover, design, solve, read progress, query metrics, fetch the
      VTK by path, patch one control point, re-solve. The assertion that earns its place is the
      *negative* one — the patched geometry must **not** hit the cache, because a hit there would
      serve the old airfoil's numbers for the new one. "No HTTP server started" is checked by
      running the whole loop in a subprocess that fails if FastAPI was imported. (#51)

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
      response level for exactly that reason. *The curve widget that was missing here has since
      shipped as `@fenix-spoon/plot` — see M4 below.* (#69)
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

## More physics, and what each one asked of the protocol

**Not a milestone — a thread that runs alongside them**, and the reason it has a section is that
it kept producing protocol changes that nothing in M0–M5 predicted. The three items reported back
from `physics-lab` above were gaps a *consumer* found by building on the toolkit. These are gaps a
**new capability** found by being added to it, which is the other direction and, so far, the more
productive one.

The order is the load-bearing part. Every extension below answers a solver that already wanted it,
rather than a specification written forward and hoped for — which is why each entry names the
capability first and the protocol change second.

- [x] **Linear elasticity: a vector unknown, and where the boundary conditions go** (#81) —
      `mock.elasticity2d` and `dolfinx.elasticity2d`, validated against the Kirsch stress
      concentration around a circular hole and a cantilever's `PL³/3EI` tip deflection. The first
      result whose unknown is a vector, and the first capability whose conditions the geometry
      could not express: `fixed_edge` / `load_edge` are params naming an axis-aligned edge, which
      is a shorthand and is declared as one. *Nodal stress is area-weighted in one shared helper,
      so the two halves of the pair cannot average differently* — the same reasoning that later
      put boundary resolution in one place. The thing it could not do became #85.
- [x] **Transient heat: answering "when", and finding out what the protocol cannot say** (#82) —
      `mock.transient_heat2d` and `dolfinx.transient_heat2d`, with the mean and the time constant
      volume-weighted on the unstructured mesh. The capability landed and immediately opened the
      two entries below: a solve with a history had nowhere to put it, and `t_max` over a
      transient meant something `stats` and `metrics` between them could not distinguish.
- [x] **A metric declares what it is taken over, and the combination that lies is refused**
      (#86, protocol 1.6) — `MetricSpec.over` is `payload` (the default, and what every steady
      capability already meant) or `run`, for a quantity only the adapter can supply: a peak over
      a history, a time to reach a level. *The refusal is the point.* A `run` metric declared as a
      reduction of the payload would be filled in generically by #46's machinery from the final
      frame and reported under a name promising the whole run — a wrong number with a
      correct-looking label, which is worse than an absent one. Additive, and a discovery-payload
      change like 1.2's, so the SDK carries the version and models nothing new.
- [x] **The time index: an artifact knows which instant it holds** (#86, protocol 1.7) — an
      optional `t` on an artifact, and a derived `frames` list on the result envelope. The time
      index of a transient *is* the artifacts carrying an instant, in time order.
      *Deliberately not a new result kind*: the field history crosses as references like every
      other large thing, and there is no server-side reduction at a chosen instant. Deriving the
      index from the files rather than storing it beside them is what makes a frame naming a file
      the result does not serve **unrepresentable** rather than merely tested for. The cap is
      enforced where an artifact is registered, not where an envelope is built, because the
      compact levels and the local API never build one — and a cap half the callers can walk past
      is not a cap. Unlike 1.6 the SDK models this one: leaving it out would have made the version
      constant advertise a shape the types denied.
- [x] **A geometry names pieces of its own boundary** (#85 first half, protocol 1.8) — optional
      stable `point_ids` on `polygon2d`, and a `boundaries` list. Three selector families, because
      they follow different things: `part` follows the **topology** and needed no invention
      (`outer`, `obstacle`, `region:<name>` are what the shipped adapters already assume),
      `points` follows the **shape** through an edit, and `near`/`box` follow the **space**.
      *Indices could not do the second*: insert a control point and every index after it shifts,
      moving a named boundary onto a different edge with nothing to notice. `all_of` intersects,
      and is a closed set rather than an expression language — that is the UFL-over-the-wire
      argument again in miniature. Resolution produces a **predicate over coordinates**,
      `f(x) -> bool` over points shaped `(2, N)`, which is exactly what `locate_entities_boundary`
      takes: one resolution, two consumers, no chance of the pair disagreeing about which edge was
      meant. *A `points` selector naming real vertices that span no edge is refused* — a boundary
      that validates and then matches nothing is precisely the failure the design exists to
      prevent — and it is refused through the same `spanned_edges` the resolver uses, so the check
      and the resolution cannot drift apart.
- [x] **A load case says what happens there** (#85 second half, protocol 1.9) — a fourth
      workspace object beside `geometry`, `material` and `design`, because an engineer reuses one
      set of restraints and loads across a family of shapes, and that reuse is the whole argument
      for it not being a block inside the design. Values stay an open dict of scalars like
      `Region2D.material`, so a new physics is not a protocol change; what an adapter
      **declares** is the condition keys it reads.
      ***The asymmetry with a material key is the release.*** A material key a solver does not
      read leaves a property at its default and the answer is merely computed with it. A
      condition a solver does not read leaves a clamp out of the assembly, and the solve
      converges and answers a different problem with no symptom. Hence three refusals, all at
      submit and all in the shared error corpus: `UnknownBoundary`, `UnknownConditionKey` and
      `ConflictingConditions` for two of a design's load cases setting one key on one boundary.
      The conditions go into the cache key, because two designs differing only in what is
      clamped are two different solves — leaving them out would have served the clamped answer
      to the caller who asked about the free one.
      *Precedence between a load case and the `fixed_edge`/`load_edge` shorthand is **total,
      never a merge***: a caller who named every boundary must not inherit an invisible clamp
      from a default it never set. The FEniCSx half builds facet tags from the same predicate
      the mock consumes as a NumPy mask, which is what the `f(x) -> bool` shape was chosen for —
      and writing it found a real bug in the 1.8 resolver, which did two-row arithmetic on the
      `(3, N)` coordinates dolfinx passes. Verified in the FEniCSx CI job: the load case
      reproduces the edge shorthand to 1e-9, and a plate hangs from its own hole.

**Where this thread leaves the open questions.** #44 said `boundary_condition` and `load_case`
would stay thin "until a capability needs them", and elasticity is the capability that did —
so `load_case` acquired a body the same way `study` did at #48, by having something concrete to
generalise from. The honest reading of #85 is that a load case is what a boundary condition was
reaching for: `boundary_condition` is now the type with no user, still thin, still waiting on a
capability rather than on an argument.

## M3 — Production job execution

Goal: multi-user deployments are safe and boring. *Everything below has landed except the Helm
chart, which is deliberately unwritten — see #15.*

Scope note: M3 is the *multi-user, distributed* axis — queue, separate workers, Redis,
server-side persistence, authentication, per-user quotas, object storage, deployment and load
testing. M2.5 required none of it at runtime and did not grow a second job system: the job
service it extracted wraps the same `ExecutionBackend` and `JobStore` this milestone built.

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
      demo page builds its controls from `params_schema`.* A fourth joined them later and is
      credited to #21 below: a lift polar, the first page whose answer is a curve rather than
      a field, and the first that keeps its inputs on the server under stable ids.
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
- [x] A curve widget — `@fenix-spoon/plot`, the fourth browser package. *The one extension the
      wire-protocol document had listed as unfinished since 1.5: the protocol carried curves and
      every consumer wrote its own plot. **No protocol change**, exactly like the explorable
      viewer above — it draws numbers that have been on the wire since 1.5. A separate package
      rather than a second element in the viewer, because the two share a canvas and nothing
      else: colormaps, viewports and contour extraction do not help draw an axis, and folding
      them together would make every page showing a temperature map carry plot code it never
      calls.*
      *`invert-y` is an attribute and not an inference*, on ADR 0001's grounds — noticing a trace
      called `cp_upper` and flipping the axis would be reading a quantity off a name.
      *Two things writing it found.* A guard against a zero-width domain in the projection was
      unreachable, because the domain repair upstream already prevents one — two defences that
      could disagree, so one went. And the accessible description was being skipped whenever the
      canvas had no 2D context, since it sat after the early return: the name of an element is a
      function of its data, not of whether the environment can rasterise.
      *Wired into the airfoil demo*, which had been computing a surface `C_p` since #68 and
      throwing it away. Its saw-toothed appearance is the mock's staircased body, not the plot —
      said on the page, because a first-time visitor would otherwise read it as a broken widget.
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

- [x] Parameter sweeps and design of experiments — the `sweep` study kind (#21, first half).
      *Built on the
      M2.5 study abstraction rather than beside it, which #48 wrote down as its purpose and
      which the diff now has to answer for: **no new operation, no new transport binding, no
      second job path, and the mesh ladder untouched.** `study.run` and `study.get` answer both
      kinds over JSON-RPC, the CLI, Python and MCP alike, because #48 defined a study rather
      than a mesh ladder.*
      *A sweep takes a grid (`axes`, full factorial, last axis fastest) or explicit `points`.
      Generating a Latin hypercube here was refused for a reason worth keeping: **a randomized
      design generated server-side breaks the property that defines a study** — that its job
      list is a pure function of its object revision — unless the seed is frozen in the body,
      at which point the caller is specifying the design anyway. So the caller generates and
      the revision freezes.*
      ***The answer is a curve, and the protocol has carried curves since 1.5.*** Response
      curves come back as `Series1DData`, the model a `series1d` result uses, so anything that
      draws a curve draws these — no parallel "response" shape, one of which would have been
      undrawable. Each trace brings its own abscissa, because a point with no answer has no
      legal encoding against a shared one: it would become a zero or shift every later value
      onto the wrong angle.
      *Acceptance: a **lift polar**, not the camber sweep the issue asked for. Camber is a
      property of the geometry and the workspace stores geometries as explicit polygons, so a
      camber sweep is a sweep over geometry references; `alpha` rotates the free stream and is
      a capability parameter, which the adapter's own description had already pointed out is
      what lets a sweep reuse one domain. "Lift **proxy**" is gone too — #68 made `c_l` real.
      The test asserts the physics rather than a golden number: potential-flow lift is linear
      in alpha, so equal steps in angle give equal steps in `c_l` — which also catches every
      point collapsing onto one cached job, the failure a sweep is uniquely exposed to.*
      *Two things found by running it rather than by testing it.* A sweep of two named metrics
      printed a twelve-column table of six: `metrics` was documented as the column list, the
      read-out honoured it and the rows never had — invisible on a ladder, obvious the moment
      the table *is* the answer. And the CLI's generic table dropped every map-valued column,
      so a polar rendered as six rows of job ids with neither the angles nor the lift; a column
      of flat maps now spreads into columns, which is still generic and is the difference
      between a table and a header.
- [x] **The workspace over HTTP** — protocol 1.10, designed in
      [ADR 0002](adr/0002-workspace-over-http.md) and built from it. *The blocker both M5
      items were waiting on, and the question #44 deferred twice.* Objects as a resource
      collection under `/objects/{type}/{id}` with the revision as a query parameter, and
      `POST /jobs` accepting a design reference — which is the actual payoff, because a
      browser that inlines its geometry can never hit the cache on an unchanged design nor
      say which revision a picture came from.
      *The design pass happened first, on its own, and paid for itself twice.* One review
      round removed the most expensive decision in it — a deliberate divergence between
      transports, dissolved once `job.submit`'s existing "waiting is a convenience, not an
      operation" pattern was noticed — and writing the code corrected the record again: a URL
      whose halves disagree is a 422 from the reference parser, not the 404 the record
      promised, because `parse_ref` has refused a type/prefix mismatch since #44.
      *Two findings the design pass made by reading rather than reasoning.* Per-principal
      isolation on objects was already written and already correct, so the deliverable was
      not to build it but to make it falsifiable — it had never been under adversarial
      pressure on a single-principal machine, and now has a negative test per verb. And
      nothing metered object creation: `FENIXSPOON_MAX_OBJECTS` joins the other three quotas,
      with no `Retry-After`, because waiting does not delete an object.
- [x] A sweep from a browser (#21, second half) — *protocol 1.11, and not the shape the issue
      sketched.* It asked for `POST /api/v1/sweeps` with the grid in the body; ADR 0002
      refused that — a sweep posted as a body is a computation with no identity, which cannot
      be pinned, re-read, re-run into the cache or shown to a colleague. A study is an
      **object that runs**: create it like any other, then `POST /studies/{id}/run` and
      `GET /studies/{id}`, neither taking a request body because the study already says
      everything the run needs.
      *No comparison widget was needed after all*, which is the payoff of a decision taken a
      release earlier: a sweep answers with `Series1DData`, so `<fs-plot>` draws a study
      report unmodified and the SDK typed it without an addition. The deliverable that
      remained was a page — [a lift polar](gallery.md#lift-polar-a-parameter-sweep-driven-from-the-page),
      the first demo that keeps its geometry, design and study on the server under stable ids.
      *And the page taught the sweep something a test would not have.* Written with a
      point-count control it reused nothing when the range widened, because N points between
      two ends move every angle by a fraction of a degree and a fraction of a degree is a
      different question. Anchoring the grid to a **step** from `from` turned a seven-solve
      widening into a one-solve one. The protocol was right; the caller was wrong; only
      driving it in a browser showed which.
      *Two open questions ADR 0002 named, one of them answered by not being a question.* A
      `study.run` on a study already running is idempotent, and nothing was added to make it
      so — the result cache has matched `queued` and `running` jobs since #47, which is what
      stops two identical submissions from racing.
- [x] Optimization loop hooks — the `optimization` object and a bounded scalar search
      (#22, first half). *The far side of the line #48 drew, and the seventh workspace object
      type. A study **enumerates** a variation space; this **searches** one, choosing its next
      point from what the last one answered — so `optimize.run`/`optimize.get` are separate
      operations rather than a third study kind, and the vocabulary says which is which.*
      ***It still has no run record, and the distinction that makes that work is worth
      keeping.*** An optimizer is not *predictable* — nothing can enumerate its points in
      advance — but it is **reproducible**, and reproducibility is the property storage would
      have been for. The method is a pure function from the answers so far to the next point,
      so a second run replays the identical sequence and every evaluation is a
      content-addressed cache hit; `optimize.get` recovers the trajectory by replaying it and
      resolving each point by cache key, which is the study's two-path lookup unchanged. Where
      the objective is not reproducible the recorded `inputs` answer instead — what is lost is
      the free second run, not the record.
      *Ask–tell rather than a callback, which is also the answer to "why not `scipy`".*
      `minimize_scalar` takes a function and calls it, which inverts control against a job
      service that is asynchronous by construction: satisfying it means blocking a thread per
      search, and it puts the trajectory inside somebody else's stack frame where neither
      `optimize.get` nor a caller polling mid-run can see it. The method here never touches a
      solver, a job or a workspace — it is tested against arithmetic, with no fixture — and
      that seam is what a gradient method or the adjoint half would implement rather than
      replace.
      *Acceptance: the **zero-lift angle** of a cambered section, checked against the sweep
      rather than against a constant.* #22 asked for camber and a lift proxy; camber is a
      property of the geometry, which the workspace stores as an explicit polygon, and the
      proxy became a real `c_l` with #68 — the same two corrections #21 made. So the search
      moves `alpha` to hit `c_l = 0`, and the test requires the sweep's polar to change sign
      **inside the bracket the search reports**: two computations of one answer, one
      tabulating and one choosing, agreeing where they overlap. Eleven solves from a 20° range.
      *Three things the shape decides.* An evaluation with no answer **stops** the search
      rather than leaving a gap, because the next point is a function of the missing value —
      the sharpest difference from a study, which would tabulate the rest. The report carries
      a **bracket** beside the best point, because "where the lowest value was seen" and
      "where the minimum is known to be" are different claims and a budget-stopped search
      shows the gap. ~~And `optimize.run` is the one operation whose duration *is* the work, so
      it has no `--detach`: there is no moment at which a search is accepted and not yet
      done.~~ **That last sentence was wrong and is struck rather than deleted.** There is such
      a moment — the one `job.submit` and `study.run` return at — and ADR 0002 decision 4 found
      it by asking what `optimize.run` would do over HTTP, which cannot hold a response open
      for minutes of solving. Protocol 1.12 makes it a receipt everywhere, `--detach` and all;
      the waiting moved to the CLI and the Python API, where it always belonged.
- [ ] Optimization: several parameters, gradients, and watching it (#22, second half) —
      multi-parameter search, dolfinx-adjoint for gradients where an adapter can supply them,
      per-iteration progress on the event channel, and the browser view that watches the shape
      morph. *The HTTP surface the last of those was waiting on landed with protocol 1.12
      (ADR 0002 decision 4): `POST /optimizations/{id}/run` returns a receipt and
      `GET /optimizations/{id}` grows a row per evaluation, so a page can already poll a search
      to a stop. What is left here is the view itself, per-iteration progress on the **event**
      channel rather than by polling, and `next_point` implemented again rather than changed —
      several parameters and a gradient are different methods, not a different loop.*
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
