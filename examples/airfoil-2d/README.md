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

## Reading a lift coefficient

`index.html`'s status line reports `C_L`, `C_m,c/4` and `C_p,min` from the result's `metrics`.
Those are real numbers as of protocol 1.4: both potential-flow adapters impose the **Kutta
condition** at the trailing edge, so the circulation is solved for rather than left to whichever
constant the body's streamline happened to carry
([#68](https://github.com/mandaloriat/fenix-spoon/issues/68)).

Neither page has an incidence control — the interesting thing to drag here is the geometry — but
the parameter is `alpha`, in degrees, and it rotates the free stream rather than the body, so a
sweep re-uses the same domain and the same mesh:

```jsonc
{ "solver": "mock.laplace2d", "geometry": { "...": "..." },
  "params": { "resolution": 192, "iterations": 12000, "alpha": 5.0 } }
```

The surface pressure distribution comes back in the same response, under `series`, as two traces
against `x/c`. Neither page plots it: `<fs-viewer>` draws 2-D fields and declines a curve, and the
plot widget a `C_p` distribution wants — axes, a legend, an inverted `y` so suction reads upward —
does not exist yet. `resultSeries(result)` is where to find the data meanwhile.

Pass `kutta: false` for the pre-1.4 behaviour: half the work, the same flow field, and no lift
metrics at all rather than a zero. The result says so in `diagnostics.warnings`, and
`GET /api/v1/capabilities/mock.laplace2d?sections=assumptions` says so before you submit.

The vanilla page was the reference implementation the widgets were extracted from, and it stays
here for exactly that reason — it is the shortest complete description of the protocol in the
repository.
