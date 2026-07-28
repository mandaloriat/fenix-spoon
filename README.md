<p align="center">
  <img src="logo.png" alt="Fenix Spoon" width="420">
</p>

**A Swiss-army toolkit for building web-based engineering applications powered by [FEniCSx](https://fenicsproject.org/).**

Fenix Spoon is an open-source (MIT) toolkit that packages everything you need to put a finite-element
solver behind a web page: a ready-to-deploy simulation server, a clean HTTP/WebSocket protocol for
submitting jobs and streaming results, and embeddable browser widgets for geometry input and field
visualization.

The canonical use case: an engineer opens a web page, drags the control points of a 2D airfoil (or a
solenoid cross-section), presses *Run*, and watches the simulation result appear live — no local
installation, no desktop tooling, just a browser talking to a FEniCSx server.

> **Status: kickstart (M0).** The repository ships a working end-to-end vertical slice using a
> pure-NumPy mock solver, so you can run the full loop (edit geometry → submit → stream progress →
> render field) without installing FEniCSx. The real FEniCSx adapter and the widget library are the
> next milestones — see the [roadmap](docs/03-roadmap.md).

## Why

Deploying FEniCS behind a web UI today means hand-rolling the same stack every time: a Docker image
with dolfinx, an API server, a job queue, a serialization format for meshes and fields, and a
JavaScript viewer. All the ingredients exist in the open-source ecosystem, but there is no glue —
see the [state-of-the-art survey](docs/01-state-of-the-art.md). Fenix Spoon is that glue.

## What's in the box

| Component | Where | Status |
|---|---|---|
| **Simulation server** — FastAPI app: job submission, WebSocket progress streaming, cancellation, wall-clock timeouts, result + artifact retrieval | [`server/`](server/) | ✅ working |
| **Solver adapter protocol** — plug any solver (FEniCSx, mock, anything Python) behind the same API via `SolverContext` (progress / cancel / artifacts) | [`server/fenixspoon/solvers/`](server/fenixspoon/solvers/) | ✅ working |
| **Mock solvers** — potential flow and magnetostatics in pure NumPy; let you develop the front-end without FEniCSx | [`mock_laplace.py`](server/fenixspoon/solvers/mock_laplace.py), [`mock_magnetostatics.py`](server/fenixspoon/solvers/mock_magnetostatics.py) | ✅ working |
| **FEniCSx adapters** — the same two problems on unstructured Gmsh meshes, cross-validated against the mock solvers | [`dolfinx_poisson.py`](server/fenixspoon/solvers/dolfinx_poisson.py), [`dolfinx_magnetostatics.py`](server/fenixspoon/solvers/dolfinx_magnetostatics.py) | ✅ validated on dolfinx 0.11 (`pytest -m fenics`, CI job in the dolfinx image) |
| **Wire protocol** — JSON schemas for geometry (`domain2d`, `regions2d`), jobs, events, `grid2d`/`mesh2d` results, artifacts — with a conformance fixture corpus | [`docs/04-wire-protocol.md`](docs/04-wire-protocol.md), [`protocol/fixtures/`](protocol/fixtures/) | ✅ v0 implemented |
| **Browser demos** — zero-dependency HTML pages: draggable airfoil with live flow, editable solenoid with magnetostatics and field lines | [`examples/airfoil-2d/`](examples/airfoil-2d/), [`examples/solenoid-2d/`](examples/solenoid-2d/) | ✅ working |
| **JS/TS SDK** — `@fenix-spoon/client`: typed protocol client with progress streaming, reconnection and runtime validators | [`client/packages/client/`](client/packages/client/) | ✅ working |
| **Widget library** — embeddable geometry editors and vtk.js-based viewers as npm packages | [`client/`](client/) | 📋 planned (M2) |
| **Docker deployment** — one image with dolfinx + server, `docker compose up` | [`Dockerfile`](server/Dockerfile), [`docker-compose.yml`](docker-compose.yml) | ✅ scaffolded |

## Quickstart (no FEniCSx required)

```bash
cd server
pip install -e ".[dev]"
uvicorn fenixspoon.main:app --reload
```

Then open <http://localhost:8000/> for the demo index — drag the airfoil's control points and
watch the flow field update, or resize a solenoid's iron core and watch the flux redistribute.
API docs (OpenAPI) live at <http://localhost:8000/docs>.

With Docker (full FEniCSx runtime):

```bash
docker compose up --build
```

Without Docker, a conda environment works too (this is also how the FEniCSx test suite runs):

```bash
micromamba create -p ./fenicsenv -c conda-forge python=3.12 fenics-dolfinx python-gmsh \
    fastapi uvicorn pytest httpx
./fenicsenv/bin/pip install -e ./server
./fenicsenv/bin/pytest server/tests            # includes the `-m fenics` adapter tests
./fenicsenv/bin/uvicorn fenixspoon.main:app --app-dir server
```

## Architecture at a glance

```mermaid
flowchart LR
    subgraph Browser
        GE[Geometry editor widget] --> SDK[JS client SDK]
        SDK --> FV[Field viewer widget]
    end
    subgraph Server["Fenix Spoon server (Docker)"]
        API[FastAPI<br/>REST + WebSocket] --> JM[Job manager]
        JM --> SA[Solver adapters]
        SA --> DX[FEniCSx / dolfinx]
        SA --> MK[Mock NumPy solver]
        DX --> GM[Gmsh meshing]
    end
    SDK -- "POST /jobs · WS /events · GET /result" --> API
```

The full design, including the trade-offs (server-side vs client-side rendering, job queue options,
serialization formats), is in [`docs/02-architecture.md`](docs/02-architecture.md).

## Repository layout

```
├── docs/                  # Survey, architecture, roadmap, wire protocol
├── server/                # Python package `fenixspoon` (FastAPI app + solvers) and tests
├── client/                # (planned) npm packages: geometry editors, field viewers, JS SDK
├── examples/airfoil-2d/   # Self-contained browser demo
└── docker-compose.yml
```

## Documentation

1. [State of the art](docs/01-state-of-the-art.md) — what exists today, and the gap this project fills
2. [Architecture](docs/02-architecture.md) — components, choices, trade-offs
3. [Roadmap](docs/03-roadmap.md) — milestones M0 → M5
4. [Wire protocol](docs/04-wire-protocol.md) — the JSON contract between client and server

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The roadmap issues are the best entry point.

## License

[MIT](LICENSE). Note that FEniCSx components (dolfinx, UFL, FFCx, Basix) are LGPL-3.0-or-later and
are used as external dependencies; the Docker images bundle them under their own licenses.
