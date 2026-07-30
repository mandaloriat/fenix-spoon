# Changelog

Notable changes to Fenix Spoon. The **wire protocol** has a version of its own
(`MAJOR.MINOR`, currently 1.5) and its history is in
[docs/04-wire-protocol.md](docs/04-wire-protocol.md); this file records what changed for the
people who build on the toolkit, protocol bump or not.

Entries follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and the
project is pre-1.0, so the packages are versioned together and nothing here is a stability
promise yet.

## Unreleased

### Added — an explorable field viewer (`@fenix-spoon/viewer`)

`<fs-viewer>` was a renderer; it is now something an application can offer as a preview a
user actually explores. **No wire-protocol change** — every operation below reads arrays the
result already carries, so a result from an older server explores exactly as well as a new
one. The boundary between what the viewer does, what the protocol carries and what the
application supplies is recorded in
[ADR 0001](docs/adr/0001-explorable-viewer.md).

- **Viewport navigation.** Wheel and pinch zoom anchored on the pointer, drag pan, keyboard
  pan/zoom/reset, `fitDomain()`, `fitGeometry()`, `resetView()`, and a `viewport` property
  that reads and sets the visible domain window. Emits `fs-viewport-change` with the reason.
- **Explicit interaction modes** — `pan`, `probe`, `section`, `seed`, `none` — so a page that
  layers `<fs-geometry-2d>` over the viewer can say which widget owns a drag instead of
  relying on which one is on top.
- **Probe and section.** `probe()` (nearest-node, as before), `sample()` (interpolated, matching
  the server's `at_point`), and `sampleSection(start, end)` returning the same shape as
  `POST /jobs/{id}/query {"op": "section"}` — computed in the page, so no job is submitted.
  `mode="section"` draws the line by hand and emits `fs-section`.
- **Manual colour scale.** `viewer.range = {min, max}` pins it, `null` restores auto, `autoRange`
  keeps the data's own range available, and the colourbar labels a pinned scale "fixed". Two
  viewers sharing one range is what makes two solves comparable.
- **Colourbar caption** now carries the field name and its unit; `fieldUnits` and `fieldLabels`
  give per-field values, so a picker that switches between °C and W/m² stops lying.
- **Integral curves** of a vector field (`streamlines="velocity"`), RK4 on the normalised
  direction field with evenly-spaced seeding, adjustable `streamlineDensity` and explicit
  `streamlineSeeds`. They are called integral curves, never "flow lines": what a curve *is*
  depends on what the vector means, which the protocol does not say.
- **Refusals with reasons.** `viewer.capabilities` reports each tool as `{available, reason}`.
  A scalar field is never integrated into curves and a geometry is never inferred from the
  `grid2d` mask; the viewer explains why instead of drawing something plausible.
- **Geometry overlay.** `viewer.geometry` takes the geometry the job was submitted with — the
  result does not carry one — and drives both `fitGeometry()` and the outline.
- **Application overlays.** `viewer.overlay` gets the drawing context and both transforms, so
  a page draws its own arrows, pins and annotations in domain coordinates.
- **Optional toolbar.** `toolbar="pan,probe,fit-domain,reset"` renders a composable, keyboard-
  operable button row; absent, there is no toolbar and nothing changes visually.
- **Vector glyph scale** (`glyph-scale`), and glyph density that now follows the *screen*: the
  lattice cell is sized from the visible span so arrow spacing stays constant while zooming,
  and indexed from the domain origin so arrows do not crawl while panning.
- **Accessibility.** Focusable and `role="application"` when interactive, an `aria-live` probe
  readout, a canvas description that states the scale, zoom and mode, and a visible focus ring.
  Nothing animates, so there is no motion for `prefers-reduced-motion` to reduce.

### Added — `@fenix-spoon/geometry-2d`

- **`viewBox`** (attribute `view-box`): a display-only frame, so an editor layered over a
  zoomed viewer can follow it. It changes the projection only — `bounds` remains the protocol's
  domain rectangle, and changing that still re-clamps the points.

### Performance

- The coloured `grid2d` field is rasterised once per (field, colormap, range) and blitted
  thereafter; contours and integral curves are computed in domain coordinates and cached. A pan
  or a zoom re-projects rather than recomputes, and the canvas backing store is only
  reallocated when the element's size actually changes.
- `mesh2d` point location went from a linear scan over every triangle to a bucket index, which
  is what made probing and curve integration affordable on real meshes; mesh drawing culls
  triangles outside the view.
- Every density control has a ceiling: 128 glyph columns, 64 seed columns, 4000 integration
  steps per curve, 250k curve points per call, 4096 section samples.

### Tests

- New suites for the viewport transforms, integral curves against analytic fields, and the
  element's modes, events, capabilities and accessibility — plus size guards on a 20k-element
  mesh and a 175k-point grid.
- A browser gesture suite (`npm run test:browser --workspace @fenix-spoon/viewer`) driving a
  real Chromium over the DevTools Protocol, with **no new dependency**: Node 22's built-in
  WebSocket is enough, and a browser download on every `npm ci` was not a price worth paying
  in a package whose selling point is its size.
