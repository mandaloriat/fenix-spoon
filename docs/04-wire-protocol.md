# Wire protocol — v1

The contract between clients and a Fenix Spoon server. JSON everywhere; all endpoints under
`/api/v1`. The pydantic models in `server/fenixspoon/geometry.py`, `protocol.py`,
`solvers/base.py` and `core/discovery.py` are the source of truth; this document is the
human-readable view, and the [protocol models](reference-protocol.md) page is generated from
them. Breaking changes bump the path version.

## Versioning

The protocol is versioned `MAJOR.MINOR`, currently **1.4**, and a server reports what it
speaks:

### `GET /api/v1/version`

```json
{ "protocol": "1.4", "implementation": "0.1.0", "api_path": "/api/v1" }
```

**The one route that never requires an API key.** A client needs to know whether it can talk
to a server *before* deciding what to send, and if version discovery needed a credential then
a misconfigured client could not tell "wrong key" from "wrong protocol". It discloses two
version strings and a path prefix — the same things the OpenAPI page already serves.

`protocol` is a **string**, not a number: as a float, `1.10` parses to `1.1` and sorts below
`1.9`.

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

The distinction matters because [M2.5](03-roadmap.md)
plans a second transport (JSON-RPC 2.0 over stdio) over the same domain models; its design draft
is [docs/07-local-agent-interface.md](07-local-agent-interface.md). **This document stays the
specification of the HTTP/WebSocket protocol** — the two are not merged here. What they share is
the models above, plus the conformance corpus both are required to satisfy.

Three pieces of that draft have landed, and how much of each reaches this document differs. The
transport-neutral core ([#42](https://github.com/mandaloriat/fenix-spoon/issues/42)) is invisible
on the wire. Progressive discovery ([#43](https://github.com/mandaloriat/fenix-spoon/issues/43))
is the four routes below. The **workspace**
([#44](https://github.com/mandaloriat/fenix-spoon/issues/44)) — versioned objects under ids like
`geometry:g-1`, RFC 6902 patches, submission by design reference — deliberately has **no HTTP
binding at all**: its first transport is the JSON-RPC adapter (#45), and binding it here first
would mean designing an object API that the transport it exists for might want differently. The
only trace it leaves on this contract is the `workspace` path in `environment.inspect`, which
[#43](https://github.com/mandaloriat/fenix-spoon/issues/43) had specified and had nothing to point
at until now. Finally, **compact results**
([#46](https://github.com/mandaloriat/fenix-spoon/issues/46)) are protocol 1.3: response levels,
declared metric *values*, formalised diagnostics and bounded field queries, all bound below. And
the **result cache** ([#47](https://github.com/mandaloriat/fenix-spoon/issues/47)) is protocol
1.4: `provenance` on every result, and an identical resubmission answered from the solve that
already ran. The rest of the draft — JSON-RPC, CLI, MCP and studies — is still design.

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
`geometries`, `params`, `metrics`, `artifacts`, `cost`, `features`, `requirements`, `examples`.
Omit it entirely for all of them plus the title, description, physics tag and availability.

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

The `params` section carries a flat parameter list — name, type, default, bounds, enum choices —
plus `schema_ref`. Being flat is the point rather than being small: a `Literal` parameter reaches
a caller as `$ref` → `$defs` → `enum`, and resolving that is work the summary does once. On the
adapters shipped today the summary is in fact marginally *larger* than the schema it summarises.

#### `GET /api/v1/capabilities/{name}/schema`

Resolves the `schema:params/{name}` reference from a `params` section to the full JSON Schema —
the same object `/solvers` embeds, fetched deliberately. Or pass `?inline_schemas=true` to
`capability.describe` to get it in one round trip.

## Geometry

Geometry payloads are a discriminated union on `type`. Two kinds exist.

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

- `background` applies wherever no region covers — but **what that means is the solver's
  choice, not the protocol's**. `mock.magnetostatics2d` solves the background as another
  material, so its `mu_r` matters. `mock.heat2d` does not solve it at all: the region set *is*
  the solid, everything else is fluid handled as a convective boundary condition, and the
  background's keys are ignored. The result's `mask` marks which cells were not solved.
- Regions may be **nested** (core inside a coil); where they overlap, **later entries in the
  list win**, like painter's order. Regions whose outlines properly *cross* are rejected —
  that describes an ambiguous material assignment rather than nesting.

### Common rules

- `bounds`: `[xmin, ymin, xmax, ymax]`, with `xmin < xmax` and `ymin < ymax`.
- `polygon2d.points`: ≥ 3 vertices, implicitly closed, strictly inside the bounds. Polygons
  must be **simple** — self-intersections are rejected at validation time because downstream
  meshers can hang on them.
- A job is rejected with `422` if the geometry kind is not in the chosen solver's
  `geometry_types` (see `GET /solvers`).

Planned kinds: `spline2d` profiles, `axisymmetric2d`, `step3d` (uploaded CAD).

## Job lifecycle

### `POST /api/v1/jobs` → `202`

```json
{ "solver": "mock.laplace2d", "geometry": { "type": "domain2d", "...": "..." }, "params": { "resolution": 128 } }
```

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
  "metrics": { "speed_max": 1.379, "cp_min": -0.903 },
  "diagnostics": { "converged": true, "residual": 8.4e-10, "warnings": [] },
  "provenance": { "cached": false, "solver": "mock.laplace2d", "solver_version": "1",
                  "cache_key": "c96e071c516e59b1cc1352e650bf6210", "seconds": 1.84 },
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

`provenance` (protocol 1.4) says where the answer came from. **`cached` is the field to read:**
false means these numbers were computed for this request, true means they came from an earlier
identical solve. It is the difference between a metric that reflects the edit you just made and
one answering a question you asked ten minutes ago.

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
payload, and this one answers the question a caller usually has. Five levels, selected with a
repeatable `?levels=`: `status`, `metrics`, `diagnostics`, `fields`, `artifacts`.

**The default is every level except `fields`**, which is the entire behavioural change: a
caller that says nothing gets an answer it can read. Measured on a 96-point potential-flow
solve, the default answer is 686 bytes against 529 kB for the full payload. An unrequested
level is *absent* rather than null, and an unknown level name is a `422` for the same reason a
misspelled capability section is.

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

Until these land, the result envelope is exactly what is documented above: `job_id`, `kind`,
`data`, `stats`, `metrics`, `diagnostics`, `artifacts`.
