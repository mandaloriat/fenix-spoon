# Roadmap

Milestones are ordered so that every one ends with something demonstrable. Every unchecked item
is tracked as a GitHub issue, and each milestone has a tracking issue with the suggested order:
[M1](https://github.com/mandaloriat/fenix-spoon/issues/26) ·
[M2](https://github.com/mandaloriat/fenix-spoon/issues/27) ·
[M3](https://github.com/mandaloriat/fenix-spoon/issues/28) ·
[M4](https://github.com/mandaloriat/fenix-spoon/issues/29) ·
[M5](https://github.com/mandaloriat/fenix-spoon/issues/30)

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
- [ ] Second physics example: 2D magnetostatics of a solenoid cross-section (axisymmetric A-φ),
      exercising material regions in the geometry schema (#5)
- [ ] Mesh-size/quality parameters exposed through solver params, with server-side caps (#6)
      — *partially done: wall-clock timeout + cancellation shipped; cell-count caps pending.*

## M2 — Embeddable client widgets (`client/` becomes real)

Goal: `npm install` two widgets and build the demo page in ten lines.

- [ ] `@fenix-spoon/client` — typed JS/TS SDK for the wire protocol (fetch + WS, reconnection) (#7)
- [ ] `@fenix-spoon/geometry-2d` — framework-agnostic (custom element) parametric 2D profile
      editor: draggable points, splines, constraints, undo, JSON in/out per the geometry schema (#8)
- [ ] `@fenix-spoon/viewer` — vtk.js-based field viewer custom element: unstructured 2D/3D,
      colormaps, contours, vector glyphs, probes (#9)
- [ ] Rebuild `examples/airfoil-2d` on the widgets; keep the zero-dependency version as
      reference (#10)
- [ ] Versioned protocol conformance tests shared between server (pytest) and SDK (vitest) (#11)

## M3 — Production job execution

Goal: multi-user deployments are safe and boring.

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

- [ ] Parameter sweeps and design-of-experiments API (N jobs, one submission) (#21)
- [ ] Optimization loop hooks (dolfinx-adjoint / scipy.optimize driving the geometry params) (#22)
- [ ] Offline/degraded mode: scikit-fem under Pyodide behind the same JS SDK interface (#23)
- [ ] Sandboxed arbitrary-UFL mode (explicitly opt-in; see security posture in architecture
      doc) (#24)
- [ ] 3D geometry input (STEP upload, OpenCascade.js editor widget) (#25)

## Non-goals

- Reimplementing a CAD kernel or a mesher — Gmsh/OpenCascade do this.
- Competing with ParaView for post-processing depth; the viewer targets *embedded app* use cases.
- A hosted SaaS. Fenix Spoon is a toolkit; hosting it is the user's business (or a future separate
  project).
