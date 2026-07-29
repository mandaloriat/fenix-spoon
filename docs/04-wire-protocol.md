# Wire protocol — draft v0

The contract between clients and a Fenix Spoon server. JSON everywhere; all endpoints under
`/api/v1`. The pydantic models in `server/fenixspoon/geometry.py` and `solvers/base.py` are the
source of truth; this document is the human-readable view. Breaking changes bump the path version.

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
  physics-agnostic and each solver documents the keys it reads (unknown keys are ignored,
  so one payload can carry properties for several solvers). `mock.magnetostatics2d` reads
  `mu_r` and `current_density`.
- Regions may be **nested** (core inside a coil); where they overlap, **later entries in the
  list win**, like painter's order. Regions whose outlines properly *cross* are rejected —
  that describes an ambiguous material assignment rather than nesting.
- `background` applies wherever no region covers.

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
