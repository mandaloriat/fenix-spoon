# @fenix-spoon/viewer

Renders and explores [Fenix Spoon](https://github.com/mandaloriat/fenix-spoon) `grid2d` and
`mesh2d` field results as a framework-agnostic custom element: colormaps, colorbar,
iso-contours, a probe readout, vector glyphs, integral curves, and zoom/pan navigation with
explicit interaction modes.

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

It **declines a `series1d` result**, the curve kind protocol 1.5 added. That is not an omission: a
curve has no bounds to fit, no topology to interpolate over and nothing to contour, and the axes,
legend and inverted `y` an aerodynamic `C_p` plot wants are a different widget's problem. Handing
one over clears the view and logs why. Read the curves with `resultSeries(result)` from
`@fenix-spoon/client` — including the ones that ride along *with* a field, which this element
draws as usual.

To control registration (a different tag name, or none at all), import the `element` subpath:

```ts
import { FieldViewerElement, defineFieldViewer } from '@fenix-spoon/viewer/element';
```

## Exploring a result

Everything below works on the arrays the result already carries. **No operation submits a
job**, and none needs a newer server: zoom, pan, probe, sections, glyph density, colour scale
and curve seeding are all recomputed in the page. The design and its boundaries are recorded
in [ADR 0001](https://mandaloriat.github.io/fenix-spoon/adr/0001-explorable-viewer/).

```html
<fs-viewer interactive mode="pan" toolbar="pan,probe,fit-domain,reset"
           vectors="velocity" glyphs="28" streamlines="velocity"></fs-viewer>
```

**Navigation is opt-in.** Without the `interactive` attribute the element behaves exactly as
it always has — it draws, and it reads a value out on hover — because a page that layers a
geometry editor over the viewer must be the one deciding which of the two owns a drag. The
programmatic API (`fitDomain()`, `viewport`, `probe()`, …) is always available.

### Interaction modes

`mode` says what a pointer gesture does, so two overlaid widgets never fight over one drag.

| Mode | Drag | Notes |
|---|---|---|
| `probe` *(default)* | nothing | Hover reads the field out; this is what the element did before modes existed |
| `pan` | moves the view | Hover still reads out |
| `section` | draws a line | Emits `fs-section` with the sampled curve on release |
| `seed` | — click seeds a curve | Adds a streamline seed at the pointer |
| `none` | nothing | Claims no pointer gesture at all — hand the surface to an overlaid editor |

A **two-finger pinch zooms in every mode but `none`**: no application wants two fingers to
mean "probe twice", and `none` means exactly what it says. The keyboard keeps working in
`none`, because focus is an unambiguous statement of which widget the user is addressing.

A second finger, or a `pointercancel`, **abandons** a section being dragged rather than
finishing it: no `fs-section` commit is emitted, and a line drawn earlier comes back. Only a
drag that ends in a `pointerup` is a section the application asked for.

Keyboard, when `interactive` and focused: arrows pan, `+`/`-` zoom, `0` or `Home` resets, `g`
fits the geometry, `Escape` clears a section. Only those keys are claimed — `Tab` still moves
focus out.

### Coordinating with the geometry editor

```js
viewer.addEventListener('fs-viewport-change', (event) => {
  editor.viewBox = event.detail.viewport;   // display-only; `bounds` stays the domain
});

editMode.addEventListener('change', (event) => {
  viewer.mode = event.target.checked ? 'none' : 'pan';
  editor.readOnly = !event.target.checked;
});
```

`<fs-geometry-2d>`'s `viewBox` reframes the drawing without touching the geometry, which is
what keeps a control point over the feature it was placed on while the viewer is zoomed.

### Integral curves — and when they are refused

`streamlines="velocity"` integrates a **vector** field into the curves tangent to it. They are
called integral curves, or streamlines, and never "flow lines": whether a curve is a flow line,
a magnetic field line or a heat-flux path depends on what the vector means, which the protocol
does not say and this package cannot know. Your application asked for the solve; it gets to name
them.

**A scalar field is never turned into a vector one.** Integrating a streamfunction, or a
gradient of whatever happens to be in the field picker, produces curves that look like physics
and are not. So the tool refuses, and says why in a sentence you can put on screen:

```js
const { available, reason, fields } = viewer.capabilities.streamlines;
if (!available) toolbar.disable('streamlines', reason);
// "This result carries no vector field, so there is nothing to integrate. Integral curves
//  cannot be reconstructed from a scalar field — a solver has to send the components for
//  them to exist."
```

Density and seeds are yours to change, and changing them re-integrates in the page:

```js
viewer.streamlineDensity = 24;              // seed lattice columns, capped at 64
viewer.streamlineSeeds = [[0.5, 0], [1, 0]]; // explicit seeds instead of the lattice
viewer.addStreamlineSeed([1.5, 0.2]);        // what `mode="seed"` does on a click
viewer.clearStreamlineSeeds();
```

### Comparing two solves on one scale

Auto-scaling two pictures gives them two different colour meanings and invites the wrong
conclusion. Pin the range and both are readable against each other:

```js
const scale = { min: 0, max: 120 };
before.range = scale;
after.range = scale;        // `viewer.scale` reads 'manual'; the colourbar says "fixed"
after.range = null;         // back to auto; `autoRange` had the data's own range all along
```

### Sections

```js
const section = viewer.sampleSection([0, -0.5], [0, 0.5], { samples: 200 });
// { distance: [...], value: [...], at: [[x, y], ...], requested: 200, skipped: 12 }
```

The shape is the server's `POST /jobs/{id}/query {"op": "section"}` result, key for key, and
the interpolation matches it too — so a page can move between the two without rewriting its
plotting code. `skipped` counts samples that fell where nothing was solved (a section across
an airfoil crosses the body); dropping them beats interpolating filler through a hole.

### Geometry

A result carries arrays and a bounding box, not an outline, so "fit geometry" needs one:

```js
viewer.geometry = { type: 'domain2d', bounds: [...], obstacle: editor.polygon };
viewer.fitGeometry();          // frames the obstacle with a margin
viewer.showGeometry = false;   // stop outlining it over the field
```

Nothing is inferred from the `grid2d` mask: it marks the cells that were not solved, which is
the *body* for a `domain2d` obstacle and the *air* for a `regions2d` heat sink. One heuristic,
two opposite meanings.

### Application overlays

```js
viewer.overlay = ({ ctx, toScreen }) => {
  const [x, y] = toScreen([centreOfPressure, 0]);
  ctx.fillStyle = '#f59e0b';
  ctx.beginPath();
  ctx.arc(x, y, 5, 0, Math.PI * 2);
  ctx.fill();
};
```

Written in domain coordinates through `toScreen`, so it survives every zoom and pan for free.
This is where a resultant arrow, a pin, or an annotation goes — rather than this package
growing a name for each.

## Why canvas — the opposite call from the geometry editor

`@fenix-spoon/geometry-2d` renders with SVG; this one uses canvas. The rule is what the pixels
are for: a handful of interactive handles want to be DOM nodes, because that buys focus, keyboard
operation and hit-testing for free. Thousands of coloured triangles per frame want a raster
surface, because a thousand `<polygon>` elements would not.

Everything that doesn't need a drawing context — colormaps, ranges, contour extraction, probing,
viewport arithmetic, curve integration — lives in [`colormap.ts`](src/colormap.ts),
[`field.ts`](src/field.ts), [`viewport.ts`](src/viewport.ts) and
[`streamlines.ts`](src/streamlines.ts) as pure functions. They are exported, unit-tested
directly, and usable without the element; the element itself is a thin painter over them.

## Not vtk.js (yet)

The roadmap names [vtk.js](https://kitware.github.io/vtk-js/) for this widget, and that remains
right for 3D. It is the wrong trade today: every result kind the protocol defines is 2D, and
pulling in a multi-megabyte WebGL toolkit to fill triangles would blow the embed footprint that
makes these widgets worth using. The rendering surface is isolated behind `draw()`, so a WebGL
backend can be added when a 3D result kind lands (roadmap M5, `step3d`) without disturbing the
element's API.

## API

### Data and appearance

| Member | Type | Notes |
|---|---|---|
| `result` | `JobResult \| null` | The result to display. Setting it re-renders and keeps the current field if the new result still has it. |
| `field` | `string \| null` | Which scalar to draw; also the `field="…"` attribute. Defaults to the result's first field. |
| `fields` | `string[]` | Field names in the current result — drive a picker from it. |
| `vectorFields` | `string[]` | Vector field names; empty against a pre-1.1 server. |
| `colormap` | `'viridis' \| 'plasma' \| 'coolwarm' \| 'greyscale'` | Also the `colormap="…"` attribute. Unknown names are ignored. |
| `range` | `{ min, max } \| null` | Get: the range currently mapped. Set: **pins** the scale; `null` restores auto. |
| `autoRange` | `{ min, max } \| null` | What the data would produce, ignoring any pin. |
| `scale` | `'auto' \| 'manual'` | Which of the two is in force. |
| `fieldUnits` | `Record<string, string> \| null` | Units per field, for the colourbar caption and the readout. |
| `fieldLabels` | `Record<string, string> \| null` | Display names per field, when the wire name is not what a reader should see. |
| `geometry` | `Geometry \| null` | The geometry the job was submitted with. Never read from the result. |
| `showGeometry` | `boolean` | Outline the geometry over the field. Default true. |
| `overlay` | `(ctx: OverlayContext) => void` | Application painter, run last. |

### Navigation

| Member | Type | Notes |
|---|---|---|
| `interactive` | `boolean` | Wire up wheel, drag, pinch and keyboard. Off by default. |
| `mode` | `'pan' \| 'probe' \| 'section' \| 'seed' \| 'none'` | What a gesture does. Default `probe`. |
| `viewport` | `Bounds2D \| null` | The visible domain window. Setting `null` restores the domain fit. |
| `zoom` | `number` | Multiple of the domain span currently framed. |
| `fitDomain()` / `resetView()` | | Frame the whole domain. |
| `fitGeometry(padding?)` | `boolean` | Frame `geometry`; false when there is none. |
| `setViewport(view, reason?)` | | Same as assigning `viewport`, with a reason for the event. |
| `zoomBy(factor, at?)` | | `factor < 1` moves in. `at` is a **domain** point, defaulting to the view centre. |
| `panBy(dxPx, dyPx)` | | Screen-space delta in CSS pixels. |
| `toScreen(p)` / `toDomain(p)` | `Point` | The transforms the overlay uses. |

### Sampling and tools

| Member | Type | Notes |
|---|---|---|
| `probe([x, y])` | `number \| undefined` | Nearest-node on a grid; barycentric inside a mesh triangle. What the readout shows. |
| `sample([x, y], field?)` | `number \| undefined` | Interpolated — bilinear on a grid — matching the server's `at_point`. |
| `sampleSection(start, end, {samples, field})` | `Section \| undefined` | Field along a line, in the server's `section` shape. |
| `section` | `{ start, end } \| null` | The line currently drawn; `clearSection()` removes it. |
| `streamlines` | `string \| null` | Vector field to integrate, or null. |
| `streamlineDensity` | `number` | Seed lattice columns, capped at 64. |
| `streamlineSeeds` | `Point[] \| null` | Explicit seeds; `addStreamlineSeed`, `clearStreamlineSeeds`. |
| `integralCurves` | `IntegralCurve[]` | The curves currently drawn, in domain coordinates. |
| `capabilities` | `ViewerCapabilities` | Per tool: `{ available, reason? }`. Drive a toolbar from it. |
| `tools` | `ToolbarTool[]` | Which built-in buttons to show; empty means no toolbar. |
| `toDataURL()` | `string` | Current view as a PNG data URL. |
| `render()` / `draw()` | | `render()` coalesces bursts into one paint; `draw()` paints now. |

### Attributes

| Attribute | Effect |
|---|---|
| `field` | Scalar to display |
| `colormap` | One of the four names above |
| `contours` | Number of iso-lines; `0` or absent draws none |
| `colorbar` | `off` hides the colorbar |
| `symmetric` | Makes the auto range symmetric about zero — what `coolwarm` needs to read correctly |
| `units` | Fallback caption on the colorbar and in the probe readout; `fieldUnits` overrides per field |
| `vectors` | Vector field to draw as arrow glyphs |
| `glyphs` | Arrows across the visible width; capped at 128 |
| `glyph-scale` | Arrow length multiplier, `0`–`8` |
| `streamlines` | Vector field to integrate into curves |
| `streamline-density` | Seed lattice columns; capped at 64 |
| `range-min` / `range-max` | Both present and ordered pins the colour scale |
| `interactive` | Enables wheel, drag, pinch and keyboard navigation |
| `mode` | `pan`, `probe`, `section`, `seed`, `none` |
| `toolbar` | Comma-separated tool ids; bare attribute uses a default set; absent shows none |

Toolbar ids: `pan`, `probe`, `section`, `seed`, `zoom-in`, `zoom-out`, `fit-domain`,
`fit-geometry`, `reset`, `export`. Unknown ids are ignored rather than rendered dead, and a
button whose tool is unavailable is disabled with the reason in its tooltip.

### Events

All bubble and cross the shadow boundary.

| Event | `detail` |
|---|---|
| `fs-viewport-change` | `{ viewport, reason }` — `zoom`, `pan`, `fit`, `reset`, `set` |
| `fs-probe` | `{ at, value, field, units }`; `at` and `value` are null when the pointer leaves |
| `fs-mode-change` | `{ mode }` |
| `fs-section` | `{ start, end, phase, field, section }` — `phase` is `preview` while dragging, `commit` on release, and only a commit carries the sampled `section` |
| `fs-seed` | `{ at }` |
| `fs-export` | `{ dataUrl }` — from the toolbar's export button; the page decides what to do with it |

## Behaviour worth knowing

- **Masked cells** (`grid2d`'s obstacle mask) render as background, contouring skips any cell
  touching a masked node, glyph averaging excludes them, and an integral curve stops at the
  edge of the solved region rather than stepping into it.
- **A constant field** doesn't divide by zero: the range is padded before normalising.
- **Glyph density follows the screen, not the data.** The lattice cell is sized from the
  *visible* span, so zooming in keeps the arrow spacing constant; cell indices are counted
  from the domain origin, so panning does not make the arrows crawl.
- **The frame survives a re-solve** of the same domain — the point of an auto-run loop is
  watching one region change — and is dropped when the new result's bounds differ.
- **The accessible description** states the field, the topology, the range, whether the scale
  is pinned, the zoom and the mode, and it is set whether or not a drawing context exists.
  The probe readout is an `aria-live="polite"` region.
- **Nothing animates.** A zoom or a fit is applied on the frame it is asked for, so there is
  no motion for `prefers-reduced-motion` to reduce; the media query covers the toolbar, which
  is the only thing with a transition.

## Styling

```css
fs-viewer {
  --fs-viewer-bg: #1e1e22;
  --fs-viewer-readout-bg: rgba(0, 0, 0, 0.6);
  --fs-viewer-readout-fg: #fff;
  --fs-viewer-focus: #f59e0b;
  --fs-viewer-tool-bg: rgba(0, 0, 0, 0.55);
  --fs-viewer-tool-fg: #fff;
  --fs-viewer-tool-border: rgba(255, 255, 255, 0.25);
  --fs-viewer-tool-active-bg: rgba(59, 130, 246, 0.85);
}
```

## Tests

```bash
npm test                # vitest, jsdom: transforms, integration, probing, element behaviour
npm run test:browser    # real Chromium over CDP: wheel, drag, pinch, keyboard, painting
```

The browser suite needs the packages built and a Chromium on the machine (it looks at
`FENIXSPOON_CHROMIUM`, `CHROME_PATH`, `PLAYWRIGHT_BROWSERS_PATH` and the usual system paths),
and skips itself with a message otherwise. It deliberately adds no dependency —
[`tests/browser/cdp.mjs`](tests/browser/cdp.mjs) is a ~150-line DevTools Protocol client over
Node's built-in WebSocket, which is cheaper than a browser download on every install of a
package that exists to be small.

## License

MIT
