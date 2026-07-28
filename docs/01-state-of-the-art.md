# State of the art: FEniCS-powered web applications

*Survey date: July 2026. Corrections and additions welcome — open an issue.*

The question this survey answers: **if you want a browser-based engineering app backed by
FEniCS today, what can you reuse, and what do you have to build yourself?**

## 1. The solver: FEniCSx

The FEniCS project has fully transitioned to **FEniCSx**: [dolfinx](https://github.com/FEniCS/dolfinx)
(C++ core with Python bindings, MPI-parallel, currently on the 0.11 release line with UFL 2026.1),
plus UFL, FFCx and Basix. Legacy FEniCS (2019.1) is unmaintained and should not be targeted by new
projects.

Deployment options relevant to a web context:

- **Docker images** (`dolfinx/dolfinx`) — the practical unit of deployment for servers. FEniCSx's
  native-code JIT pipeline (FFCx → C → shared object) makes containerization essentially mandatory
  for reproducible server installs.
- **Conda / Spack packages** — fine for development machines, awkward for fleet deployment.
- **[FEM on Colab](https://fem-on-colab.github.io/)** — installs FEniCSx into Google Colab
  notebooks; proves the "zero-install FEniCS in the browser via a hosted kernel" demand, but is
  notebook-bound, not app-bound.
- **WebAssembly: not viable.** dolfinx depends on MPI, PETSc and a JIT C compiler; a browser build
  is not on any roadmap. Any "FEniCS in the browser" story is necessarily client–server. (Lighter
  pure-Python FEM libraries like [scikit-fem](https://github.com/kinnala/scikit-fem) *can* run under
  Pyodide and are interesting as a degraded offline mode — see roadmap M5.)

**Takeaway:** the solver tier is solved and healthy, but it lives server-side, behind Docker.

## 2. Existing "FEM in the browser" systems

| System | Model | Notes |
|---|---|---|
| [SimScale](https://www.simscale.com/) | Commercial SaaS | Proves the product category (cloud FEM/CFD with in-browser geometry + post-processing). Closed source. |
| [NGSolve webgui](https://docu.ngsolve.org/latest/i-tutorials/appendix-webgui/webgui.html) | Jupyter widget, three.js | The closest open-source UX to what we want: mesh/field rendering in the browser, driven from Python. Tightly coupled to NGSolve's data structures; not reusable as a standalone widget for FEniCSx. **Main design reference.** |
| [FEAScript](https://feascript.com/) | JS library, in-browser solver | Lightweight FEM fully client-side. Interesting, but by construction limited to small problems; no path to the FEniCSx ecosystem (UFL forms, parallel solvers). |
| [SPARSELAB](https://sparselab.com/) | Browser front-end + cloud solve | Same architecture we propose, closed/commercial. |
| Jupyter(-Hub/-Lite) + [PyVista](https://pyvista.org/) | Notebook | The de-facto way FEniCSx results are shown in browsers today. Great for analysts, wrong shape for *applications* (an engineer shouldn't see a notebook). |

**Takeaway:** the category is validated (SimScale), the open-source pieces exist only as
notebook-bound widgets (NGSolve webgui, PyVista/trame) or toy in-browser solvers. **There is no
open-source, framework-agnostic toolkit for putting FEniCSx behind a web app.** That's the gap.

## 3. Visualization building blocks

- **[vtk.js](https://kitware.github.io/vtk-js/)** — WebGL/WebGPU scientific rendering in the
  browser: unstructured grids, scalar/vector fields, cutting planes, glyphs. The natural choice for
  client-side rendering of FEM results. No opinion about transport — you feed it data.
- **[trame](https://kitware.github.io/trame/)** (Kitware) — Python framework that builds whole web
  apps around VTK/ParaView, with two rendering modes (server-side image streaming, or scene-state
  sync rendered client-side by vtk.js). Excellent for standalone dashboards; heavier to *embed* into
  an existing product page, and it owns the whole page/app lifecycle. We treat trame as an
  alternative backend for M2+ rather than the core dependency.
- **three.js** — general-purpose 3D; what NGSolve webgui builds on. More manual work for scientific
  colormaps/meshes than vtk.js, but a smaller, more familiar dependency for web developers.
- **2D-first options** — for problems like airfoil sections and solenoid cross-sections, plain
  `<canvas>`/SVG (or Plotly/D3 for axes and contours) is often enough and keeps the embed footprint
  tiny. Our M0 demo deliberately uses raw canvas.
- **Formats:** glTF (+Draco compression) for surface meshes, VTU/VTKHDF for full datasets,
  or plain JSON typed-array payloads for small 2D fields. XDMF/HDF5 remains the FEniCSx-side
  interchange standard.

## 4. Geometry input in the browser

- **[OpenCascade.js](https://ocjs.org/)** / [CascadeStudio](https://github.com/zalo/CascadeStudio) /
  [replicad](https://replicad.xyz/) / [brepjs](https://github.com/andymai/brepjs) — the OpenCascade
  B-Rep kernel compiled to WASM: real parametric CAD in the browser, STEP export. Powerful but heavy
  (multi-MB WASM) and overkill for parametric 2D profiles.
- **Server-side [Gmsh](https://gmsh.info/)** (Python API, OpenCascade kernel built in) — the
  workhorse for turning geometry descriptions into FEniCSx meshes
  (`dolfinx.io.gmshio.model_to_mesh`). Battle-tested; this is what our server uses.
- **Hand-rolled 2D editors** — draggable control points / splines on canvas or SVG (optionally with
  libraries like Konva or Paper.js). This is what engineering UIs actually need for the airfoil /
  solenoid class of problems, and no reusable open-source "parametric 2D profile editor" widget
  exists. **Building one is a core deliverable (M2).**

**Takeaway:** send *parametric geometry descriptions* (JSON) from client to server and mesh with
Gmsh server-side; keep in-browser CAD kernels as an optional advanced widget.

## 5. Transport, jobs, orchestration

- **[FastAPI](https://fastapi.tiangolo.com/)** + WebSockets — the standard modern Python API layer;
  OpenAPI schemas for free, async-native, easy to embed a job manager into.
- **Job execution** — the well-trodden pattern is FastAPI + [Celery](https://docs.celeryq.dev/)
  (Redis/RabbitMQ) with worker containers; lighter alternatives (arq, Dramatiq, plain
  `asyncio` + process pools) are adequate below multi-user scale. FEM jobs add two twists that
  generic templates don't cover: **progress/residual streaming** during the solve and **large binary
  artifacts** as results. This is exactly the layer worth standardizing in a toolkit.
- **Existing FastAPI+Celery templates** ([example](https://github.com/GregaVrbancic/fastapi-celery))
  cover the plumbing but know nothing about meshes, fields, or solver adapters.

## 6. Gap analysis → what Fenix Spoon builds

| Need | Exists? | Fenix Spoon deliverable |
|---|---|---|
| FEniCSx server runtime | ✅ Docker images | Curated image + compose file (M1) |
| Job API with progress streaming | ❌ generic templates only | FastAPI server + job manager + WS protocol (M0 ✅) |
| Language-neutral geometry/field schemas | ❌ | Wire protocol v0 (M0 📝, M1) |
| Solver plug-in interface | ❌ | `Solver` adapter protocol (M0 ✅) |
| Embeddable geometry editor widget | ❌ | `@fenix-spoon/geometry-2d` (M2) |
| Embeddable field viewer widget | partial (vtk.js is a lib, not a widget) | `@fenix-spoon/viewer` wrapping vtk.js (M2) |
| Develop front-end without FEniCSx | ❌ | NumPy mock solver (M0 ✅) |

## Sources

- [FEniCSx documentation](https://docs.fenicsproject.org/) · [dolfinx on GitHub](https://github.com/FEniCS/dolfinx) · [dolfinx releases](https://github.com/FEniCS/dolfinx/releases) · [FEniCSx tutorial (J. Dokken)](https://jsdokken.com/dolfinx-tutorial/)
- [NGSolve webgui docs](https://docu.ngsolve.org/latest/i-tutorials/appendix-webgui/webgui.html) · [webgui internals](https://docu.ngsolve.org/latest/i-tutorials/appendix-webgui/webgui-internal.html)
- [trame: Visual Analytics Everywhere (Kitware)](https://www.kitware.com/trame-visual-analytics-everywhere/) · [PyVista + trame](https://tutorial.pyvista.org/tutorial/09_trame/index.html) · [Scientific visualization on the web with VTK/ParaView](https://web3d.siggraph.org/archive/web3d2023/2023/07/18/scientific-visualization-on-the-web-with-vtk-and-paraview/index.html)
- [FEAScript](https://feascript.com/) · [SPARSELAB](https://sparselab.com/) · [Elmer FEM](https://www.elmerfem.org/blog/) · [FreeFEM](https://freefem.org/)
- [OpenCascade.js](https://ocjs.org/) · [CascadeStudio](https://github.com/zalo/CascadeStudio) · [brepjs](https://github.com/andymai/brepjs) · [awesome-cad](https://github.com/mlightcad/awesome-cad)
- [FastAPI + Celery template](https://github.com/GregaVrbancic/fastapi-celery) · [FastAPI and Celery (TestDriven)](https://testdriven.io/blog/fastapi-and-celery/)
