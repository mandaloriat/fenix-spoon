# @fenix-spoon/viewer

Renders [Fenix Spoon](https://github.com/mandaloriat/fenix-spoon) `grid2d` and `mesh2d` field
results as a framework-agnostic custom element: colormaps, colorbar, iso-contours and a hover
probe readout.

```bash
npm install @fenix-spoon/viewer
```

## Usage

```html
<fs-viewer field="speed" colormap="viridis" contours="10" units="m/s"
           style="width: 100%; height: 400px"></fs-viewer>

<script type="module">
  import '@fenix-spoon/viewer';
  import { FenixSpoonClient } from '@fenix-spoon/client';

  const client = new FenixSpoonClient('http://localhost:8000');
  const job = await client.submit({ solver: 'mock.laplace2d', geometry });
  document.querySelector('fs-viewer').result = await job.wait();
</script>
```

The element takes `grid2d` and `mesh2d` results interchangeably — it reads the fields through the
protocol's shape, so swapping the mock solver for a FEniCSx one changes nothing on this side.

To control registration (a different tag name, or none at all), import the `element` subpath:

```ts
import { FieldViewerElement, defineFieldViewer } from '@fenix-spoon/viewer/element';
```

## Why canvas — the opposite call from the geometry editor

`@fenix-spoon/geometry-2d` renders with SVG; this one uses canvas. The rule is what the pixels
are for: a handful of interactive handles want to be DOM nodes, because that buys focus, keyboard
operation and hit-testing for free. Thousands of coloured triangles per frame want a raster
surface, because a thousand `<polygon>` elements would not.

Everything that doesn't need a drawing context — colormaps, ranges, contour extraction, probing —
lives in [`colormap.ts`](src/colormap.ts) and [`field.ts`](src/field.ts) as pure functions. They
are exported, unit-tested directly, and usable without the element; the element itself is a thin
painter over them.

## Not vtk.js (yet)

The roadmap names [vtk.js](https://kitware.github.io/vtk-js/) for this widget, and that remains
right for 3D. It is the wrong trade today: every result kind the protocol defines is 2D, and
pulling in a multi-megabyte WebGL toolkit to fill triangles would blow the embed footprint that
makes these widgets worth using. The rendering surface is isolated behind `draw()`, so a WebGL
backend can be added when a 3D result kind lands (roadmap M5, `step3d`) without disturbing the
element's API.

## API

| Member | Type | Notes |
|---|---|---|
| `result` | `JobResult \| null` | The result to display. Setting it re-renders and keeps the current field if the new result still has it. |
| `field` | `string \| null` | Which scalar to draw; also the `field="…"` attribute. Defaults to the result's first field. |
| `fields` | `string[]` | Field names in the current result — drive a picker from it. |
| `colormap` | `'viridis' \| 'plasma' \| 'coolwarm' \| 'greyscale'` | Also the `colormap="…"` attribute. Unknown names are ignored. |
| `range` | `{ min, max } \| null` | The scalar range currently mapped to the colormap. |
| `probe([x, y])` | `number \| undefined` | Sample at a domain position. Grids use nearest-node; meshes interpolate barycentrically inside the containing triangle. |
| `toDataURL()` | `string` | Current view as a PNG data URL. |
| `render()` / `draw()` | | `render()` coalesces bursts into one paint; `draw()` paints now. |

### Attributes

| Attribute | Effect |
|---|---|
| `field` | Scalar to display |
| `colormap` | One of the four names above |
| `contours` | Number of iso-lines; `0` or absent draws none |
| `colorbar` | `off` hides the colorbar |
| `symmetric` | Makes the range symmetric about zero — what `coolwarm` needs to read correctly |
| `units` | Caption on the colorbar and in the probe readout |

## Behaviour worth knowing

- **Masked cells** (`grid2d`'s obstacle mask) render as background, and contouring skips any cell
  touching a masked node rather than drawing a line through a hole.
- **A constant field** doesn't divide by zero: the range is padded before normalising.
- **The accessible description** states the field, the topology and the range, and it is set
  whether or not a drawing context exists.

## Styling

```css
fs-viewer {
  --fs-viewer-bg: #1e1e22;
  --fs-viewer-readout-bg: rgba(0, 0, 0, 0.6);
  --fs-viewer-readout-fg: #fff;
}
```

## License

MIT
