# Roadmap

Milestones are ordered so that every one ends with something demonstrable. Each unchecked item
should become a GitHub issue when its milestone starts.

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

- [ ] Harden `dolfinx_poisson` adapter: Gmsh meshing of `domain2d` geometry, validated against the
      mock solver on coincident cases
- [ ] CI job that runs the FEniCSx adapter tests inside the `dolfinx/dolfinx` image
- [ ] Result serialization for unstructured meshes: triangles + node values (protocol `mesh2d` kind)
- [ ] Artifact channel: results downloadable as VTU/XDMF; inline JSON kept for small fields
- [ ] Second physics example: 2D magnetostatics of a solenoid cross-section (axisymmetric A-φ),
      exercising material regions in the geometry schema
- [ ] Mesh-size/quality parameters exposed through solver params, with server-side caps

## M2 — Embeddable client widgets (`client/` becomes real)

Goal: `npm install` two widgets and build the demo page in ten lines.

- [ ] `@fenix-spoon/client` — typed JS/TS SDK for the wire protocol (fetch + WS, reconnection)
- [ ] `@fenix-spoon/geometry-2d` — framework-agnostic (custom element) parametric 2D profile
      editor: draggable points, splines, constraints, undo, JSON in/out per the geometry schema
- [ ] `@fenix-spoon/viewer` — vtk.js-based field viewer custom element: unstructured 2D/3D,
      colormaps, contours, vector glyphs, probes
- [ ] Rebuild `examples/airfoil-2d` on the widgets; keep the zero-dependency version as reference
- [ ] Versioned protocol conformance tests shared between server (pytest) and SDK (vitest)

## M3 — Production job execution

Goal: multi-user deployments are safe and boring.

- [ ] Pluggable job backend: Celery or arq implementation (Redis), worker containers with dolfinx
- [ ] Job persistence (SQLite/Postgres) and artifact storage (filesystem/S3-compatible)
- [ ] Auth hooks (API keys / OIDC middleware), per-user quotas, wall-clock and memory limits
- [ ] Helm chart / compose profiles for API + workers + Redis
- [ ] Load test: N concurrent solves with progress streaming

## M4 — Gallery and docs site

Goal: adoption. People find, run, and copy examples.

- [ ] Docs site (mkdocs-material) with protocol reference generated from pydantic models
- [ ] Example gallery: airfoil potential flow → incompressible Navier–Stokes; solenoid
      magnetostatics; heat sink; each as a copy-paste-able app
- [ ] "Deploy to Fly.io/Render/self-host" one-clickish guides
- [ ] Announce: FEniCS Discourse, r/CFD, Hacker News

## M5 — Advanced / exploratory

- [ ] Parameter sweeps and design-of-experiments API (N jobs, one submission)
- [ ] Optimization loop hooks (dolfinx-adjoint / scipy.optimize driving the geometry params)
- [ ] Offline/degraded mode: scikit-fem under Pyodide behind the same JS SDK interface
- [ ] Sandboxed arbitrary-UFL mode (explicitly opt-in; see security posture in architecture doc)
- [ ] 3D geometry input (STEP upload, OpenCascade.js editor widget)

## Non-goals

- Reimplementing a CAD kernel or a mesher — Gmsh/OpenCascade do this.
- Competing with ParaView for post-processing depth; the viewer targets *embedded app* use cases.
- A hosted SaaS. Fenix Spoon is a toolkit; hosting it is the user's business (or a future separate
  project).
