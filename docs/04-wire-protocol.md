# Wire protocol — v1

The contract between clients and a Fenix Spoon server. JSON everywhere; all endpoints under
`/api/v1`. The pydantic models in `server/fenixspoon/geometry.py`, `protocol.py`,
`solvers/base.py` and `core/discovery.py` are the source of truth; this document is the
human-readable view, and the [protocol models](reference-protocol.md) page is generated from
them. Breaking changes bump the path version.

**This is the HTTP contract.** The same operations are reachable over a pipe with no server
and no port — see [JSON-RPC over stdio](08-json-rpc.md), which is an adapter over the same
core and the same models, so the two answer the same questions with the same shapes. The
protocol version below is shared: `rpc.describe` reports it too.

## Versioning

The protocol is versioned `MAJOR.MINOR`, currently **1.14**, and a server reports what it
speaks:

### `GET /api/v1/version`

```json
{ "protocol": "1.14", "implementation": "0.1.0", "api_path": "/api/v1" }
```

**The one route that never requires an API key.** A client needs to know whether it can talk
to a server *before* deciding what to send, and if version discovery needed a credential then
a misconfigured client could not tell "wrong key" from "wrong protocol". It discloses two
version strings and a path prefix — the same things the OpenAPI page already serves.

`protocol` is a **string**, not a number: as a float, `1.10` parses to `1.1` and sorts below
`1.9`. **And it is not a string comparison either** — `"1.10"` also sorts below `"1.9"`,
because `'1' < '9'` at the third character. Split on the dot and compare the halves as
integers, which is what `checkProtocolCompatibility` does and what the corpus's `1.10` case
exists to keep honest. The second half of this warning was missing until 1.10 made it real,
and its absence produced a draft of [ADR 0002](adr/0002-workspace-over-http.md) proposing
`"1.10" > "1.9"` as a test — a false assertion written by someone who had read the first
half.

### What is a breaking change

| | Change | Version |
|---|---|---|
| **Additive** | A new optional field | MINOR |
| | A new member of a discriminated union — a geometry kind, a result kind | MINOR |
| | A new endpoint, a new solver, a new `stats` key | MINOR |
| **Breaking** | Removing or renaming a field | MAJOR |
| | Narrowing a type, or making an optional field required | MAJOR |
| | Changing a discriminator value (`"domain2d"` → something else) | MAJOR |
| | Giving a status code a new meaning | MAJOR |

The discriminated unions are what make this answerable rather than a matter of taste: adding
a union member cannot break a client that never asks for it, whereas changing a tag breaks
every client that switches on one.

**MAJOR is mirrored in the path.** `/api/v1` serves protocol 1.x, and a 2.0 would be served
at `/api/v2` — so two majors can coexist during a deprecation window, and a client that
constructs `/api/v1` URLs is already asserting the major it expects. MINOR shares a path,
because that is exactly what "additive" buys.

### Why the version is not in every payload

It does not vary within a session, so repeating it on every event and result would be
per-message overhead for something one call answers. Nor is it a header: the transports
planned in [M2.5](03-roadmap.md#m25-local-automation-and-agent-interface) have neither headers
nor paths, and expose the same question as `environment.inspect`. Making it an *operation* is
what lets one answer serve every transport.

The consequence to know: a **stored** result carries no version. Provenance on stored results
is [#47](https://github.com/mandaloriat/fenix-spoon/issues/47)'s job, not the envelope's.

### Changing it

One number lives in three places — `PROTOCOL_VERSION` in `server/fenixspoon/protocol.py`,
`PROTOCOL_VERSION` in `@fenix-spoon/client`, and `protocol_version` in
`protocol/fixtures/version.json`. Both test suites assert against the fixture, so moving any
one of them alone turns the other red. The checklist is in
[CONTRIBUTING.md](https://github.com/mandaloriat/fenix-spoon/blob/main/CONTRIBUTING.md).

## Scope: domain contract vs HTTP transport

Two things are described here and they age differently.

**The domain contract** — geometry kinds, solver descriptions, capability declarations, job
statuses, progress and status events, result kinds, `stats`, artifact metadata — is what a Fenix
Spoon *means*, independent of how it is carried. It is defined by the pydantic models in
`geometry.py`, `solvers/base.py`, `protocol.py` and `core/discovery.py` (rendered in the
[protocol reference](reference-protocol.md)) and validated by the fixtures in
`protocol/fixtures/`. Any future transport is expected to carry these same models with the same
semantics.

**The HTTP envelope** — paths under `/api/v1`, verbs, status codes (`202` on submit, `409` on a
result that isn't ready, `410` on one whose arrays have been swept, `422` on validation failure,
`429` over quota), the WebSocket event
channel, the `Authorization` header and the `url` fields that make artifacts fetchable — is this
transport's binding of that contract. A caller that is not speaking HTTP will encode "job not
finished" and "unknown solver" differently, but must mean the same thing.

The distinction stopped being theoretical with [#45](https://github.com/mandaloriat/fenix-spoon/issues/45):
[JSON-RPC 2.0 over stdio](08-json-rpc.md) now carries the same domain models with the same
semantics and encodes the envelope differently — "job not finished" is a `409` here and a
`-32002` there. **This document stays the specification of the HTTP/WebSocket protocol** — the
two are not merged. What they share is the models above, plus the conformance corpus both are
required to satisfy.

Three pieces of that draft have landed, and how much of each reaches this document differs. The
transport-neutral core ([#42](https://github.com/mandaloriat/fenix-spoon/issues/42)) is invisible
on the wire. Progressive discovery ([#43](https://github.com/mandaloriat/fenix-spoon/issues/43))
is the four routes below. The **workspace**
([#44](https://github.com/mandaloriat/fenix-spoon/issues/44)) — versioned objects under ids like
`geometry:g-1`, RFC 6902 patches, submission by design reference — had **no HTTP binding at
all** until 1.10, deliberately: its first transport was the JSON-RPC adapter (#45), and binding
it here first would have meant designing an object API that the transport it exists for might
want differently. That deferral ended when the deferral's own reason did — JSON-RPC carried the
shape through five releases and three consumers, so 1.10 binds a proven shape rather than a
guessed one. See [workspace objects](#workspace-objects) below and
[ADR 0002](adr/0002-workspace-over-http.md) for the decisions. Finally, **compact results**
([#46](https://github.com/mandaloriat/fenix-spoon/issues/46)) are protocol 1.3: response levels,
declared metric *values*, formalised diagnostics and bounded field queries, all bound below. And
the **result cache** ([#47](https://github.com/mandaloriat/fenix-spoon/issues/47)) is protocol
1.4: `provenance` on every result, and an identical resubmission answered from the solve that
already ran. The **JSON-RPC transport** ([#45](https://github.com/mandaloriat/fenix-spoon/issues/45))
adds nothing to this document by design — it is a second binding of these models, not a change to
them, and it is where the workspace finally became reachable. The rest of the draft has since
landed on the same terms and left this document equally unchanged: the
[CLI and Python adapters](10-cli-and-python.md) ([#50](https://github.com/mandaloriat/fenix-spoon/issues/50)),
the [MCP adapter](09-mcp.md) ([#49](https://github.com/mandaloriat/fenix-spoon/issues/49)), and
[studies](07-local-agent-interface.md#7-minimum-operation-set)
([#48](https://github.com/mandaloriat/fenix-spoon/issues/48)) — each a further binding of these
models rather than an addition to them, which is the property the transport-neutral core exists
to keep. Studies stopped being an exception at 1.11: they are bound here now, in
[studies](#studies) below, on the same terms as the workspace and for the same reason — the
shape had been carried by four transports before this document described it. 1.12 does the same
for [optimizations](#optimizations), which finishes ADR 0002, and it is the one bump that
changes a shipped behaviour rather than only adding: `optimize.run` stops blocking.

**Protocol 1.5** adds two things to the domain contract and both are additive: a
[`series1d` result kind](#one-dimensional-results) with a `series` key beside `data`
([#69](https://github.com/mandaloriat/fenix-spoon/issues/69)), and an
[`assumptions` section](#assumptions) on `capability.describe`
([#70](https://github.com/mandaloriat/fenix-spoon/issues/70)). The potential-flow adapters also
started reporting `circulation`, `c_l`, `c_m_c4` and `x_cp`
([#68](https://github.com/mandaloriat/fenix-spoon/issues/68)), which is *not* a protocol change:
`metrics` keys have always been whatever a capability declares, and `GET /capabilities/{name}`
is where a caller learns them.

**Protocol 1.13** adds one thing, to the part of the contract that had not grown since 1.0: a
third [geometry kind](#geometry), `axisymmetric2d`
([#100](https://github.com/mandaloriat/fenix-spoon/issues/100)). Additive, because a new member
of a discriminated union is — a client that never sends one cannot tell. What it changes is what
the server can *refuse*: a meridian half-section of a body of revolution used to travel as
`regions2d` with bounds starting at zero, meaning r, and a plane solver would accept it and
quietly solve a slice. `geometry_types` and the `422` behind it have always been able to prevent
that; what was missing was a kind to name.

## Authentication

Optional and off by default: with no keys configured every caller is the principal
`anonymous` and no header is needed. When a server sets `FENIXSPOON_API_KEYS`, every route
requires a key:

```
Authorization: Bearer <key>          # or:  X-API-Key: <key>
```

Missing or wrong is `401` with `WWW-Authenticate: Bearer`. **The event-stream WebSocket takes
the key as `?api_key=<key>` instead** — a browser cannot set headers on a WebSocket handshake.
An unauthenticated stream is refused at the handshake, which reaches a browser as an HTTP `403`
and an `onerror` rather than an open socket. The header still works there for non-browser
clients.

Jobs belong to the principal that created them. Another principal's job id is a `404` on every
endpoint, not a `403`. Exceeding a quota (`concurrent jobs`, `jobs/hour`, `artifact bytes`) is a
`429` with a prose `detail`, and `Retry-After` where a wait actually helps. See
[deployment](05-deployment.md).

## Discovery

### `GET /api/v1/solvers`

Lists solvers installed on this server, with a JSON Schema for their parameters (drive your UI
forms from it):

```json
[
  {
    "name": "mock.laplace2d",
    "title": "Potential flow (mock, NumPy)",
    "description": "2D incompressible potential flow around an obstacle ...",
    "geometry_types": ["domain2d"],
    "params_schema": { "type": "object", "properties": { "resolution": {"...": "..."} } }
  }
]
```

This is the exhaustive answer, and it is the right one for a form generator: to build every
control it needs every schema. Its payload is fixed — protocol 1.2 added the four routes below
*beside* it rather than changing it.

### Progressive discovery

Added in protocol 1.2 ([#43](https://github.com/mandaloriat/fenix-spoon/issues/43)). Three
questions that `/solvers` answers only by answering all of them at once. Measured on the three
mock adapters, `/solvers` is 4.0 kB where `/capabilities` is 0.5 kB; both grow with the number of
solvers installed, so the ratio matters more than either figure.

These are the HTTP binding of `environment.inspect`, `capability.list` and
`capability.describe` — the same operations the local transports in
[M2.5](03-roadmap.md#m25-local-automation-and-agent-interface) expose without a server. That is
why sections are a list of strings and the schema reference is an opaque `schema:` identifier
rather than a URL: neither has anything HTTP-shaped in it.

#### `GET /api/v1/environment`

What this installation *is*: versions, which dependencies imported, execution and event
backends, the store, the data and workspace directories, the configured limits, and **the calling
principal's** quotas and current usage. No schemas. Behind the auth gate, unlike `/version` — the
two version strings are not secret, but a data directory, a backend topology and another
principal's quota position are not free information.

`cache` is reported as an explicit `null`: the content-addressed cache is
[#47](https://github.com/mandaloriat/fenix-spoon/issues/47) and does not exist yet, and a null
lets a caller distinguish "no cache here" from "this server is too old to say".

#### `GET /api/v1/capabilities`

One line per installed capability — name, title, physics tag, accepted geometry kinds,
availability (`mock` or `fenicsx`). No schemas, no prose:

```json
[
  {
    "name": "mock.laplace2d",
    "title": "Potential flow (mock, NumPy)",
    "physics": "potential-flow",
    "geometry_types": ["domain2d"],
    "availability": "mock"
  }
]
```

#### `GET /api/v1/capabilities/{name}`

One capability, in the sections asked for. `?sections=` is repeatable and selects from
`geometries`, `params`, `metrics`, `assumptions`, `artifacts`, `cost`, `features`,
`requirements`, `examples`. Omit it entirely for all of them plus the title, description,
physics tag and availability.

**An unrequested section is absent from the JSON, not present and null.** `?sections=metrics`
returns exactly `name` and `metrics` — `name` because an answer should be identifiable without
correlating it back to the request. An unknown section name is a `422` rather than being
ignored: a caller that misspells `metrics` and gets a payload with no metrics in it would
conclude the capability has none, which is a wrong answer arrived at quietly.

```json
{
  "name": "mock.heat2d",
  "metrics": [
    {"name": "t_max", "unit": "degC", "description": "Peak temperature ...",
     "field": "T", "reduction": "max"},
    {"name": "t_rise", "unit": "K", "description": "Peak temperature above ambient ..."}
  ]
}
```

Metrics are **declared, not yet computed**: this tells a caller what a solve will report, and
the level that returns the values is [#46](https://github.com/mandaloriat/fenix-spoon/issues/46).
Where a metric names a `field` and a `reduction` it is a stated reduction of a declared result
field; `t_rise` names neither because it depends on a parameter as well, and claiming otherwise
would be a declaration nothing can evaluate.

Where a metric is neither a field reduction nor a plain derived ratio it names a `boundary`
instead: `c_l` and `c_m_c4` are integrals over the **body surface**, not over the domain, and
saying so is what lets a caller tell "the solver integrates this over the body" from "the solver
works this out somehow" — added in 1.5 with
[#68](https://github.com/mandaloriat/fenix-spoon/issues/68).

`over` says **what the number is taken over**, and 1.6 added it because a transient made the
question unavoidable ([#86](https://github.com/mandaloriat/fenix-spoon/issues/86)). `payload` is
the default and means the result that came back — for a steady solve that is the whole answer,
for a transient it is the final instant. `run` means the whole solve: the peak over a start-up,
the time to reach a level, a property of the configuration. The distinction is not cosmetic and
the models enforce half of it: a `run` metric **cannot** name a `field`, because a field
reduction is computed by the runtime from the payload and a `run` quantity is by definition not
in it. Without the declaration the two are told apart only by their names, and a transient's
`t_peak` and `t_final_max` agree on every monotonic case anyone would check by hand.

### Naming a piece of the boundary

Protocol 1.8 lets a geometry name parts of its own boundary
([#85](https://github.com/mandaloriat/fenix-spoon/issues/85)). The split it rests on is that
two questions were being conflated:

- **where** — which part of the boundary. That *is* a property of the shape: only the geometry
  knows where one edge ends and the next begins;
- **what** — clamped, loaded, convecting. That is **not**: the same cantilever is loaded three
  ways and remains the same cantilever.

So the geometry carries names, and what happens on them is a load case (a separate object,
landing separately). Putting the conditions in the geometry would mint a new geometry revision
for every load change, and turn comparing two load cases into comparing two shapes.

```jsonc
{
  "type": "domain2d", "bounds": [-2, -2, 3, 3],
  "obstacle": {"type": "polygon2d",
               "points": [[0,0], [1,0], [1,1], [0,1]],
               "point_ids": ["a", "b", "c", "d"]},
  "boundaries": [
    {"name": "root", "select": {"type": "points", "ids": ["c", "d"]}},
    {"name": "plane", "select": {"type": "near", "axis": "y", "value": 0.0}},
    {"name": "body", "select": {"type": "part", "of": "obstacle"}}
  ]
}
```

**Both `points` and `near` exist because they follow different things.** An id follows the
*shape*: insert a control point and every index after it shifts, so an index-based boundary
would slide onto a different edge with nothing to notice — an id stays on the edge it was given
to. A predicate follows the *space*: a symmetry plane is a statement about position and stays
put whatever profile is placed on it. Neither can express the other honestly.

`part` is the third family and needed no invention: `outer`, `obstacle` and `region:<name>` are
what potential flow, magnetostatics and heat already assume implicitly. `all_of` intersects
selectors — deliberately a small closed set rather than an expression language, which would be
the UFL-over-the-wire argument again in miniature.

Everything here is **additive and empty by default**: `point_ids` is absent unless a boundary
is named, and every adapter shipped today puts its conditions where the outer/obstacle split
implies them.

### Saying what happens there: the load case

Protocol 1.9 is the other half of the same issue. A **`load_case`** workspace object maps a
boundary name to the scalars in force on it, and a design references it the way it already
references a geometry:

```jsonc
// load_case object
{"conditions": {
   "root": {"fixed": 1},
   "tip":  {"traction_x": 0.0, "traction_y": -1.0e6}
}}

// design — the reproducible route
{"solver": "dolfinx.elasticity2d", "geometry": "geometry:g-7f3a",
 "load_cases": ["load_case:lc-91bd"], "params": {"plane": "stress"}}

// job.submit — the inline route, for one-shot use
{"solver": "mock.elasticity2d", "geometry": {...},
 "conditions": {"root": {"fixed": 1}, "tip": {"traction_y": -1.0e6}}}
```

A separate object rather than a block in the design, because an engineer reuses one set of
restraints and loads across a family of shapes and that reuse is the whole argument. Two load
cases on one design are merged per key, and a disagreement — both setting the same key on the
same boundary — is refused rather than resolved by list order.

**The values are an open map of scalars, exactly like `Region2D.material`.** A typed enum of
condition kinds would put physics into the protocol, so every new physics would become a
protocol change. What keeps that openness from costing a caller a silent typo is that each
capability **declares** the keys it reads, in the `conditions` section of
`capability.describe`:

```json
{"name": "traction_y", "unit": "Pa", "kind": "neumann",
 "description": "Uniform surface traction along y, positive towards +y."}
```

Two refusals follow from the pair, and both are **422 at submit rather than a silent no-op**
— a condition applied to nothing produces a solve that runs, converges, and answers a
different problem:

| Refusal | When |
|---|---|
| `UnknownBoundary` | the load case names a boundary the geometry does not declare |
| `UnknownConditionKey` | the key is one the capability does not read — including every key, for a capability that reads none |
| `ConflictingConditions` | two of a design's load cases set the same key on the same boundary |

The load case is also part of a solve's **content address**: two load cases on one shape are
two cache entries. Leaving them out of the key would serve the clamped answer to the caller
who asked about the free one.

Adapters that had boundary conditions as *parameters* keep them. `fixed_edge` / `load_edge`
on the elasticity pair remain the shorthand for the common case; a load case, when one is
supplied, replaces them entirely rather than merging with them, so a caller who named every
boundary never inherits an invisible clamp from a default it did not set.

### Time: an index over the artifacts

A time-dependent solve produces two things of different natures, and 1.7 keeps them apart
([#86](https://github.com/mandaloriat/fenix-spoon/issues/86)). **Scalars against time are
curves** — `T_max(t)`, a probe history — and `series1d` has carried those since 1.5.
**Fields against time are large**, so they cross as references like every other large thing
here: an adapter writes one file per stored instant, the artifact carries the instant it
holds in `t`, and the result's `frames` lists them in time order.

```json
{
  "artifacts": [
    {"name": "frame_0005.vtk", "size": 8213, "t": 50.0, "url": "..."},
    {"name": "solution.vtk", "size": 8213, "url": "..."}
  ],
  "frames": [{"t": 50.0, "artifact": "frame_0005.vtk"}]
}
```

`frames` is **derived from the artifacts, never stored beside them**. That is the whole
reason the instant lives on the file: an index naming something the result does not serve is
not a case to validate, it is unrepresentable. It also means a steady result carries no index
at all rather than an empty one, and that the index rides on the `artifacts` level — so the
default answer tells a caller which instants exist, and how large each is, before it fetches
any of them. The count is capped for the same reason the series lengths are.

**There is deliberately no reduction at a chosen instant.** `result.query` reduces the
payload, and a transient's payload is its final instant. A scalar-against-time question is
answered by the curve; a field question means fetching the frame. That limit was accepted
when the shape was settled rather than discovered afterwards, and a `t` argument on
`FieldQuery` would answer about the wrong time.

### Modes: the same index over an ordering that is not time

*Added in protocol 1.14 ([#101](https://github.com/mandaloriat/fenix-spoon/issues/101)).*

An eigensolve produces exactly the two natures above: **a scalar per member of an ordered
family** — the spectrum — and **a field per member**, too large to inline. So it needed the
shape 1.5 and 1.7 already built, and one new name:

```json
{
  "artifacts": [
    {"name": "mode_1.vtk", "size": 4096, "mode": 1, "url": "..."},
    {"name": "mode_4.vtk", "size": 4096, "mode": 4, "url": "..."}
  ],
  "modes": [{"mode": 1, "artifact": "mode_1.vtk"}, {"mode": 4, "artifact": "mode_4.vtk"}]
}
```

`modes` is derived from the artifacts exactly as `frames` is, is capped for the same reason,
and rides on the same level. What is *not* shared is the field: **a mode number does not go
in `t`**, and refusing to overload it is the decision this version records
([ADR 0004](adr/0004-a-mode-is-not-an-instant.md)). `t` is an instant in the solver's time
unit; a mode number is an ordinal — dimensionless, 1-based, with no metric on it, since the
gap between mode 1 and mode 2 is not a quantity. One slot for both would sort modes as
though they were seconds and leave every 1.7 consumer asking which meaning a result meant.
**The two are mutually exclusive on one file**, and a payload carrying both is refused rather
than resolved: a file indexed twice would be ordered two ways.

The **frequencies** need nothing new at all. They are a `series1d` — the fourth row of the
table in [one-dimensional results](#one-dimensional-results), which had had no producer since
1.5 — whose abscissa is the **mode number**, in the same numbering the artifacts carry:

```json
{ "name": "spectrum",
  "x": { "name": "mode", "unit": "1", "values": [1, 2, 3, 4] },
  "traces": [ { "name": "frequency", "unit": "Hz", "values": [0.0, 0.0, 0.0, 537.2] },
              { "name": "rigid_body", "unit": "1", "values": [1, 1, 1, 0] } ] }
```

so a caller joins "mode 4 is at 537 Hz" to mode 4's shape **by that number**, never by
position in two lists that happen to be ordered alike. Rigid-body modes — the three
zero-frequency motions of an unrestrained plane structure — are reported rather than filtered,
flagged in the curve and counted in the `rigid_body_modes` metric, because their count is how
a caller tells a sound model from one restrained somewhere nobody intended.

The `params` section carries a flat parameter list — name, type, default, bounds, enum choices —
plus `schema_ref`. Being flat is the point rather than being small: a `Literal` parameter reaches
a caller as `$ref` → `$defs` → `enum`, and resolving that is work the summary does once. On the
adapters shipped today the summary is in fact marginally *larger* than the schema it summarises.

#### Assumptions

*Added in protocol 1.5 ([#70](https://github.com/mandaloriat/fenix-spoon/issues/70)).*

Every other section says what a capability **does**. This one says what its model **assumes**,
and where it stops applying — which is the thing a caller most needs before trusting a number,
and which previously existed only as prose inside `description`, if at all:

```json
{
  "name": "mock.laplace2d",
  "assumptions": [
    {"name": "incompressible", "statement": "Density is constant. Valid below roughly Mach 0.3.",
     "quantity": "mach", "limit": 0.3, "comparator": "<"},
    {"name": "inviscid",
     "statement": "No boundary layer, so no wall shear, no separation and no stall. Pressure drag integrates to exactly zero (d'Alembert) ...",
     "excludes": ["drag", "c_d", "skin_friction", "stall", "alpha_stall", "separation"]},
    {"name": "no_circulation",
     "statement": "With `kutta` false the streamfunction is set to an arbitrary constant on the body ...",
     "excludes": ["lift", "c_l", "circulation", "c_m_c4", "x_cp"],
     "when": "kutta", "when_value": false},
    {"name": "two_dimensional", "statement": "A cross-section of a body infinitely long in z ...",
     "excludes": ["end_effects", "span_efficiency", "three_dimensional_flow"]}
  ]
}
```

**`excludes` is the field that pays for itself.** It lets a caller ask *"can this capability tell
me about drag?"* and get a definite **no** rather than a plausible zero. That is the same
argument [#43](https://github.com/mandaloriat/fenix-spoon/issues/43) made for
`CapabilityFeatures`, where all three flags default to false so "can I sweep this?" has an answer
a caller can act on — this is the physics equivalent.

**`quantity`, `limit` and `comparator` make an assumption checkable** where it has a numeric edge
and the quantity is computable: Mach against 0.3, `b_max` against a saturation threshold. That is
what makes this more than documentation. `mock.magnetostatics2d` is the case that motivated it —
its `b_max` metric description already said *"past roughly 1.5 T for common steels the
permeability collapses and a linear solve stops describing the device"*, which is exactly right
and was unusable, being prose inside a metric. The per-run half — "this run has M = 0.42, above
the 0.3 limit" — is a *diagnostic* and belongs with `warnings`; declaring the limit here is what
lets such a warning name the assumption it violated instead of restating it.

**`when` marks a conditional assumption** by naming a boolean parameter, and `when_value` says
which setting of it arms the assumption. Almost all assumptions are unconditional and omit both.
(`when` is a bare parameter name in `ArtifactSpec` too, and means the same thing there — that
consistency is why the value lives in a field of its own rather than in a `!` prefix.) The one that is
not is worth the field: run `mock.laplace2d` with `kutta` false and there genuinely is no
circulation, so `c_l` is genuinely absent — and a caller reading the section before it has chosen
its parameters sees both `kutta_condition` and `no_circulation`, which is the honest answer to
"what might this assume".

**An empty list means "none declared", and is not the same as the section being absent.** Since
every physical model assumes something, an empty list reads as *undeclared* — which is itself
information, and is why the shipped adapters are tested for declaring one.

#### `GET /api/v1/capabilities/{name}/schema`

Resolves the `schema:params/{name}` reference from a `params` section to the full JSON Schema —
the same object `/solvers` embeds, fetched deliberately. Or pass `?inline_schemas=true` to
`capability.describe` to get it in one round trip.

## Geometry

Geometry payloads are a discriminated union on `type`. Three kinds exist.

### `domain2d` — a domain with a hole

For flow-around-a-body problems: the obstacle is *cut out* of the mesh.

```json
{
  "type": "domain2d",
  "bounds": [-2.0, -1.5, 4.0, 1.5],
  "obstacle": { "type": "polygon2d", "points": [[0.0, 0.0], [1.0, 0.05], [0.3, -0.08]] }
}
```

### `regions2d` — a domain filled with material regions

For field problems where the physics varies by material (solenoid: iron core, copper coil, air).
Every region is *filled*; the mesh covers the whole rectangle.

```json
{
  "type": "regions2d",
  "bounds": [-0.06, -0.06, 0.06, 0.06],
  "background": { "mu_r": 1.0 },
  "regions": [
    { "name": "core", "shape": { "type": "polygon2d", "points": [["..."]] },
      "material": { "mu_r": 1000.0 } },
    { "name": "coil_right", "shape": { "type": "polygon2d", "points": [["..."]] },
      "material": { "current_density": 5.0e6 } }
  ]
}
```

- `material` is an **open dict of scalars**, not a typed physics model: the protocol stays
  physics-agnostic and each solver documents the keys it reads. Unknown keys are ignored, so
  one payload can carry properties for several solvers and be sent to each in turn:

  | solver | reads | default when absent |
  |---|---|---|
  | `mock.magnetostatics2d`, `dolfinx.magnetostatics2d` | `mu_r`, `current_density` | `1.0`, `0.0` |
  | `mock.heat2d`, `dolfinx.heat2d` | `k` (W/m·K), `q` (W/m³) | `1.0`, `0.0` |
  | `mock.electrostatics_axi2d`, `dolfinx.electrostatics_axi2d` | `eps_r`, `voltage` (V) | `1.0`, — |

- `background` applies wherever no region covers — but **what that means is the solver's
  choice, not the protocol's**. `mock.magnetostatics2d` solves the background as another
  material, so its `mu_r` matters. `mock.heat2d` does not solve it at all: the region set *is*
  the solid, everything else is fluid handled as a convective boundary condition, and the
  background's keys are ignored. The result's `mask` marks which cells were not solved.
- Regions may be **nested** (core inside a coil); where they overlap, **later entries in the
  list win**, like painter's order. Regions whose outlines properly *cross* are rejected —
  that describes an ambiguous material assignment rather than nesting.

### `axisymmetric2d` — a meridian (r, z) section

*Added in protocol 1.13 ([#100](https://github.com/mandaloriat/fenix-spoon/issues/100)).*

A half-section of a **body of revolution**: the horizontal coordinate is a radius, and a solver
integrates with the `r` weight the cylindrical volume element carries, so one section stands for
the whole revolved body rather than for a slice per unit depth. Capacitance comes back in
farads, not farads per metre.

```json
{
  "type": "axisymmetric2d",
  "bounds": [0.019, -0.0006, 0.0231, 0.0006],
  "background": { "eps_r": 1.0 },
  "regions": [
    { "name": "electrode", "shape": { "type": "polygon2d", "points": [["..."]] },
      "material": { "voltage": 1.0 } },
    { "name": "mirror", "shape": { "type": "polygon2d", "points": [["..."]] },
      "material": { "voltage": 0.0 } }
  ]
}
```

It is a **filled region map**, exactly like `regions2d`: painter's-order nesting, no partially
overlapping outlines, the same open scalar `material` dict. Those are not restated rules but the
same ones — one function in `geometry.py` enforces both kinds, so a payload cannot be legal as a
plane section and illegal as a meridian one.

**What the kind buys is a refusal.** The same rectangle could always be sent as `regions2d` with
bounds starting at zero and *meant* as r; nothing validated it, nothing told a viewer the
horizontal axis was a radius, and nothing stopped it reaching a plane solver — which would accept
it and answer a different problem. The failure mode was a wrong number rather than an error.

Two rules are its own, and both follow from what the coordinates mean:

- **`rmin >= 0`**, enforced at validation. A negative radius is not a domain a body of revolution
  has; it is a plane section that has been mislabelled, and this is where that is cheap to catch.
- **A region may lie *on* r = 0** when the section reaches the axis. A solid shaft is bounded by
  the axis, so its outline sits on the left edge; the strictly-inside rule below would otherwise
  make the commonest axisymmetric shape unrepresentable. The relaxation is the axis alone — a
  section starting at r = 19 mm has a *truncation* there, and a region touching it is refused.

**A section that does not reach the axis is legitimate**, and a solver must not assume otherwise:
an annular electrode 20 mm out has no reason to model the centreline. Nothing has to be said in
the payload either way — `rmin` says it.

**The axis needs no mechanism.** In an r-weighted weak form the boundary term carries the same
`r`, so at r = 0 it vanishes identically: the natural symmetry condition is what an adapter gets
by doing nothing there, and the only rule is not to impose a potential on it. A caller that wants
to *name* the axis — to refer to it in a load case — uses the selector 1.8 already shipped:

```json
{ "name": "axis", "select": { "type": "near", "axis": "x", "value": 0.0 } }
```

The selector's `axis` names the **coordinate slot**, not the physical axis of revolution: `x` is
the first coordinate, which for this kind is r. Why the kind carries the claim that the first
coordinate is a radius, rather than a field on the payload saying so, is
[ADR 0003](adr/0003-axisymmetric-axis-label.md).

### Common rules

- `bounds`: `[xmin, ymin, xmax, ymax]`, with `xmin < xmax` and `ymin < ymax`. For
  `axisymmetric2d` they are `[rmin, zmin, rmax, zmax]` and `rmin >= 0`.
- `polygon2d.points`: ≥ 3 vertices, implicitly closed, strictly inside the bounds — except on the
  axis of an `axisymmetric2d` section, as above. Polygons must be **simple** —
  self-intersections are rejected at validation time because downstream meshers can hang on them.
- A job is rejected with `422` if the geometry kind is not in the chosen solver's
  `geometry_types` (see `GET /solvers`). This is the whole protection an axisymmetric section
  gets, and it is why the kind exists.

Planned kinds: `spline2d` profiles, `step3d` (uploaded CAD). Neither is planned *because someone
wants to draw that shape* — [ADR 0005](adr/0005-thin-about-physics-thick-about-claims.md) is the
test each has to pass first: what can a server refuse, or a consumer check, once the protocol
knows the outline is a spline? `axisymmetric2d` passed it (`rmin >= 0`, the axis relaxation, the
`422` above); these two have not been asked yet.

## Workspace objects

*Added in protocol 1.10 ([ADR 0002](adr/0002-workspace-over-http.md)) — additive, so every route
here is new and nothing that worked at 1.9 changed.*

Versioned objects under stable ids, so a client stops resending what it already sent. Seven
types — `geometry`, `material`, `boundary_condition`, `load_case`, `design`, `study`,
`optimization` — reachable from every transport since 1.10 and from a local process since #44.

| | Route | |
|---|---|---|
| `GET` | `/api/v1/objects` | this principal's objects, newest first, `?type=` to filter |
| `POST` | `/api/v1/objects/{type}` | create, `201` with revision 1 |
| `GET` | `/api/v1/objects/{type}/{id}` | the head, or `?revision=3` for a pinned one |
| `GET` | `/api/v1/objects/{type}/{id}/revisions` | which revisions exist |
| `PATCH` | `/api/v1/objects/{type}/{id}` | apply an RFC 6902 patch, `200` with the new revision |

**A reference is not a path segment.** An object is named `geometry:g-12`, optionally pinned as
`geometry:g-12@3` — and `{type}/{id}` is the pair that identifier decomposes into, so a client
never percent-encodes a colon and the server rebuilds the canonical reference from its own path
parameters. A URL whose halves disagree — `/objects/design/g-1`, where a design id starts with
`d` — is a `422` naming both, not a lookup that quietly finds nothing.

**The revision is a query parameter** because it is a *view* of an object at a point in its
history, not a sub-resource with an identity of its own. One route answers both, in one shape.

**Objects are never mutated and never deleted.** `PATCH` reads the head, applies the patch and
writes the next revision; the one it came from stays readable forever. There is no `DELETE`, on
any transport: a job is a computation and losing it costs a re-run, an object is something a
person authored and losing it is data loss.

Errors: `404` no such object *or somebody else's* — the same answer, because confirming an id
exists is itself a disclosure · `422` unknown type, malformed reference, a body that fails its
schema, or a patch that is not valid RFC 6902 · `409` a patch that changed nothing, or a patch
aimed at a pinned revision · `429` over the object quota.

**The object quota** (`FENIXSPOON_MAX_OBJECTS`, `0` for unlimited) counts objects a principal
owns and is checked at create. It carries no `Retry-After`: nothing about waiting deletes an
object, and a hint that suggested otherwise would be the kind of lie the artifact-bytes quota
already refuses to tell.

## Studies

*Added in protocol 1.11 ([ADR 0002](adr/0002-workspace-over-http.md), decision 3) — additive,
and it adds no models: both routes speak shapes four other transports have carried since
[#48](https://github.com/mandaloriat/fenix-spoon/issues/48).*

A **study** is a `study` object, created and patched through the object routes above like any
other. These two routes act on one:

| | Route | |
|---|---|---|
| `POST` | `/api/v1/studies/{study_id}/run` | submit every variation, `202`, `?revision=` to pin |
| `GET` | `/api/v1/studies/{study_id}` | the table and what it means, `?revision=` to pin |

**There is no request body on either.** A study says what to solve — which design, which
parameter, which values, which metrics — so a run has nothing left to be told, and the answer
is a pure function of the revision it names. That is also why
[#21](https://github.com/mandaloriat/fenix-spoon/issues/21)'s sketched `POST /sweeps`, with the
grid in the body, is not what landed: a sweep posted that way is a computation with no
identity. It cannot be pinned, re-read next week, re-run into the cache, or handed to a
colleague as an id. Change the grid by patching the study, which costs one JSON Patch and
leaves a revision behind saying what changed.

**`run` returns before the solving does**, `202`, exactly as `POST /jobs` does and for the same
reason — a five-rung ladder takes minutes and this is a request a browser is holding open:

```json
{ "study": "study:s-3@2", "jobs": ["j-1a2b...", "j-9f0e..."],
  "submitted": 4, "reused": 2, "refused": 0 }
```

`reused` counts variations the [result cache](#the-result-cache) answered without solving,
and this reply is the one place it is interesting: it says how much of the study just came
free. `refused` counts variations the server would not accept — a cell budget, a quota — and
`GET` says why, per variation. One rung over the budget does not fail the other four.

**Running a study twice is idempotent**, including while the first run is still in flight, and
nothing was added to make it so: every variation goes through `POST /jobs`, and the cache has
matched `queued` and `running` jobs since 1.4. The second run returns the same job ids with
`reused` counting them.

**`?revision=` pins**, the same way and for the same reason it does on the object routes — and
it is not decoration. *"You cannot pin it"* is the first thing this design holds against a
sweep posted as a request body, so a binding that could only ever act on the head would refute
its own argument. `GET /studies/s-3?revision=1` reports the study as it was written then, which
is how a table stays readable after the grid has been widened twice, and
`POST /studies/s-3/run?revision=1` re-runs it — free, because a study's job list is a pure
function of its revision and every one of those jobs is already in the cache. A revision that
does not exist is a `404`, never a quiet fall back to the head.

**`GET` is free to call before the run, during it, and after.** There is no stored run record
that could be absent: the study says what to solve, the jobs are the answer, and the report is
assembled from them each time. Poll it while a run is in flight and `complete` goes true when
every variation has reached a terminal state, answered or refused.

The report's shape is discriminated on the study's own `kind`. A `mesh_convergence` study
answers with the ladder, plus `relative_change` between rungs and the value each metric
`settled_at`. A `sweep` answers with one row per point, plus **one
[`Series1DData`](#one-dimensional-results) per tabulated metric** — the same model a `series1d`
result uses, so anything that already draws a curve draws this one, `<fs-plot>` included, with
no adapter in between. That was the reason for choosing that model for sweeps a release before
anything could fetch one.

```json
{ "study": "study:s-3@2", "kind": "sweep", "design": "design:d-1@1",
  "parameters": ["alpha"], "solver": "mock.laplace2d",
  "points": [ { "values": { "alpha": -6 }, "job_id": "j-1a2b...", "status": "done",
                "cached": true, "metrics": { "c_l": -0.4411 } } ],
  "curves": [ { "name": "c_l", "...": "..." } ],
  "complete": true }
```

Errors are the object routes' errors, because the argument is an object reference: `404` no
such study *or somebody else's* · `422` a reference that is not a study, or a study body whose
parameter or metric names the capability does not declare — reported with a `loc` pointing at
the axis that misspelled it rather than at the document as a whole.

## Optimizations

*Added in protocol 1.12 ([ADR 0002](adr/0002-workspace-over-http.md), decision 4) — the last of
that record's four pieces, and the one carrying a genuinely new mechanism.*

An **optimization** searches a bracket for the parameter value that minimises, maximises or hits
a target on a declared metric. Like a study it is an object that runs, created and patched
through the object routes, with two routes acting on it:

| | Route | |
|---|---|---|
| `POST` | `/api/v1/optimizations/{id}/run` | start the search, `202`, `?revision=` to pin |
| `GET` | `/api/v1/optimizations/{id}` | the trajectory so far, `?revision=` to pin |

**The receipt names no jobs**, and that is structural rather than an omission:

```json
{ "optimization": "optimization:o-3@1", "started": true }
```

A study can hand back every job id at once because its job list is a pure function of its
revision. A search cannot name its second point until the first is answered — that *is* what
"chooses the next point" means — so a receipt promising job ids would be promising something
unknowable. `started: false` means a search for this revision was already in flight in this
process and the call joined it rather than starting a rival.

**`optimize.run` used to block and return the finished report**, on every transport, and 1.12
changes that. It is the one shipped behaviour this protocol has altered rather than added to,
and the argument is in decision 4: a search is minutes of solving, HTTP cannot hold a request
open across the proxies and browser timeouts between a page and a server, and the first draft of
that record concluded the transports would have to diverge. They do not. `POST /jobs` has always
returned a receipt while the CLI waits, and nobody calls that a divergence, because **waiting is
a convenience of the caller-facing layer, not part of the operation**. So `optimize.run` joins
that pattern: it returns as soon as the search is accepted everywhere, and
[`fenix-spoon optimize run`](10-cli-and-python.md) and `local.wait_for_optimization` keep
waiting on top of it — with `--detach` to skip the wait, exactly as `job submit` has.

**The search then continues as a task owned by the server process.** That is the first thing
here that is neither a job nor a request, and the property that keeps it honest is that it is a
*driver, not state*: the state is the jobs. Kill the server mid-search and nothing is lost that
a re-run does not recover for free — every point already answered is a cache hit, and
`GET /optimizations/{id}` rebuilds the trajectory by replaying the method against the jobs
either way.

Poll that route while a search runs and it grows a row per evaluation. Two fields are worth
reading beside the rows:

- **`running`** — whether a search is in flight *in the process answering this call*. The one
  field in the report that is not a replay, and it says so: behind two API replicas, a search
  driven by the other one reads as `false`. That is a true statement this process can
  substantiate, where `true` would not be.
- **`stopped`** — `converged` (the bracket reached the tolerance), `budget`, `stalled` (an
  evaluation had no answer, which ends a *search* where it would only leave a gap in a study's
  table), `not_run`, or `incomplete`. The last one is the honest cost of having no run record:
  a submission the server refused leaves no job behind, so a replay can see how far the
  trajectory got and not why it ended. A process that ran the search remembers the reason and
  the refusal's message for as long as it lives; a process that did not says `incomplete`
  rather than guessing.

Errors are the object routes' errors: `404` no such optimization *or somebody else's* · `422` a
reference that is not an optimization, or a body whose parameter or metric the capability does
not declare.

## Job lifecycle

### `POST /api/v1/jobs` → `202`

Either a design reference — protocol 1.10, and the reason the object routes exist:

```json
{ "design": "design:d-18" }
```

or an inline solve, which is what every version before 1.10 accepted and still works:

```json
{ "solver": "mock.laplace2d", "geometry": { "type": "domain2d", "...": "..." }, "params": { "resolution": 128 } }
```

**Both together is a `422`**, not a precedence rule: a request carrying a design *and* a
geometry holds two intents, and honouring one silently produces a job whose inputs are not what
the caller thinks they are. **`params` beside a design is the same `422`** — to change a
parameter, patch the design, which is one small JSON Patch and leaves the next solve
reproducible from the workspace alone. That last refusal was missing until a review of #21
asked what became of the overrides: they were accepted and then discarded, which is the failure
this rule exists to prevent, reached from the one direction nobody had checked.

What the design form buys, beyond the bytes: a job submitted by reference records the exact
object revisions it resolved, so `provenance` can answer *which design this picture came from*.
A submission that inlined its geometry never can.

Response: `{ "job_id": "j-8f3a...", "status": "queued" }`

Errors: `404` unknown solver · `422` invalid geometry/params (pydantic detail format) · `422`
over the server's cell budget, with a plain-string detail naming the estimate and the limit:

```json
{ "detail": "job would use about 4,194,304 cells, over this server's limit of 2,000,000. Lower the resolution or mesh size, or raise FENIXSPOON_MAX_CELLS." }
```

The budget check runs at submit time from the solver's own cheap estimate (grid resolution,
or `2·area/h²` for a meshed domain), so an over-sized request is refused immediately instead
of being started and killed halfway through by the wall-clock timeout. Operators set the
limit with `FENIXSPOON_MAX_CELLS` (default 2,000,000; `0` disables it). A solver that cannot
estimate its cost is admitted, with the timeout as the backstop.

### `GET /api/v1/jobs`

Job history, newest first. `?limit=` (1–200, default 50) and `?offset=` paginate; out-of-range
values are a `422`.

```json
{ "jobs": [ { "job_id": "...", "solver": "...", "status": "done", "...": "..." } ],
  "total": 137, "limit": 50, "offset": 0 }
```

Entries are the same shape as `GET /jobs/{job_id}`. With a persistent store configured (the
default) the listing spans process lifetimes; with `FENIXSPOON_STORE=memory` it covers only the
current one.

### `GET /api/v1/jobs/{job_id}`

`{ "job_id": "...", "solver": "...", "status": "queued|running|done|failed|cancelled", "error": null, "created_at": "...", "finished_at": null }`

### `POST /api/v1/jobs/{job_id}/cancel` → `202`

Requests cooperative cancellation; the solver stops at its next check point and the job ends in
the `cancelled` terminal status. `409` if the job already ended. Long-running jobs are also
subject to a server-side wall-clock timeout (`FENIXSPOON_JOB_TIMEOUT`, default 600 s), which
fails the job with a timeout error.

### `WS /api/v1/jobs/{job_id}/events`

Server pushes one JSON message per event. Past events are replayed on connect, so subscribing
after completion still yields the full history. Stream closes after a terminal event
(`done`, `failed`, or `cancelled`).

```json
{ "type": "progress", "iteration": 400, "total": 2000, "residual": 3.2e-05, "message": null }
{ "type": "status", "status": "done" }
{ "type": "status", "status": "failed", "error": "..." }
{ "type": "status", "status": "cancelled" }
```

### `GET /api/v1/jobs/{job_id}/result`

`409` until the job is `done`, and `410` afterwards if the field arrays have gone — a retention
sweep or a hand cleanup can remove `result.json` while the job's metadata, metrics, diagnostics
and artifact list survive in the database. `410 Gone` rather than `409` or `404` because the job
is very much there and the compact levels still answer for it; only the payload is missing, which
is precisely what `Gone` means. Result envelope:

```json
{
  "job_id": "j-8f3a...",
  "kind": "grid2d",
  "data": { "...": "see result kinds below" },
  "stats": { "cells": 8192, "iterations": 3000, "seconds": 1.8421 },
  "metrics": { "speed_max": 1.379, "cp_min": -0.903, "c_l": 0.596, "circulation": -0.298 },
  "diagnostics": { "converged": true, "residual": 8.4e-10, "warnings": [] },
  "provenance": { "cached": false, "solver": "mock.laplace2d", "solver_version": "1",
                  "cache_key": "c96e071c516e59b1cc1352e650bf6210", "seconds": 1.84 },
  "series": [ { "name": "surface_cp", "traces": [ { "...": "see one-dimensional results" } ] } ],
  "artifacts": [
    { "name": "solution.vtk", "content_type": "model/vnd.vtk", "size": 191234,
      "url": "/api/v1/jobs/j-8f3a.../artifacts/solution.vtk" }
  ]
}
```

**`stats` is what the solve cost; `metrics` is what it answered.** Both are open maps of
`string → number` and clients must treat every key as optional, but they are different
questions. An operator reads `stats` to size a machine; an engineer reads `metrics` to make a
decision. `cells` and `seconds` are conventional in `stats` and `seconds` is always present
(the job manager measures it); the rest is whatever the adapter knows — the mock solvers
report `iterations`, the FEniCSx adapters report `dofs`. A key present in `stats` is the
measured value, unlike the pre-flight `estimate_cells` used by the budget check.

`metrics` keys are exactly the ones the capability declares — ask
`GET /capabilities/{name}?sections=metrics` for their units and meanings *before* running
anything. **`t_max` and `t_rise` moved here from `stats` in protocol 1.3**, which was the
whole point: they were never costs. That is additive rather than breaking under the rule
above, because `stats` keys have always been documented as server-defined and all optional —
but a client that ignored that and hard-coded `stats.t_rise` will need the one-line change
the [heat-sink demo](gallery.md) shows.

`diagnostics` carries the three things `stats` could not hold, being typed `string → number`:
`converged` (null where the question does not apply — a direct LU factorisation does not
iterate toward a tolerance), `residual`, and `warnings`, which previously could only be said
in a progress event and therefore only to a client that happened to be watching.

`series` is protocol 1.5's addition and is described under
[one-dimensional results](#one-dimensional-results). It is always present on this route —
empty for a capability that produces no curves — because this is the exhaustive envelope, which
already carries the field arrays, so withholding a bounded curve from it would save nothing.

`provenance` (protocol 1.4) says where the answer came from. **`cached` is the field to read:**
false means these numbers were computed for this request, true means they came from an earlier
identical solve. It is the difference between a metric that reflects the edit you just made and
one answering a question you asked ten minutes ago.

`solver_version` and `environment` are **recorded when the job is accepted**, not read off the
server as it stands when you ask. So they keep answering "what produced this payload" after the
deployment has moved on — an adapter that bumps its version, or a package upgrade, does not
rewrite the history of jobs that ran before it. A job stored before this was recorded reports
`solver_version: "unknown"` and an empty `environment` rather than today's values.

## The result cache

Added in protocol 1.4 ([#47](https://github.com/mandaloriat/fenix-spoon/issues/47)). In an
iterative loop most resubmissions are identical to something already computed — patch a control
point, solve, patch it back, solve — and a solve with an identity derived from its inputs makes
the second of those a database lookup.

**A hit returns the job that already ran.** `POST /jobs` still answers `202`, but the `job_id`
may be one you have seen before, `status` may already be `done`, and `cached` is `true`. A client
that assumes a fresh submission is always `queued` will wait for a transition that already
happened — that is the one behavioural change in 1.4 and the reason `cached` is on the submit
response rather than only on the result.

The identity covers **everything that determines the answer**: the solver name, its declared
`version`, the *validated* geometry and params, and the versions of the packages the capability
depends on. Validated rather than as-submitted is what makes it hit at all — a caller that omits
a defaulted parameter and one that states it have sent different JSON and want the same answer.

Three consequences worth knowing:

- **Caching is opt-in per adapter.** A capability is cached only if it declares itself
  deterministic; `GET /environment` lists which ones do. Serving a cached answer for a solver
  that does not reproduce is a wrong answer delivered quickly, and a missed hit is merely a
  solve, so the default is the safe one.
- **A hit costs no quota**, because it costs no compute. Quotas limit work, and a lookup is not
  work.
- **The cache expires with the job.** An entry *is* its job, so `FENIXSPOON_JOB_TTL` is the only
  lifetime involved; sweeping a job makes the next identical submission a miss that recomputes.
  There is no second retention policy and no dangling entry.

The cache is per-principal. A cross-principal hit would save more and would tell one caller that
another has run this exact geometry, which a job id is already treated as disclosing.

### Compact results

Added in protocol 1.3 ([#46](https://github.com/mandaloriat/fenix-spoon/issues/46)). The
envelope above is right for a viewer, which draws every one of those numbers, and wrong for a
caller that has to reason about the answer: a `grid2d` result is tens of thousands of floats.

#### `GET /api/v1/jobs/{job_id}/summary`

The same relationship `/capabilities` has to `/solvers` — the exhaustive route keeps its
payload, and this one answers the question a caller usually has. Seven levels, selected with a
repeatable `?levels=`: `status`, `metrics`, `diagnostics`, `provenance`, `series`, `fields`,
`artifacts`.

**The default is every level except `series` and `fields`**, which is the entire behavioural
change: a caller that says nothing gets an answer it can read. Measured on a 96-point
potential-flow solve, the default answer is 686 bytes against 529 kB for the full payload. An
unrequested level is *absent* rather than null, and an unknown level name is a `422` for the same
reason a misspelled capability section is.

`series` (1.5) sits outside the default for a narrower reason than `fields` does. A curve is
*bounded* and is genuinely part of the answer rather than part of the cost, so leaving it out is
not free — but the acceptance criterion for the default is that it carries **no numeric array
longer than a handful of entries**, and a two-hundred-point surface distribution is a numeric
array whatever else it is. What would have justified including it is not at stake: levels are a
*list* on one request, so `?levels=status&levels=metrics&levels=series` is a single call. An
empty list there means the capability produced no curves, which a caller must be able to tell
from a level it forgot to ask for.

Artifacts here carry a `path` rather than a `url`: this route reports what the core knows, and
a caller wanting a download uses the artifact endpoint the full envelope advertises.

#### `POST /api/v1/jobs/{job_id}/query`

One bounded question about one field, for the scalars the declared metrics do not cover:

```json
{ "field": "speed", "op": "max" }
→ { "job_id": "j-8f3a...", "field": "speed", "op": "max",
    "result": { "value": 1.3796, "at": [0.326, 0.111] } }
```

Operations: `max` / `min` (with location), `mean` (area-weighted), `integral`, `at_point`
(interpolated), `over_region`, `section` along a line, `sample` (decimated), `hotspots`
(the N most extreme *distinct* locations, not N neighbours of one peak).

`POST` because the request is a structured object with a dozen optional arguments rather than
an identifier; it is still a read. `section` and `sample` take a `samples` budget which the
server **caps** rather than refuses — an uncapped budget is the whole field spelled
differently. Scalar fields only: an extremum of a vector needs a norm nobody has chosen, which
is why the adapters emitting `velocity` also emit `speed` beside it.

`over_region` is the one operation that needs something the result does not carry — a result
has arrays, not region names — so it resolves the geometry through the job's workspace
provenance. A job submitted with an inline geometry kept no reference to one and gets a `422`
saying so rather than an empty region.

Result kinds:

- `series1d` (implemented, added in 1.5): curves rather than a field — see
  [one-dimensional results](#one-dimensional-results).
- `grid2d` (implemented): fields sampled on a regular grid —
  `{ "bounds": [xmin, ymin, xmax, ymax], "shape": [ny, nx], "fields": { "<name>": [...] },
  "mask": [...] }`. Arrays are row-major with index `[iy * nx + ix]`, y increasing upward;
  `mask` is 1 inside the obstacle.
- `mesh2d` (implemented; emitted by both the mock solver and the FEniCSx adapter, where it
  carries the actual P1 triangulation): unstructured triangle mesh —
  `{ "bounds": [...], "points": [[x, y], ...], "triangles": [[i, j, k], ...],
  "point_fields": { "<name>": [...] } }`. Triangle indices reference `points`; `cell_fields`
  is reserved for per-triangle data.

### `GET /api/v1/jobs/{job_id}/artifacts/{name}`

Downloads an artifact listed in the result envelope. Only names registered by the solver are
servable (artifact names are bare filenames by construction — no path traversal). Artifacts
live on the server filesystem under `FENIXSPOON_DATA_DIR` and share the job's lifetime.

## Durability

Job metadata, the event log, the result payload and the artifacts all outlive the server
process: mount `FENIXSPOON_DATA_DIR` and a restarted server answers for jobs the previous one
ran. Two consequences a client should expect:

- A job that was `running` when the server died comes back `failed` with
  `"server restarted while this job was running"` — a status stream that could never
  terminate is worse than a job that admits it was lost.
- Records are kept for `FENIXSPOON_JOB_TTL` (default 7 days, `0` keeps them forever). Past
  that, the job and its files are gone and every endpoint answers `404`.

## Conventions

- All floats are IEEE-754 doubles in JSON; binary framing (msgpack / typed-array over WS) is a
  planned optimization, negotiated via `Accept`, never the default.
- Timestamps are RFC 3339 UTC.
- CORS is open in dev images; production deployments configure allowed origins explicitly (M3).

## Vector fields

*Added in protocol 1.1 — additive, so it shares `/api/v1` with 1.0 and a 1.0 client is
unaffected.*

Both result kinds carry vectors in a map of their own, indexed exactly like the scalar one:

| kind | scalars | vectors |
|---|---|---|
| `grid2d` | `fields` — name → `ny*nx` numbers | `vector_fields` — name → `ny*nx` `[x, y]` pairs |
| `mesh2d` | `point_fields` — name → one per node | `point_vector_fields` — name → one `[x, y]` per node |

```json
"vector_fields": { "velocity": [[1.0, 0.0], [0.9, 0.1], "..."] }
```

**Why a separate map rather than `u` and `v` in `fields`.** Two scalars named by convention
are not a vector: a viewer cannot know they pair, `result.query` cannot ask for "maximum
speed" over them, and every solver would invent its own naming. One named entry makes the
vector a thing the protocol knows about.

**Magnitude is shipped as well, not instead.** `mock.laplace2d` sends both `velocity` and
`speed`. That is redundant on the wire and deliberate: the viewer colours by magnitude on
every frame, and recomputing it over ~170k points in JavaScript to save one field is the
wrong trade. A client that wants only direction can ignore `speed`.

**Drawing them.** `<fs-viewer vectors="velocity">` overlays arrow glyphs on whatever scalar
is being coloured. Glyph density comes from the `glyphs` attribute — roughly how many arrows
span the width — and **not** from the data's resolution: one arrow per grid point is
unreadable at 512×341 and sparse at 16×16, and the same field would look like a different
physical situation at two mesh sizes.

**Integrating them.** `<fs-viewer streamlines="velocity">` draws the curves tangent to the
field. Note what the protocol does *not* say here: `vector_fields` is a map of names to
component pairs, and nothing in it declares that `velocity` is a velocity — that an adapter
chose the name is a convention, not a type. So the viewer's API calls the result **integral
curves**, and it is the consumer application, which knows what it submitted, that gets to call
them flow lines. A viewer cannot know whether it has drawn a streamline, a magnetic field line
or a heat-flux path.

The same distinction is why a scalar field is never turned into a vector one. A viewer *could*
integrate the gradient of whatever scalar is selected and produce curves; they would look like
physics and would not be it, because the relationship between a scalar and a flow is a
modelling assumption this contract deliberately does not carry. **A result with no
`vector_fields` has no curves**, and the tool is unavailable with a reason rather than falling
back to something plausible — the same argument `Assumption.excludes` makes for `drag`.

**Exploring them costs nothing on the wire.** Zoom, pan, probe, sections, glyph density, curve
seeding and a pinned colour scale are all functions of the arrays above, computed in the page.
No part of this contract describes presentation, and none of it needed to grow for the viewer
to become explorable — see
[ADR 0001](adr/0001-explorable-viewer.md).

## One-dimensional results

*Added in protocol 1.5 ([#69](https://github.com/mandaloriat/fenix-spoon/issues/69)) — additive,
so it shares `/api/v1` and a 1.4 client is unaffected.*

Until 1.5 there were two result kinds and both were *fields over a 2-D domain*. A great many
engineering answers are not that shape. They are an ordered list of (x, y) pairs with a name and
a unit:

| The question | The curve |
|---|---|
| What is this profile doing? | `C_p(x/c)` along the upper and lower surface |
| How does it respond? | a parameter sweep — `C_L(alpha)`, `T_max(fin count)`, `force(gap)` |
| Do I believe it? | a convergence history — a metric against mesh size |
| Where does it resonate? | a frequency sweep, or a modal frequency list |

The homes such a curve had before were each wrong in a different way. `stats` is
`string → number`, so a 200-point curve became 200 keys — and `stats` is what the solve *cost*,
which 1.3 had just finished separating from what it answered. A `grid2d` with `ny = 1` lied about
the topology, dragged a `mask` and `bounds` along, and rendered as a one-pixel-tall picture. An
artifact worked, and put the *compact* half of the answer behind a second fetch and outside every
typed model, so each application invented its own JSON for the same thing.

### The shape

```json
{
  "name": "surface_cp",
  "description": "Pressure coefficient along the body.",
  "traces": [
    { "name": "cp_upper", "unit": "1", "values": [1.0, -1.62, -0.74],
      "x": { "name": "x/c", "unit": "1", "values": [0.0, 0.12, 0.45] } },
    { "name": "cp_lower", "unit": "1", "values": [1.0, 0.21],
      "x": { "name": "x/c", "unit": "1", "values": [0.0, 0.3] } }
  ]
}
```

A sweep, whose traces share one abscissa, puts it at the top level instead:

```json
{ "name": "lift_curve",
  "x": { "name": "alpha", "unit": "deg", "values": [-4, -2, 0, 2, 4] },
  "traces": [ { "name": "c_l", "unit": "1", "values": [-0.477, -0.239, 0.0, 0.239, 0.477] } ] }
```

**A shared abscissa, with a per-trace override.** Shared covers a sweep and a distribution
resampled to common stations; per-trace is needed the moment two traces are genuinely sampled
differently, which is the upper and lower surface of an airfoil if nobody resampled them. A trace
with neither is rejected — index order is not an abscissa a protocol should invite a client to
guess.

**Units travel on the wire, unlike the 2-D kinds.** A curve is drawn with axis labels and the
client has no other way to learn what the axis is; a field is coloured, and `<fs-viewer>` takes
its colourbar caption from an attribute the page sets. `"1"` means dimensionless.

**Complex values are two real traces, not a complex type.** A harmonic result is plotted as a
magnitude and a phase — different units, different axes — so a complex scalar would need every
consumer to pick one of the two anyway, and JSON has no complex number to be faithful to.

**Length is bounded, at three levels**: 4096 points per trace, 32 traces per collection, and 8192
points across a whole result. The third is the one that matters, because the first two multiply
out to far more than either intends. Without them "series" would be a second name for the field
arrays, and the compact response's promise would have a hole in it.

### Two places, one at a time

A result may be curves, or it may be a field *with* curves:

| | `kind` | curves live in |
|---|---|---|
| the answer is a curve — a sweep, a convergence history | `series1d` | `data` |
| the answer is a field, and a curve came with it | `grid2d` / `mesh2d` | `series` |

One solve legitimately answers both questions — the airfoil adapters return the flow field *and*
the surface `C_p` — and making a caller submit twice for the second would be inventing a round
trip. A `series1d` result carrying anything in `series` is **rejected**: `kind` selects the schema
of `data`, and letting the two disagree would leave a consumer needing a rule for which wins. In
the SDK, `resultSeries(result)` reads whichever place applies.

### Drawing them

`<fs-viewer>` is a 2-D field widget: a curve has no bounds to fit, no topology to interpolate over
and nothing to contour, so handing it a `series1d` clears the view and warns rather than drawing a
one-pixel picture. A plot with axes, a legend and the inverted-y convention aerodynamic `C_p` uses
is a separate widget, and is not in this release.

## Planned extensions to the domain contract

Not implemented; recorded here so the models grow compatibly instead of being duplicated in a
second protocol. Each is driven by
[M2.5](03-roadmap.md) and detailed in the
[local agent interface draft](07-local-agent-interface.md).

- ~~**Metric values on a result.**~~ Landed in 1.3 — see [compact results](#compact-results).
- ~~**Diagnostics.**~~ Landed in 1.3, grown out of `stats` rather than beside it.
- **Object references.** A geometry that has already been sent should be referenceable rather than
  resent. Whatever identifier scheme the workspace settles on must be expressible in a job request
  on every transport, not only in the local one.
- ~~**Result levels.**~~ Landed in 1.3, as a separate route rather than a query parameter on the
  existing one: `/result` keeps its shape and its arrays, and `/summary` is the compact form. Two
  shapes on one path, chosen by a query parameter, is not something a typed client can describe.
- ~~**One-dimensional results.**~~ Landed in 1.5 — see
  [one-dimensional results](#one-dimensional-results).
- ~~**Declared assumptions.**~~ Landed in 1.5 — see [assumptions](#assumptions).
- ~~**A curve widget.**~~ Shipped as [`@fenix-spoon/plot`](../client/packages/plot/) — axes with
  round ticks, a legend, a hover readout and an opt-in inverted `y`. **No protocol change**: it
  draws numbers 1.5 already carried, which is the same relationship `<fs-viewer>`'s explorable
  tools have to 1.1. `invert-y` is an attribute rather than something inferred from a trace
  called `cp_upper`, on the grounds [ADR 0001](adr/0001-explorable-viewer.md) records — a name is
  not a quantity.
- **Boundary integrals in general.** `MetricSpec.boundary` names the boundary a metric integrates
  over, and today `body` is the only name any adapter uses. Gap force in magnetostatics, reaction
  forces in elasticity and wall heat flux in conduction are the same shape. Half of this is now
  settled and from the other direction than expected: the question *"does the boundary need to be
  addressable"* was answered **yes** by 1.8, but by a load case needing to say where a clamp goes
  rather than by a metric needing to say where a force was integrated. So the geometry can name a
  boundary and a submission can act on one, while `MetricSpec.boundary` still takes the `body`
  convention. What remains is the small half — letting a declared metric name a
  [geometry boundary](#naming-a-piece-of-the-boundary) — and it stays unbuilt until an adapter reports a
  reaction force, on the same rule the rest of this thread follows: the capability first, the
  protocol change second.

Until these land, the result envelope is exactly what is documented above: `job_id`, `kind`,
`data`, `stats`, `metrics`, `diagnostics`, `provenance`, `series`, `artifacts`, and the derived
`frames` and `modes`.
