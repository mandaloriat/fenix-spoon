# 0003 — The axis label belongs to the kind, not to a field

**Status:** accepted — *implemented as protocol 1.13,
[#100](https://github.com/mandaloriat/fenix-spoon/issues/100)*
**Affects:** the `axisymmetric2d` geometry kind, `@fenix-spoon/client`, `<fs-viewer>` and
`<fs-geometry-2d>` (by not changing them)

## Context

Protocol 1.13 adds `axisymmetric2d`: a meridian half-section of a body of revolution, whose
horizontal coordinate is a **radius** and whose vertical one is an axial position. Every other
geometry in this protocol is drawn on x and y, and every consumer that draws one assumes so.

That assumption is now wrong for one kind out of three, and it is wrong in the way that
teaches: a meridian section drawn on axes marked *x* and *y* looks like a plane slice of
something, which is exactly the mistake the kind was added to prevent. The picture would be
consistent with a payload that means something else.

So the question is not whether the axis label matters. It is **where the claim lives**, and
[ADR 0001](0001-explorable-viewer.md) makes that question sharp rather than obvious, because it
answers two adjacent questions in opposite directions:

- decisions 2 and 3 say a **viewer must not read a quantity off a name**: integrated curves are
  `streamlines`, never "flow lines", and a scalar field is never turned into a vector one,
  because whether `psi`'s gradient is a velocity is a modelling assumption the protocol cannot
  check;
- the consequences say **units and display names come from the page**, per field, because they
  are presentation and the application knows what it asked for.

Read one way, the first argues the geometry should carry its axis meaning explicitly rather than
let a consumer infer it. Read the other way, the second argues an axis label is a display name
and belongs to the page. Both readings have been used to justify a field on the payload, and
they cannot both be describing this case.

## Decision

### 1. The kind carries the claim. `type: "axisymmetric2d"` *is* the statement that the first coordinate is a radius.

There is no `axis_labels` field, and the absence is the decision rather than a deferral.

What distinguishes this from the "flow lines" case is that it is **checkable and checked**.
Whether a streamfunction's gradient is a velocity depends on a modelling assumption living
outside the payload; whether the first coordinate is a radius is enforced *inside* it —
`rmin >= 0` is validated, a region may lie on r = 0 only because that edge is an axis, and the
`r` weight in the integrand is what makes a solver's answer the answer for a revolved body. A
consumer reading "radius" off this discriminator is reading a fact the server refuses payloads
over, not guessing a physics.

That is also the difference from a *name*. ADR 0001's rule is about names chosen freely by an
adapter or a caller — `velocity`, `cp_upper`, `region: "core"`. A discriminator is neither: it
is a closed protocol value, and the protocol is the document that says what it means.

### 2. A field a caller can set would be a claim a caller could get wrong.

The alternative considered first was `axis_labels: ["r", "z"]` with that default. It fails on
its own terms: nothing can validate it. A payload labelled `["z", "r"]`, or `["x", "y"]`, or
`["Radius", "Height"]` is as legal as the right one, and a viewer that trusted it would draw a
section transposed with complete confidence. The kind would then say one thing and the field
another, with no way to tell which the author meant.

A payload that can lie about its own coordinates is worse than one that says nothing, because
the lie is the part a consumer would use.

### 3. Nor a derived field, and the reason is where geometry is *stored*.

The next alternative was a computed field — `axes`, derived from the discriminator, on the wire
but not settable. It cannot disagree with the kind, which answers decision 2's objection.

It fails on a different one. A `geometry` workspace object stores **the body as the caller sent
it** (`workspace.VALIDATED` validates; the store keeps the bytes). So a derived field would be
present in a payload serialised from the model and absent from the same geometry read back from
`GET /objects/geometry/{id}` — and a consumer would have to fall back to reading the
discriminator anyway. A field that a consumer must not rely on is worse than no field: it makes
two code paths where there is one fact.

### 4. The mapping lives in the SDK, once, so no consumer hard-codes strings.

`axisLabels(geometry)` in `@fenix-spoon/client` returns `['r', 'z']` for an axisymmetric
section and `['x', 'y']` for the other two. One function rather than a convention repeated per
page, for the same reason `spanned_edges` is one function on the server: two implementations of
one mapping is how they come to disagree, and the disagreement would be silent — a page drawing
the right picture beside a page drawing the wrong one, both looking finished.

The widgets are unchanged. `<fs-viewer>` receives a **result**, which carries arrays and a
bounding box and no geometry at all — ADR 0001 decision 4 is the same finding from the other
side — so the label reaches a picture the way every other label does: the page sets it, and now
has somewhere correct to get it from.

## Consequences

- A meridian section and a plane one are told apart by the discriminator alone, on the wire and
  in every consumer. There is one fact and one place it lives.
- A page that draws an `axisymmetric2d` geometry must ask `axisLabels` (or read the type) rather
  than assume x and y. A page that does neither draws the wrong picture — which is a real cost,
  and the one this record accepts in exchange for not shipping a field that could be wrong.
- `<fs-geometry-2d>` still edits points on unlabelled axes. Labelling its axes is a widget
  change this record does not make, and the mapping above is what it would use when someone does.
- If a later kind arrives whose coordinates are **not** determined by its discriminator — a
  general curvilinear section, say, or a `step3d` import carrying its own frame — this decision
  does not extend to it, and the field rejected here would become the honest answer. The test is
  the one applied above: can the server refuse a payload whose coordinates are not what it says?
