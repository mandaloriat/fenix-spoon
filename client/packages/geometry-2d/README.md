# @fenix-spoon/geometry-2d

A parametric 2D profile editor as a framework-agnostic custom element. Drag control points to
reshape an airfoil, a solenoid core, any closed profile — and read the result back as
[Fenix Spoon](https://github.com/mandaloriat/fenix-spoon) protocol geometry.

```bash
npm install @fenix-spoon/geometry-2d
```

## Usage

```html
<fs-geometry-2d bounds="-1,-1,2,1" mode="spline" style="height: 400px"></fs-geometry-2d>

<script type="module">
  import '@fenix-spoon/geometry-2d';

  const editor = document.querySelector('fs-geometry-2d');
  editor.addEventListener('change', () => {
    console.log(editor.value); // { type: 'domain2d', bounds: [...], obstacle: {...} }
  });
</script>
```

It is a custom element, so it drops into React, Vue, Svelte or plain HTML unchanged. Importing
the package registers `<fs-geometry-2d>`. To control registration — a different tag name, or
none at all — import the `element` subpath, which exports the class and the registration
helper without running it:

```ts
import { GeometryEditorElement, defineGeometryEditor } from '@fenix-spoon/geometry-2d/element';

defineGeometryEditor('my-profile-editor');
```

## Why SVG rather than canvas

Each control point is a real DOM node. That is the load-bearing decision in this package:

- **Keyboard operation and screen-reader labels come for free** — a `<circle tabindex="0">` is
  focusable, and each carries an `aria-label` with its index and coordinates.
- **Hit-testing is the browser's job**, not a distance-to-point loop that has to be kept in sync
  with the rendering.
- **Crisp at any zoom**, with no devicePixelRatio bookkeeping.
- **Testable in jsdom** without a canvas implementation — this package's suite drives real
  pointer and keyboard events.

Canvas remains the right tool for the *field* (see `@fenix-spoon/viewer`); it is the wrong one
for handles.

## Interacting

| Action | Mouse | Keyboard |
|---|---|---|
| Move a point | drag a handle | focus it, then arrow keys (hold <kbd>Shift</kbd> for coarse steps) |
| Insert a point | click a hollow midpoint dot | <kbd>Enter</kbd> or <kbd>+</kbd> on a handle |
| Remove a point | double-click a handle | <kbd>Delete</kbd> / <kbd>Backspace</kbd> |
| Undo / redo | — | <kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>Z</kbd> |

One drag is one undo step, and a click that moves nothing records none.

## Polygon and spline modes

`mode="polygon"` (default) sends the control points to the solver as-is. `mode="spline"` treats
them as controls for a closed **centripetal Catmull-Rom** curve and sends the sampled outline
instead — `samples` per span, default 8. The control polygon is drawn as a dashed guide.

Centripetal (α = 0.5) rather than uniform parameterisation is deliberate: uniform Catmull-Rom
overshoots into cusps and self-intersections on the uneven point spacing a hand-edited airfoil
always has, and the protocol rejects self-intersecting outlines — so the overshoot would surface
as a validation error the user cannot explain. The test suite asserts the sampled outline passes
the protocol's own polygon validator on several awkward inputs.

Note that Catmull-Rom is *not* hull-preserving: it interpolates its control points with tangents,
so a convex outline can bulge modestly outside its hull. That is expected, and bounded.

## API

| Member | Type | Notes |
|---|---|---|
| `value` | `Domain2D` | Protocol geometry. Getting builds it from the current outline; setting replaces the contents and clears undo history. Throws on geometry the protocol rejects. |
| `controlPoints` | `[number, number][]` | The draggable points. Setting them is an edit (recorded in undo history). |
| `outlinePoints()` | `[number, number][]` | What the solver receives: the control points, or the sampled spline. |
| `mode` | `'polygon' \| 'spline'` | Also settable as an attribute. |
| `bounds` | `[number, number, number, number]` | `[xmin, ymin, xmax, ymax]`; also the `bounds="…"` attribute. |
| `readOnly` | `boolean` | Also the `readonly` attribute. Hides midpoints, drops handle focusability. |
| `undo()` / `redo()` / `canUndo()` / `canRedo()` | | |
| `insertPointAfter(i)` / `removePoint(i)` | | `removePoint` refuses below 3 points, which the protocol rejects anyway. |

**Events**: `input` fires continuously while dragging, `change` when an edit is committed
(pointer released, key pressed, undo). Bind expensive work — like submitting a solve — to
`change`.

Points are always clamped strictly inside `bounds`, because the protocol requires it.

## Styling

Style through CSS custom properties on the host:

```css
fs-geometry-2d {
  --fs-outline-fill: rgba(20, 20, 24, 0.55);
  --fs-outline-stroke: #fff;
  --fs-handle-fill: #fff;
  --fs-handle-stroke: #3b82f6;
  --fs-handle-focus: #f59e0b;
  --fs-hull-stroke: rgba(255, 255, 255, 0.35);
}
```

The element is transparent, so it layers directly over a field viewer.

## License

MIT
