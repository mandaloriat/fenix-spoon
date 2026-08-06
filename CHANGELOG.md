# Changelog

Notable changes to Fenix Spoon. The **wire protocol** has a version of its own
(`MAJOR.MINOR`, currently 1.15) and its history is in
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

### Added — protocol 1.15: adapters can come from somewhere else (#105)

ADR 0005 said breadth is bought with adapters and depth with protocol, and named the reason
that split did not work: there was no supported way to load an adapter that does not live in
this repository. `solvers/__init__.py` imported its own by hand, there were no entry points,
and the solver guide's advice — *"import the module once at startup"* — pointed at a place
that did not exist, because nothing an operator controls runs before the registry is
populated. So every new physics had to land here, in the thing that is supposed to refuse
recipes.

- **Two sources, one loader.** An entry point in the `fenixspoon.solvers` group, which is how
  an installed distribution says it carries adapters; and `FENIXSPOON_SOLVER_MODULES`, a
  comma-separated list of module paths, for an adapter that lives in an application's own tree
  and is not worth packaging. Both load **after** the built-ins, which is the whole of the
  shadowing rule — a plugin claiming `dolfinx.poisson` loses and cannot win by importing first.
- **A failure is data, not a traceback and not a silence.** A stranger's broken import must not
  take down a server whose other thirteen capabilities are fine, and a name collision is the
  plugin author's bug rather than the operator's outage — but swallowing either leaves a
  missing capability with no reason attached, which is the failure every other declaration here
  exists to prevent. So each source is caught, classified and reported.
- **`plugins` on `environment.inspect`**, which is the protocol half and the whole of the bump.
  A caller that does not find a capability could not previously tell *"the operator did not
  install it"* from *"it is installed and raised `ImportError: no module named slepc4py`"* —
  one is a configuration and the other is a broken deployment. `status` is `loaded`, `failed`
  with the exception's text, or `disabled`; the last exists because *off* and *none installed*
  are different answers, and an operator debugging a locked-down deployment deserves to be told
  which. `capabilities` may be non-empty on a failure: a module that registers two adapters and
  raises between them really did add the first, and the registry has no removal.
- **What a plugin cannot do is add a geometry kind, a result kind or a field.** An adapter
  chooses from the vocabulary the protocol already has, or its author comes here and argues for
  more. Otherwise ADR 0005's admission test would have a back door and the protocol would grow
  by whoever shipped a package rather than by whoever won an argument.
- **The result cache already covered a stranger, and now there is a test saying so.** `version`
  and `requires` are declared per adapter and the content address reads both, so a third-party
  adapter that changes its maths and bumps its version stops matching its own cached results.
  Nothing in `cache.py` changed — but *"it should already work"* is how a cache serves a stale
  answer forever.
- `docs/start-write-a-solver.md` gains a **Getting it loaded** section with a working
  `pyproject.toml` fragment, and the front page's *"the server needs no changes to accept it"*
  is true for the first time.

Not in scope, and refused rather than deferred: loading an adapter from a path, a URL or an
upload. That is arbitrary code execution wearing a feature's clothes, and it gets the same
answer as `run_python`.

### Documentation — the test a protocol addition has to pass before it is written

No code changed. [ADR 0005](docs/adr/0005-thin-about-physics-thick-about-claims.md) writes down
the criterion the last four records were applying case by case, and which nothing stated: the
protocol is **thin about physics** — it adds no semantics FEniCS does not have, and no UFL travels
in either direction — and **thick about claims** — what a capability asserts, refuses and lets a
caller check is contract, and being sparing there is a defect rather than a virtue.

Where the two pull apart, the question is one line: *is there something a server can now refuse,
or a consumer now check, that would otherwise live only inside an adapter's source?* If yes it is
contract; if it only makes a case more convenient to express, it is a recipe and belongs in an
adapter.

The record works that against the case that looks like a counter-example. `axisymmetric2d` **does**
add semantics FEniCS lacks — a dolfinx mesh has an `x[0]`, not an `r`, and the revolution lives
only in the weak form — and it passes anyway, because the semantics is what makes `rmin >= 0`,
the on-axis relaxation and the plane solver's `422` expressible at all. It was added in order to
be able to say no. A `beam1d` added because beams are common would not be.

Consequences that are already visible: `spline2d` and `step3d` stay planned, but the wire protocol
now says the question they have to answer is what a server could refuse once it knows the outline
is a spline, not whether someone wants to draw one. And decision 5 names a contradiction rather
than fixing it — breadth is supposed to be bought with adapters, but `solvers/__init__.py` imports
its own by hand and there are no entry points, so today every new physics has to land *in this
repository*. Third-party adapter loading stops being a convenience and becomes the outlet this
criterion depends on.

### Added — protocol 1.14: an eigensolve, and the index it needed (#101)

*"Where does it resonate?"* was the one row of `series1d`'s own table of purposes with no
producer behind it. `mock.modal2d` and `dolfinx.modal2d` are that producer, and the protocol
change they needed is one field.

- **Natural frequencies and mode shapes of a plane structure**, as the generalised
  eigenproblem `K phi = omega^2 M phi`. The NumPy half lumps the mass and factorises densely
  with `eigh`; the FEniCSx half uses the consistent mass matrix and SLEPc with
  shift-and-invert. They err in opposite directions, which is what makes cross-validating them
  informative rather than circular.
- **Unrestrained structures work, and are the interesting case.** A floating structure has a
  singular stiffness matrix and exactly three rigid-body modes in the plane. They are counted
  and reported rather than filtered — `rigid_body_modes` is a declared metric — because the
  count is how a caller tells a sound model from one held somewhere nobody intended. The
  FEniCSx half takes its spectral shift just below zero for this reason, and the shift scales
  with the structure rather than being a number.
- **Checked against answers from outside this repository**: the tabulated roots of a prismatic
  beam, 1.875 for a cantilever's first mode and 4.730 for a free-free bar's, both matched to a
  few percent and from the expected side.
- **`mode` on an artifact, and a derived `modes` on the result** — 1.7's time index applied to
  an ordering that is not time. The frequencies need nothing new: they are a `series1d` whose
  abscissa is the mode number, the same number the shapes are indexed by, so the two are
  joined by a value rather than by list position.
- **A mode number is not an instant**, and refusing to put one in `t` is
  [ADR 0004](docs/adr/0004-a-mode-is-not-an-instant.md). It would have worked — the first
  draft did exactly that and no test failed — and it would have made a viewer's time slider
  read "mode 3" as three seconds while `frames` silently listed modes. The two fields are
  mutually exclusive on one file, refused at registration rather than at serialisation.
- **A modal load case is an elasticity load case minus the loads**, sharing the very
  `ConditionSpec` objects rather than a second spelling: one set of restraints holds a bracket
  for a stress check and for its modes. A traction is refused, because an eigensolve has no
  load and ignoring the key would answer a different question.

### Added — protocol 1.13: `axisymmetric2d`, and the slice it stops you solving by accident (#100)

A third geometry kind: a meridian *(r, z)* half-section of a body of revolution, with two
adapters that read it. Additive — a new member of a discriminated union — and the first new
geometry kind since `regions2d`.

- **The kind is a refusal, not a new shape.** A meridian section could always be sent: a
  `regions2d` with bounds starting at zero and *meant* as r. Nothing validated it, nothing told
  a viewer the horizontal axis was a radius, and nothing stopped it reaching a plane solver —
  which accepted it and quietly solved a slice through an infinite prism. The failure mode was a
  wrong number, not an error. `geometry_types` and the `422` behind it could always have
  prevented that; what was missing was a kind to name.
- **It is `regions2d`'s filled-region model, sharing the rules rather than restating them.**
  Painter's-order nesting, refusal of properly crossing outlines, the open scalar `material`
  dict — one function in `geometry.py` enforces them for both kinds, so a payload cannot be
  legal as a plane section and illegal as a meridian one.
- **Two rules are its own, and both come from what the coordinates mean.** `rmin >= 0`, because
  a negative radius is a plane section that has been mislabelled. And a region may lie *on*
  r = 0 when the section reaches the axis: a solid shaft is bounded by the axis, and the
  strictly-inside rule would otherwise make the commonest axisymmetric shape unrepresentable.
  The relaxation is the axis alone — a section starting at r = 19 mm has a truncation there.
- **A section that does not reach the axis is legitimate**, and it is the motivating case: the
  capacitive sensor's annular electrode sits 20 mm out. Solvers must not assume the axis is in
  the domain, and the conformance corpus carries a section that proves the point.
- **The axis needed no mechanism.** In an r-weighted weak form the boundary term carries the
  same `r` and vanishes at r = 0, so the natural symmetry condition is what an adapter gets by
  doing nothing there. Naming the axis costs nothing new either: protocol 1.8's `near` selector
  already reaches it, and every selector family resolves against the new kind unchanged.
- **`mock.electrostatics_axi2d` and `dolfinx.electrostatics_axi2d`**, following the rule this
  repository states for the domain contract — the capability first, the protocol change second.
  Axisymmetric electrostatics with the `r` weight in the integrand and in the volume element, so
  the capacitance comes back in **farads for the whole revolved body** rather than per unit
  depth. Electrodes are regions carrying a `voltage` material key, or named boundaries carrying
  a `voltage` condition. Checked against a closed form from outside this repository — a coaxial
  section's `2πεL/ln(b/a)`, which is a form the `r` weight cannot be dropped from — and against
  the motivating sensor, where the answer sits above its parallel-plate estimate by the fringe
  and the chamfer.
- **The axis label is the kind's, not a field's** —
  [ADR 0003](docs/adr/0003-axisymmetric-axis-label.md). A viewer cannot infer that a coordinate
  is a radius, and drawing a meridian section on axes marked x and y teaches the wrong picture;
  but a settable `axis_labels` would be a claim nothing can check, and a *derived* one would be
  absent from exactly the payload a viewer usually reads, since the workspace stores a geometry
  body as it was sent. So the discriminator carries the claim — it is the one statement the
  server refuses payloads over — and `axisLabels()` in the SDK is the single place the mapping
  lives.

### Changed — protocol 1.12: optimizations over HTTP, and `optimize.run` stops blocking (#22)

[ADR 0002](docs/adr/0002-workspace-over-http.md) decision 4, the last of its four pieces, and
the one carrying a genuinely new mechanism. **Additive on the wire and not additive in
behaviour**, which is why this entry is *Changed*: one shipped operation answers differently.

- **`POST /api/v1/optimizations/{id}/run` → `202` and `GET /api/v1/optimizations/{id}`**, with
  `?revision=` on both, the same object-that-runs shape as a study. The receipt names no jobs,
  and the absence is structural rather than an omission: a search chooses each point from the
  last answer, so a receipt promising job ids would be promising something unknowable.
- **`optimize.run` returns a receipt on every transport instead of the finished trajectory.**
  It blocked for the whole search — minutes of solving — which is fine on a pipe a child
  process owns and impossible over HTTP. The first draft of ADR 0002 concluded the two
  transports would have to diverge; they do not, because `job.submit` had already settled the
  question: **waiting is a convenience of the caller-facing layer, not part of the operation.**
  So `fenix-spoon optimize run` and `local.wait_for_optimization` wait, with `--detach` to skip
  it, exactly as `job submit` and `study run` have always done, and no transport is special.
- **The search continues as a task owned by the server process** — the first thing here that is
  neither a job nor a request. It is a *driver, not state*: the state is the jobs, so killing
  the server mid-search costs a loop and nothing else, and a re-run recovers the whole
  trajectory from the cache. A second `run` while one is in flight joins it (`started: false`)
  rather than starting a rival.
- **`running` on the report**, and it says what it means: whether a search is in flight *in the
  process answering this call*. Behind two API replicas a search driven by the other one reads
  as `false`. That is a statement this process can substantiate, where `true` would not be.
- **One thing decision 4 did not foresee, and the fix is recorded rather than smoothed.**
  Moving the report out of `run`'s reply made `stopped: "stalled"` — and the refusal message
  beside it — unreachable everywhere, because a replay cannot recover them: a submission the
  server *refused* leaves no job behind. So the core keeps a small process-local memory of the
  last search's tail, beside `running` and on the same terms, applied only when a replay stops
  at exactly the point the search did. A process that did not run the search still says
  `incomplete`, and a test builds a second core over the same data directory to prove it.
- *Three documents said this could not be done, and all three are corrected in place.* The
  roadmap's #22 entry argued `optimize.run` could take no `--detach` because a search has no
  moment at which the work is accepted and not yet done; the JSON-RPC guide called it "the only
  method whose duration is the work"; `local.run_optimization`'s docstring said a search *is*
  the waiting, so there was nothing to hand back. The roadmap's sentence is struck through
  rather than deleted.

### Added — protocol 1.11: studies over HTTP, and a sweep in the browser (#21)

[ADR 0002](docs/adr/0002-workspace-over-http.md) decision 3, the third of its four landable
pieces, and the half of [#21](https://github.com/mandaloriat/fenix-spoon/issues/21) that has
been waiting since the sweep kind shipped. **Additive**, and it adds no models: both routes
speak shapes four other transports have carried since #48.

- **`POST /api/v1/studies/{id}/run` → `202` and `GET /api/v1/studies/{id}`.** Neither takes a
  request body, which is the whole argument. #21 sketched `POST /sweeps` with the grid in the
  body; a sweep posted that way is a computation with no identity — it cannot be pinned,
  re-read next week, re-run into the cache, or handed to a colleague as an id. So the study is
  created through the object routes like anything else and these two act on it. Change the
  grid by patching the study.
- **`?revision=` on both study routes**, the same way and for the same reason the object routes
  have it. Not decoration: *"you cannot pin it"* is the first thing ADR 0002 holds against a
  sweep posted as a request body, so a binding that could only ever act on the head would
  refute the argument it was built on. Re-running a pinned study is free — its job list is a
  pure function of its revision, so every job is already in the cache.
- **Running a study twice is idempotent, including mid-flight, and nothing was added for it.**
  Every variation goes through `POST /jobs`, and the result cache has matched `queued` and
  `running` jobs since 1.4 — the mechanism that stops two identical submissions from racing.
  This was one of two questions ADR 0002 left open; it turned out not to be a decision, and
  the record now says so.
- **The SDK gains `runStudy` and `studyReport`**, plus the workspace methods' return types —
  and no new curve type. A sweep answers with `Series1DData`, which the client has typed since
  1.5, so `<fs-plot>` draws a study report with no adapter in between. That was the reason for
  choosing the model a release before anything could fetch one, and it is now checked rather
  than asserted.
- **A fourth demo: [a lift polar](docs/gallery.md#lift-polar-a-parameter-sweep-driven-from-the-page).**
  The first page that keeps its geometry, design and study on the server under stable ids and
  names them instead of resending them. Sweep once and every row reads `done`; sweep again and
  every row reads `cached`. It also demonstrates, by having got it wrong first, why the angle
  grid is a **step** rather than a point count: with N points between two ends, widening the
  range moves every angle by a fraction of a degree and a widening that should have cost one
  solve costs seven. Anchored at `from`, extending the range reuses everything already
  computed — 6 of 7 free, in the screenshot.
- **Three refusals that were missing, all found by a review of #21 asking about shapes nobody
  had sent.** `{"design": ..., "params": {...}}` was accepted by `POST /api/v1/jobs` and the
  overrides were then discarded — the exact "a job whose inputs are not what the caller thinks"
  the both-forms rule exists to prevent, arrived at from the one direction unchecked; it is a
  `422` now. The SDK's `validateJobRequest` had gone through the whole of 1.10 still demanding
  a `solver`, so a caller who validated a design-form request before sending it was told the
  protocol's own new shape was invalid; the shared corpus could not catch that, having had no
  design-form case in it, and now has four. And `runStudy`/`studyReport` used only the id half
  of a reference, so `geometry:g-1` reached `/studies/g-1/run` and a pinned `study:s-3@2` ran
  the head — both are refused or carried client-side now.
- **`JobRequest` is a union in the SDK**, `InlineJobRequest | DesignJobRequest`, where it had
  been one interface with every field optional — which typed `{ params: {} }` as valid. A
  client type that permits what the server refuses has stopped describing the protocol.
- *One stale claim removed from the code.* `SweepReport`'s docstring argued for sharing
  `Series1DData` and then noted that no browser could fetch one. The note is kept and marked
  as having paid off rather than deleted: a design bet that came off is worth more in the
  record than a sentence that only ever described the present.

*Not in this release, and named in the roadmap:* the optimization run routes and the
background task that makes a search pollable over HTTP (ADR 0002 decision 4 — landed in 1.12).

### Added — protocol 1.10: the workspace over HTTP (ADR 0002)

The decision #44 deferred twice, designed in [ADR 0002](docs/adr/0002-workspace-over-http.md)
*before* any of it was built, and implemented from that record. Both remaining M5 items were
waiting on it. **Additive**: every route is new and nothing that worked at 1.9 changed.

- **Objects as a resource collection.** `POST /api/v1/objects/{type}`,
  `GET|PATCH /api/v1/objects/{type}/{id}`, `?revision=` for a pinned read, `/revisions` for the
  list, `GET /api/v1/objects` to list without bodies. **A reference is not a path segment**:
  `{type}/{id}` is the pair `geometry:g-12` decomposes into, so no page percent-encodes a colon
  and the server rebuilds the canonical reference from its own path parameters. The revision is
  a query parameter because it is a *view* of an object, not a sub-resource with an identity.
- **`POST /api/v1/jobs` accepts `{"design": "design:d-18"}`**, and this is the payoff rather
  than the routes. A browser that inlines its geometry resends the whole outline every
  iteration, can never hit the result cache on an unchanged design, and — the part that shows
  only when you ask — can never say *which design revision* a picture came from, because
  provenance can only name revisions the caller submitted. Sending both forms is a `422`, the
  same refusal `job.submit` has always given over JSON-RPC.
- **`FENIXSPOON_MAX_OBJECTS`**, a quota that did not exist because creating an object was free
  and local. Refused at create, with no `Retry-After`: nothing about waiting deletes an object,
  where an hourly job window genuinely does roll.
- **Per-principal isolation is proved rather than added.** It was already written and already
  correct — every workspace method takes an owner, and somebody else's reference has always
  returned "not found" rather than "forbidden". What it had never been is *falsifiable*, since
  on a single-principal machine every caller is the same caller. There is now a negative test
  per verb, including the one that is not obvious: writing a design that references another
  principal's geometry is allowed (a reference is a string) and *solving* it is a 404.
- *The design pass paid for itself twice, and both corrections are in the record.* A review
  round removed its most expensive decision — a deliberate divergence between transports for
  `optimize.run`, which dissolved once `job.submit`'s existing "waiting is a convenience, not
  an operation" pattern was noticed. And writing the code corrected it again: a URL whose
  halves disagree is a `422` from the reference parser, not the `404` the record promised,
  because `parse_ref` has refused a type/prefix mismatch since #44.
- **1.10 is the first version where "the version is a string, not a number" stops being
  enough.** As a float `1.10` sorts below `1.9`; as a *string* it also sorts below `"1.9"`,
  because `'1' < '9'` at the third character. A draft of the ADR proposed pinning
  `"1.10" > "1.9"` as a test — false, and written by someone who had read the warning. The
  wire-protocol document now gives both halves, the corpus carries a `1.10` case, and the SDK
  suite pins both traps beside the comparison that survives them: split on the dot, compare
  the halves as integers, which `checkProtocolCompatibility` has always done.

*Not in this release, and named in the roadmap:* the study and optimization run routes
(decision 3 — landed in 1.11 for studies) and the background task that makes a search pollable
over HTTP (decision 4).

### Added — optimization: choosing the next point (#22)

The far side of the boundary #48 drew. A study **enumerates** a variation space — given
`study:s-1@2` you can say which solves it implies without running any of them — and a search
cannot promise that, because its third point depends on what the first two answered. So
`optimize.run` and `optimize.get` are separate operations rather than a third study kind, and
`optimization` is the seventh workspace object type.

- **`optimization`**: a design, one parameter, a bracket, and an objective — a declared metric
  to `minimize`, `maximize` or hit as a `target`. All three become one minimisation internally,
  so the method never learns which the caller meant. An objective the capability does not
  declare is refused, for the reason a study refuses an unknown column.
- **There is still no run record, and the distinction that allows it is worth keeping.** An
  optimizer is not *predictable*; it is **reproducible**, and reproducibility is what storage
  would have been for. The method is a pure function from the answers so far to the next point,
  so a second run replays the identical sequence and every evaluation is a content-addressed
  cache hit — which is also how `optimize.get` recovers a trajectory nobody wrote down, by
  replaying the method and resolving each point by cache key. Where the objective is not
  reproducible, the recorded job `inputs` answer instead: what is lost is the free second run,
  not the record.
- **Ask–tell rather than a callback, which is also why not `scipy`.** `minimize_scalar` takes a
  function and calls it, inverting control against a job service that is asynchronous by
  construction — a thread blocked per search, and the trajectory inside somebody else's stack
  frame where neither `optimize.get` nor a caller polling mid-run can see it. The method here
  never touches a solver, a job or a workspace, and is tested against arithmetic with no
  fixture at all.
- **`optimize.run` waits**, and it is the only operation that does. `study.run` hands back
  every job id at once because it knows them all; a search has no moment at which the work is
  accepted and not yet done, so there is no `--detach` either.
- **An evaluation with no answer stops the search.** A study tabulates what it has and marks a
  rung refused; here the next point is a function of the missing value, so there is nowhere to
  continue from — and a "best" out of the points before it would present a truncated search as
  a finished one.
- **The report carries a bracket beside the best point.** The best evaluation is where the
  lowest value was *seen*; the bracket is where the minimum is *known to be*. A search stopped
  by its budget has a bracket wider than its tolerance, and reading only the best point would
  take that for a located answer. The convergence history comes back as `Series1DData` — the
  shape protocol 1.5 named "a sweep, a convergence history" when it was introduced.
- **The acceptance case is the zero-lift angle, checked against the sweep rather than against a
  constant.** #22 asked for camber and a lift proxy, and both moved exactly as they did for
  #21: camber is a property of the geometry, and `c_l` became real with #68. The search moves
  `alpha` to hit `c_l = 0` in eleven solves from a 20° range, and the test requires the sweep's
  polar to change sign *inside the bracket the search reports* — two computations of one answer
  agreeing where they overlap.
- *Not built, and named in the roadmap:* multi-parameter search, gradients and dolfinx-adjoint,
  per-iteration progress on the event channel, and the browser view. The first two implement
  `next_point` rather than change it; the last wants the HTTP surface #21 is also waiting on.

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
- *Two from review, and the first is the guard defeating itself.* The point cap asked
  `len(self.variations())` — so **validating** a body naming ten axes of a hundred values would
  build the 10^20-point product it was about to refuse, turning a guard against an accidental
  256-solve grid into a far more effective denial of service. Counted with `prod` over the axis
  lengths now, and the regression test monkeypatches `product` to fail rather than leaving a
  reintroduction to exhaust the machine. The second: the unknown-parameter refusal reported
  `loc: ["parameter"]` whatever the kind, sending a caller to a field a sweep does not have.
  One error per bad name now, each located — the axis that carries it, by index, or `points`.
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
