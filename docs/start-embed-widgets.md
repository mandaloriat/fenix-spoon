# Embed the widgets

Three packages, no framework. The editor and the viewer are custom elements, so they work
in React, Vue, Svelte or a plain `<script>` tag identically.

| Package | What it is |
|---|---|
| `@fenix-spoon/client` | Typed SDK for the protocol: submit, stream progress, fetch results |
| `@fenix-spoon/geometry-2d` | `<fs-geometry-2d>` — draggable profile editor, emits protocol JSON |
| `@fenix-spoon/viewer` | `<fs-viewer>` — canvas renderer for `grid2d` and `mesh2d` |

## The whole loop

```js
import { FenixSpoonClient } from '@fenix-spoon/client';
import '@fenix-spoon/geometry-2d';
import '@fenix-spoon/viewer';

const client = new FenixSpoonClient('http://localhost:8000');
const editor = document.querySelector('fs-geometry-2d');
const viewer = document.querySelector('fs-viewer');

document.querySelector('#run').addEventListener('click', async () => {
  const job = await client.submit({
    solver: 'mock.laplace2d',
    geometry: { type: 'domain2d', bounds: [-1, -1, 2, 1], obstacle: editor.polygon },
    params: { resolution: 128 },
  });

  viewer.result = await job.wait((event) => {
    if (event.type === 'progress') console.log(event.iteration, event.residual);
  });
});
```

`job.wait()` holds the WebSocket, reports progress through the callback and resolves with
the result. `examples/airfoil-2d/index.html` is this, complete and running.

## Getting the packages

They are not on npm yet. Build them from the repository:

```bash
npm --prefix client install && npm --prefix client run build
```

The server then serves them at `/packages/`, which is how the demos load them — through
an import map, no bundler:

```html
<script type="importmap">
  { "imports": {
      "@fenix-spoon/client": "/packages/client/index.js",
      "@fenix-spoon/geometry-2d": "/packages/geometry-2d/index.js",
      "@fenix-spoon/viewer": "/packages/viewer/index.js"
  } }
</script>
```

## The editor

```html
<fs-geometry-2d bounds="-1,-1,2,1" mode="spline"></fs-geometry-2d>
```

Reads and writes `element.polygon` as protocol JSON, emits `change` on every edit. Points
are draggable with mouse or keyboard, `mode="spline"` samples a closed centripetal
Catmull–Rom curve through them, and undo/redo is built in.

Two behaviours worth knowing. The spline **bulges outside its control points** — that is
what interpolation with tangents does, and it is why the sampled outline, not the control
hull, is what gets validated. And changing `bounds` re-clamps the points and clears the
history, because an undo that restored a point outside the new box would produce geometry
the server rejects.

## The viewer

```html
<fs-viewer field="psi" colormap="viridis" contours="12"></fs-viewer>
```

Set `element.result` to a result envelope and it renders. It handles both result kinds,
draws a colorbar and iso-contours, and probes values on hover. `element.fields` lists the
field names in the current result, which is what you build a selector from.

Canvas, not vtk.js: every result kind is 2D today, and a multi-megabyte WebGL toolkit
would dominate the embed footprint. The drawing surface is isolated so a WebGL backend
can land when a 3D result kind does.

## Validators, when you do not trust the source

The SDK exports the runtime validators used by the conformance suite:

```js
import { validateJobResult, validateGeometry } from '@fenix-spoon/client';

const result = validateJobResult(await response.json());  // throws with a specific message
```

The client's own methods do *not* validate — they cast, because the server is usually
your own. Reach for these when a payload arrives from somewhere else: a saved file, a
paste, another service. They mirror the server's pydantic rules and both sides are tested
against the same fixture corpus, so what one accepts the other does too.
