# Airfoil 2D demo

Two pages solving the same problem, kept side by side on purpose:

| Page | What it is |
|---|---|
| [`index.html`](index.html) | Built from the published packages — `@fenix-spoon/client`, `<fs-geometry-2d>`, `<fs-viewer>`. Run `npm --prefix client install && npm --prefix client run build` first; the server then serves the bundles at `/packages/`. |
| [`index-vanilla.html`](index-vanilla.html) | One self-contained HTML file, no build step, no dependencies. The readable reference for what actually goes over the wire. |

The widget version is roughly a third the JavaScript of the vanilla one, and everything that
disappeared — keyboard operation, undo/redo, insert/remove, contours, the colorbar, the probe
readout — is *more* than what the vanilla page ever had. That's the case for the packages.

## What the vanilla page exercises

1. **Geometry input**: draggable control points of an airfoil polygon (double-click to add/remove
   points) inside a rectangular flow domain.
2. **Job submission**: `POST /api/v1/jobs` with the `domain2d` geometry and solver params.
3. **Live progress**: WebSocket subscription to `/api/v1/jobs/{id}/events` (iteration + residual).
4. **Result rendering**: the `grid2d` field (velocity magnitude or streamfunction) drawn to a
   canvas with a viridis colormap, obstacle masked out.

## Run it

```bash
cd server && pip install -e . && uvicorn fenixspoon.main:app
npm --prefix ../client install && npm --prefix ../client run build   # for the widget version
```

then open <http://localhost:8000/> and pick a page. The server mounts `examples/` at `/demo` and
any built package at `/packages/<name>/`; the widget page uses an import map pointing there, and
tells you to build if the bundles are missing. `index-vanilla.html` also works opened straight
from disk — it falls back to `http://localhost:8000` as the API base.

The solver dropdown lists whatever the server has: in a plain venv that's the NumPy mock solver;
inside the Docker image the FEniCSx adapter appears alongside it, with the same UI.

The vanilla page was the reference implementation the widgets were extracted from, and it stays
here for exactly that reason — it is the shortest complete description of the protocol in the
repository.
