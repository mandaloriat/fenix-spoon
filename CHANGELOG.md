# Changelog

Notable changes to Fenix Spoon. The **wire protocol** has a version of its own
(`MAJOR.MINOR`, currently 1.9) and its history is in
[docs/04-wire-protocol.md](docs/04-wire-protocol.md); this file records what changed for the
people who build on the toolkit, protocol bump or not.

*The number above is asserted, not maintained by hand: `test_the_prose_states_the_current_protocol`
reads this line — and the matching one in the README — and compares each with
`fenixspoon.protocol.PROTOCOL_VERSION`. It had drifted three minors behind before that test
existed, and the README drifted one commit after it, which is why the check covers both.*

Entries follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and the
project is pre-1.0, so the packages are versioned together and nothing here is a stability
promise yet.

## Unreleased

### Added — parameter sweeps and design of experiments (#21)

The second study kind, and the first real test of whether #48 defined *a study* or merely a
mesh ladder with ambitions. **No wire-protocol change, no new operation, no new transport
binding and no second job path**: `study.run` and `study.get` answer a sweep over JSON-RPC,
the CLI, Python and MCP exactly as they answer a convergence ladder, and the ladder itself is
untouched.

- **`kind: "sweep"`** on the `study` object. Either `axes` — a full factorial, last axis
  varying fastest, first axis the abscissa — or explicit `points`, which is how a Latin
  hypercube or any other design of experiments arrives.
- **The server does not generate randomized designs, and the reason is not squeamishness about
  a dependency.** A design generated here would break the property that *defines* a study —
  that its job list is a pure function of its object revision — unless the seed were frozen in
  the body, at which point the caller is specifying the design anyway. The caller generates,
  the revision freezes, and a DOE survives contact with the reproducibility rule.
- **A sweep's answer is a curve, and the protocol has carried curves since 1.5.** Response
  curves come back as `Series1DData`, the model a `series1d` result uses, so anything that
  draws a curve draws these — `<fs-plot>` included, when something can fetch one. Every trace
  brings its own abscissa: a point with no answer has no legal encoding against a shared axis,
  where it would have to become a zero or shift every later value onto the wrong angle. A
  partial sweep says exactly what it knows.
- **Bounded at 64 points, and the bound is on the answer rather than on the queue.** Every
  point goes through `job.submit` and meets the cell budget and the quota like any other job;
  what nothing else governs is that a grid *multiplies* — four axes of four values is 256
  solves from a body that fits on one line. The first axis must carry at least two values,
  which makes a curve possible and, by arithmetic, makes the trace count unable to exceed the
  series ceiling: a legal sweep always has an encodable report.
- **The acceptance case is a lift polar**, not the camber sweep #21 asked for, and both halves
  of that sentence moved for a reason. Camber is a property of the *geometry*, which the
  workspace stores as an explicit polygon, so a camber sweep is a sweep over geometry
  references; `alpha` is a capability parameter that rotates the free stream, which the
  adapter's own parameter description had already noted is what lets a sweep reuse one domain.
  And "lift **proxy**" is gone — #68 made `c_l` a real number. The test asserts the physics
  rather than a golden value: potential-flow lift is linear in alpha, so equal steps in angle
  must give equal steps in `c_l`, which also catches every point collapsing onto one cached
  job — the failure a sweep is uniquely exposed to.
- *Two things found by running it rather than by testing it.* A sweep of two named metrics
  rendered a twelve-column table of six: `metrics` is documented as the column list, the
  read-out honoured it and the rows never had. Invisible on a ladder, where the read-out is
  what a caller reads; obvious the moment the table *is* the answer. **Rows now carry the
  columns the study asked for** — a caller who wants everything omits the list, as before.
  And the CLI's generic table dropped every map-valued column, so a lift polar printed as six
  rows of job ids with neither the angles nor the lift in sight. **A column of flat maps now
  spreads into `parent.key` columns**, which is still generic — no operation is named in the
  renderer — and is the difference between a table and a header.
- **Provenance now records the whole override map** (`variation`) rather than a bare
  `variation_value`. A grid point is not one value, and a value recorded without its parameter
  was already half a sentence when only the ladder existed.
- *Not built, deliberately: the HTTP surface* `POST /api/v1/sweeps`. A study is a workspace
  object and the workspace has no HTTP binding by #44's decision; binding sweeps alone would
  design half an object API through a side door. That is the same question #44 deferred.

### Added — a curve widget (`@fenix-spoon/plot`)

The one extension `docs/04-wire-protocol.md` had listed as unfinished since 1.5: the protocol
carried curves and every consumer wrote its own plot. **No wire-protocol change** — `<fs-plot>`
draws numbers that have been on the wire since `series1d` landed, the same relationship the
explorable viewer has to 1.1.

- **`<fs-plot>`**, a fourth browser package. Takes a `JobResult` of either kind — a `series1d`
  payload *is* the curve set, a field result carries its curves beside `data` — or a
  `Series1DData` directly. Axes with round ticks chosen from {1, 2, 5} × 10ⁿ, a legend, a
  pointer readout that resolves in *screen* space (a data-space distance would add metres to
  pascals), linear or log scales, and per-trace abscissae honoured, which is what an airfoil's
  two surfaces need.
- **`invert-y` is an attribute, never an inference.** A `C_p` is drawn with suction upwards and
  it would be easy to notice a trace called `cp_upper` and flip the axis — the class of guess
  [ADR 0001](docs/adr/0001-explorable-viewer.md) records the viewer refusing. A name is not a
  quantity, so the page says so and the widget does not decide.
- **One y axis, stated.** Where traces disagree about units — a magnitude and a phase — the axis
  goes uncaptioned and the legend carries each unit, rather than labelling the axis with the
  first trace's unit and being wrong about every other curve on it.
- **A separate package, not a second element in the viewer.** They share a canvas and nothing
  else, and folding them together would make every page showing a temperature map carry axis
  code it never calls — against the property `@fenix-spoon/viewer` exists to keep.
- **The airfoil demo plots its surface pressure**, which it has computed since #68 and been
  discarding. The saw teeth are the mock's staircased body rather than the plot, and the page
  says which, because a first-time visitor would otherwise read a faithful drawing as a bug.
- *Three things found while building it, two by the tests and one in review.* A zero-width-domain
  guard inside the projection was unreachable — the domain repair upstream already prevents one —
  so two defences that could have disagreed became one. The accessible description was skipped
  whenever the canvas had no 2D context, because it sat after the early return; the name of an
  element is a function of its data, not of whether the environment can rasterise. And **padding
  a log axis additively is wrong in a way nothing reports**: a residual history from 1e-1 down to
  1e-7 had 0.005 subtracted from its lower bound, which is negative, so the domain repair lifted
  it to the log floor and six decades silently became twelve. Padding is now a fraction of the
  *decades* on a log scale, decided in one place for both axes — the version that special-cased
  x at the call site and left y additive is what produced it.
- *Paints are coalesced to one per frame*, the `render()` / `draw()` split `<fs-viewer>` uses. It
  matters more here than there: a pointer crossing a dense curve resolves a different nearest
  point many times per frame, and each one repainted the axes, the ticks and every trace. The
  accessible description stays **synchronous** — deferring it to a frame would be a milder
  version of the mistake of deferring it to a rendering context — which is affordable because
  the resolved traces are now cached rather than re-paired on every pointer sample.

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
