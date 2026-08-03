# Changelog

Notable changes to Fenix Spoon. The **wire protocol** has a version of its own
(`MAJOR.MINOR`, currently 1.9) and its history is in
[docs/04-wire-protocol.md](docs/04-wire-protocol.md); this file records what changed for the
people who build on the toolkit, protocol bump or not.

*The number above is asserted, not maintained by hand: `test_changelog_states_the_current_protocol`
reads this line and compares it with `fenixspoon.protocol.PROTOCOL_VERSION`. It had drifted three
minors behind before that test existed, which is the whole argument for it.*

Entries follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and the
project is pre-1.0, so the packages are versioned together and nothing here is a stability
promise yet.

## Unreleased

### Added — two more physics, and the three protocol gaps they exposed

Each capability below was added for its own sake and each one found something the protocol
could not say. That order matters: the extensions are answers to a solver that needed them,
not a specification written forward.

- **Linear elasticity** (`mock.elasticity2d`, `dolfinx.elasticity2d`) — the first physics with
  a **vector** unknown, and the first whose boundary conditions the geometry could not
  express. Validated against closed forms: the Kirsch stress concentration around a circular
  hole and a cantilever's `PL³/3EI` tip deflection. Nodal stress is area-weighted, in one
  shared helper, so the pair cannot average differently. (#81)
- **Transient heat conduction** (`mock.transient_heat2d`, `dolfinx.transient_heat2d`) — the
  first capability that answers *when* rather than *what at rest*, and the one that found the
  protocol had no time dimension at all. Volume-weighted mean and time constant on the
  unstructured mesh. (#82)

- **Protocol 1.6 — a metric declares what it is taken over.** `MetricSpec.over` is `payload`
  (the default, and what every steady capability already meant) or `run`, for a quantity only
  the adapter can supply: a peak over a transient's history, a time to reach a level. The
  combination that lies — a `run` metric declared as a reduction of the payload — is refused
  rather than computed. Additive, and a discovery-payload change, so the SDK carries the
  version and models nothing new. (#86)
- **Protocol 1.7 — an artifact knows which instant it holds.** `ArtifactRef.t`, and a derived
  `frames` list on the result envelope: the time index of a transient *is* the artifacts
  carrying an instant, in time order. Deliberately **not** a new result kind — the field
  history crosses as references like every other large thing. Deriving the index from the
  files rather than storing it beside them is what makes a frame naming a file the result does
  not serve unrepresentable rather than merely tested for. Capped at `MAX_FRAMES`, enforced
  where the artifact is registered rather than where the envelope is built, because the
  compact levels and the local API never build an envelope. The SDK *does* model this one:
  `ArtifactRef.t` and `FrameRef` are part of the result envelope it already types. (#86)
- **Protocol 1.8 — a geometry can name pieces of its own boundary.** Optional stable
  `point_ids` on `polygon2d`, and a `boundaries` list whose selectors come in three families:
  `part` (topological — `outer`, `obstacle`, `region:<name>`), `points` (by stable id), and
  `near`/`box`/`all_of` (geometric). Additive and empty by default; every adapter shipped
  before it puts its conditions where the outer/obstacle split implies them.
  *Ids and predicates are both here because they follow different things — an id follows the
  shape through an edit, a predicate follows the space — and neither can express the other
  honestly.* Resolution produces a **predicate over coordinates**, `f(x) -> bool` for points
  shaped `(2, N)`, which is exactly what `locate_entities_boundary` takes, so a FEniCSx
  adapter passes it through and a mock gets a NumPy mask from the same call. A selector that
  names real vertices spanning **no edge** is refused: a boundary that validates and then
  matches nothing is the one outcome the design is arranged to prevent. What happens *on* a
  named boundary is a load case, and lands separately. (#85, first half)
- **Protocol 1.9 — a load case says what happens there.** `conditions` on a job request, and a
  `load_case` workspace object a design references — its own object type, because reusing one
  set of restraints and loads across a family of shapes is the whole argument for it not being
  a block inside the design. Values stay an open dict of scalars, so a new physics is not a
  protocol change; what a capability *declares* is the keys it reads, discoverable as a tenth
  `capability.describe` section.
  ***An unread condition is an error where an unread material key is not***, and that
  asymmetry is the point of the release. An ignored material key leaves a property at its
  default. An ignored condition leaves a clamp out of the assembly, and the solve converges and
  answers a different problem with no symptom. So three refusals, all at submit and all in the
  shared error corpus: `UnknownBoundary` for a name the geometry does not declare,
  `UnknownConditionKey` for a key no capability reads, and `ConflictingConditions` for two of a
  design's load cases setting one key on one boundary — refused rather than resolved by list
  order. The conditions go into the cache key, since two designs differing only in what is
  clamped are two different solves.
  *Both elasticity adapters read them*, falling back to `fixed_edge`/`load_edge` when no load
  case is supplied. **Precedence is total, never a merge**: a caller who named every boundary
  must not inherit an invisible clamp from a default it never set. The FEniCSx half tags facets
  built from the same predicate the mock consumes as a NumPy mask, so the pair cannot apply one
  load case to different edges — and writing it turned up a real bug in the 1.8 resolver, which
  did two-row arithmetic on the `(3, N)` coordinates dolfinx passes.
  *Verified in the FEniCSx CI job*, where a load case reproduces the edge shorthand to 1e-9 and
  a plate hangs from its own hole. (#85, second half)

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
