# 0001 — An explorable viewer without a protocol change

**Status:** accepted
**Affects:** `@fenix-spoon/viewer`, `@fenix-spoon/geometry-2d`, the wire protocol (by not changing it)

## Context

`<fs-viewer>` was a renderer: set `result`, get a coloured domain, a colourbar, iso-contours
and a value on hover. Applications built on it wanted something else — to *explore* a
result. Zoom into a suction peak, pan along a fin, frame the body rather than the far field,
pin the colour scale so two solves can be compared, overlay arrows and integrated curves,
draw a line across the domain and read the field along it, and export the picture.

None of that is physics. All of it is generic over "a scalar or vector field on a 2-D
domain". The question this record answers is **where each piece belongs**: on the wire, in
the widget, or in the application.

## Decision

### 1. No wire-protocol change. Exploration reads data already sent.

Every operation above is a function of the arrays the result already carries. `grid2d` and
`mesh2d` have carried bounds, topology and a mask since 1.0, and named vector fields since
1.1 — which is exactly the data a viewport transform, a probe, a section and a curve
integration need. Adding a `viewer` section to the protocol would have described *presentation*
in a document whose entire discipline is that it describes the domain contract, and would
have made every solver adapter responsible for how a browser draws its output.

So the protocol version stays at 1.5, no fixture changes, no adapter changes, and a result
produced by a server that predates this work is fully explorable by a viewer that postdates
it. The one thing this costs is stated in decision 3.

### 2. Integrated curves are `streamlines` / `integralCurves`, never "flow lines".

The API integrates a stationary vector field and returns the trajectories tangent to it.
That is a mathematical object with a definition that does not mention fluids. Whether a
particular curve is a streamline, a magnetic field line, or a heat-flux path depends on what
the vector *means*, and the protocol deliberately does not say: `vector_fields` is a map of
names to component pairs, and `mock.laplace2d` calling one of them `velocity` is an adapter's
choice, not a type. A viewer that named them flow lines would be asserting a physics it
cannot check.

The consumer application submitted the job. It knows it asked for potential flow, and it is
the right place for the label "flow lines" to appear.

### 3. A scalar field is never turned into a vector one. The refusal is a feature.

Given a streamfunction, a gradient, and a plausible-looking rotation, one *can* produce
curves from a scalar field. They would look like physics and would not be it — the
relationship between a scalar and a velocity is a modelling assumption that varies by
discipline and by formulation, and Fenix Spoon has no way to know which one applies.

`streamlineAvailability()` therefore returns a refusal with a sentence explaining it, and
`viewer.capabilities.streamlines.reason` puts that sentence where a page can show it. The
three refusals are distinguished, because they call for different fixes: *this result carries
no vector field at all* (ask the adapter for one), *that name is a scalar field of this
result* (pick a vector one), and *that name is not in this result*.

This is the same argument [#43](https://github.com/mandaloriat/fenix-spoon/issues/43) made
for `CapabilityFeatures` defaulting every flag to false, and
[#70](https://github.com/mandaloriat/fenix-spoon/issues/70) made for `Assumption.excludes`:
a definite no a caller can act on beats a plausible zero.

### 4. "Fit geometry" needs a geometry, and the application supplies it.

A result carries arrays and a bounding box. It does not carry an outline, and the two ways
to invent one both fail:

- **From the `grid2d` mask.** The mask marks cells that were not solved. For a `domain2d`
  obstacle that is the body — but for the `regions2d` heat sink it is the *air*, so the
  "geometry" derived this way would be everything except the object. One heuristic, two
  opposite meanings, no way for the viewer to tell which it has.
- **From the mesh boundary.** A `mesh2d` has no marker distinguishing an interior hole from
  the outer boundary; recovering one means a topological walk whose answer is still a guess
  about which loop the user meant.

So `viewer.geometry` is set by the page, which submitted the geometry and has it exactly.
`capabilities.fitGeometry` is unavailable, with a reason, until it is. The same property
drives the outline overlay, so the two can never disagree.

### 5. Navigation is opt-in; the interaction mode is explicit.

`examples/airfoil-2d/` layers `<fs-geometry-2d>` over `<fs-viewer>`. Two widgets, one box,
and a drag that both of them have a use for. A viewer that started claiming drags on upgrade
would have broken that page silently, which is why `interactive` is an attribute rather than
the default, and why what a gesture *does* is a named mode (`pan`, `probe`, `section`, `seed`,
`none`) rather than an implicit consequence of which element is on top.

The mode is the coordination primitive: an application switches the viewer to `none` while
the user edits geometry, and back to `pan` afterwards. `<fs-geometry-2d>` gained a
display-only `viewBox` in the same change, so an overlaid editor can follow the viewer's
frame — without it, "coordinate the two" was true only as long as nobody zoomed.

### 6. The mapping stays anisotropic.

`<fs-viewer>` has always stretched the domain to fill its box. Preserving aspect ratio would
be defensible in a new widget and would silently re-frame every page that already embeds this
one. Zoom multiplies both spans by one factor, so whatever distortion a page starts with, it
keeps — and no gesture ever changes the shape of what is on screen.

### 7. Overlays are painted by the application through a callback, not by a vocabulary here.

A resultant force arrow, a pin on a hotspot, an annotation: each application has its own, and
adding a name for each to this package is how a rendering library ends up with an opinion
about aerodynamics. `viewer.overlay` receives the drawing context and both transforms, so an
overlay is written in domain coordinates and survives every zoom and pan for free.

### 8. Browser tests drive Chromium over CDP rather than adding Playwright.

Three things a gesture depends on — layout, a real 2-D canvas, and pointer events synthesised
by the renderer — do not exist in jsdom, so something has to drive a browser. Playwright would,
and costs a browser download on every `npm ci` in a package whose selling point is that it is
small. Node 22 ships a WebSocket client and Chromium speaks CDP over one, so the harness in
`tests/browser/` is ~150 lines and zero dependencies. Everything provable by arithmetic stays
in the jsdom suites, which is most of it.

## Consequences

- The protocol document gains no `viewer` section, and adapters gain no rendering
  responsibilities. A 1.0-era result explores exactly as well as a 1.5 one, minus the vector
  overlays that need 1.1 data — and the viewer says so rather than drawing nothing.
- Units and display names for 2-D fields still come from the page, now per field
  (`viewer.fieldUnits`) rather than one global attribute. The page can read them from
  `GET /capabilities/{name}?sections=metrics`; the wire protocol's decision to carry units for
  curves and not for fields stands unchanged.
- Applications that want "flow lines", a geometry outline, or an application overlay must
  supply the label, the geometry, or the painter. That is more setup than a viewer that
  guessed, and it is the difference between a picture that is right and one that is plausible.
- If a future result kind carries its own boundary — a `spline2d` domain, a `step3d` import —
  decision 4 should be revisited: at that point the outline would be *given*, not inferred,
  and reading it from the result would stop being a guess.
