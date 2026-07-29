# Wire protocol — v1

The contract between clients and a Fenix Spoon server. JSON everywhere; all endpoints under
`/api/v1`. The pydantic models in `server/fenixspoon/geometry.py`, `protocol.py` and
`solvers/base.py` are the source of truth; this document is the human-readable view, and the
[protocol models](reference-protocol.md) page is generated from them. Breaking changes bump the
path version.

## Versioning

The protocol is versioned `MAJOR.MINOR`, currently **1.1**, and a server reports what it
speaks:

### `GET /api/v1/version`

```json
{ "protocol": "1.1", "implementation": "0.1.0", "api_path": "/api/v1" }
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

**The domain contract** — geometry kinds, solver descriptions, job statuses, progress and status
events, result kinds, `stats`, artifact metadata — is what a Fenix Spoon *means*, independent of
how it is carried. It is defined by the pydantic models in `geometry.py`, `solvers/base.py` and
`protocol.py` (rendered in the [protocol reference](reference-protocol.md)) and validated by the
fixtures in `protocol/fixtures/`. Any future transport is expected to carry these same models with
the same semantics.

**The HTTP envelope** — paths under `/api/v1`, verbs, status codes (`202` on submit, `409` on a
result that isn't ready, `422` on validation failure, `429` over quota), the WebSocket event
channel, the `Authorization` header and the `url` fields that make artifacts fetchable — is this
transport's binding of that contract. A caller that is not speaking HTTP will encode "job not
finished" and "unknown solver" differently, but must mean the same thing.

The distinction matters because [M2.5](03-roadmap.md)
plans a second transport (JSON-RPC 2.0 over stdio) over the same domain models; its design draft
is [docs/07-local-agent-interface.md](07-local-agent-interface.md). **This document stays the
specification of the HTTP/WebSocket protocol** — the two are not merged here, and nothing in the
local-interface draft is implemented. What they share is the models above, plus the conformance
corpus both are required to satisfy.

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

`409` until the job is `done`. Result envelope:

```json
{
  "job_id": "j-8f3a...",
  "kind": "grid2d",
  "data": { "...": "see result kinds below" },
  "stats": { "cells": 8192, "iterations": 3000, "seconds": 1.8421 },
  "artifacts": [
    { "name": "solution.vtk", "content_type": "model/vnd.vtk", "size": 191234,
      "url": "/api/v1/jobs/j-8f3a.../artifacts/solution.vtk" }
  ]
}
```

`stats` is what the solve actually cost, as an open map of `string → number`. Clients must
treat every key as optional: `cells` and `seconds` are conventional and `seconds` is always
present (the job manager measures it), the rest is whatever the adapter knows — the mock
solvers report `iterations`, the FEniCSx adapters report `dofs`. A key present in `stats` is
the measured value, unlike the pre-flight `estimate_cells` used by the budget check.

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

- **Declared metrics.** `SolverInfo` describes a solver's inputs but not its outputs, so a caller
  cannot know what a solve will report until it reads one. A `metrics` section (name, unit,
  description) on the solver description, and a `metrics` map on the result envelope, would let a
  form generator and a non-visual caller work from the same declaration. `stats` is the measured
  *cost* of a solve; metrics are its engineering *answer*, and the two should stay distinct.
- **Diagnostics.** Convergence flag, final residual and warnings have no home today: some of it
  is in `stats`, some only in progress events, some nowhere. A small structured diagnostics object
  alongside `stats` is the natural place — note that `stats` is typed `dict[str, float]`, so a
  convergence flag or a warning string has literally nowhere to go in it today.
- **Object references.** A geometry that has already been sent should be referenceable rather than
  resent. Whatever identifier scheme the workspace settles on must be expressible in a job request
  on every transport, not only in the local one.
- **Result levels.** `status` / `metrics` / `diagnostics` / `fields` / `artifacts` as separately
  requestable levels, so a caller can ask for a summary without the arrays. The HTTP binding is
  likely a query parameter on the result endpoint; it must not change the default payload the
  browser SDK already relies on.

Until these land, the result envelope is exactly what is documented above: `job_id`, `kind`,
`data`, `stats`, `artifacts`.
