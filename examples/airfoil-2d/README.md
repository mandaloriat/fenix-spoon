# Airfoil 2D demo

A single self-contained HTML page — no build step, no dependencies — exercising the whole
Fenix Spoon loop:

1. **Geometry input**: draggable control points of an airfoil polygon (double-click to add/remove
   points) inside a rectangular flow domain.
2. **Job submission**: `POST /api/v1/jobs` with the `domain2d` geometry and solver params.
3. **Live progress**: WebSocket subscription to `/api/v1/jobs/{id}/events` (iteration + residual).
4. **Result rendering**: the `grid2d` field (velocity magnitude or streamfunction) drawn to a
   canvas with a viridis colormap, obstacle masked out.

## Run it

```bash
cd server && pip install -e . && uvicorn fenixspoon.main:app
```

then open <http://localhost:8000/demo/airfoil-2d/index.html> (the server mounts `examples/` at
`/demo`). Opening `index.html` directly from disk also works — it falls back to
`http://localhost:8000` as the API base.

The solver dropdown lists whatever the server has: in a plain venv that's the NumPy mock solver;
inside the Docker image the FEniCSx adapter appears alongside it, with the same UI.

This page doubles as the reference implementation for the M2 widget work: the editor and viewer
here will become `@fenix-spoon/geometry-2d` and `@fenix-spoon/viewer`.
