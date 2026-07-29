# Wire protocol — draft v0

The contract between clients and a Fenix Spoon server. JSON everywhere; all endpoints under
`/api/v1`. The pydantic models in `server/fenixspoon/geometry.py` and `solvers/base.py` are the
source of truth; this document is the human-readable view. Breaking changes bump the path version.

## Scope: domain contract vs HTTP transport

Two things are described here and they age differently.

**The domain contract** — geometry kinds, solver descriptions, job statuses, progress and status
events, result kinds, artifact metadata — is what a Fenix Spoon *means*, independent of how it is
carried. It is defined by the pydantic models in `geometry.py`, `solvers/base.py` and
`protocol.py`, and validated by the fixtures in `protocol/fixtures/`. Any future transport is
expected to carry these same models with the same semantics.

**The HTTP envelope** — paths under `/api/v1`, verbs, status codes (`202` on submit, `409` on a
result that isn't ready, `422` on validation failure), the WebSocket event channel, and the
`url` fields that make artifacts fetchable — is this transport's binding of that contract, and is
specific to it. A caller that is not speaking HTTP will encode "job not finished" and "unknown
solver" differently, but must mean the same thing.

The distinction matters because [M2.5](03-roadmap.md#m25--local-automation-and-agent-interface)
plans a second transport (JSON-RPC 2.0 over stdio) over the same domain models; its design draft
is [docs/05-local-agent-interface.md](05-local-agent-interface.md). **This document stays the
specification of the HTTP/WebSocket protocol v1** — the two are not merged here, and nothing in
the local-interface draft is implemented. What the two share is the models above, plus the
conformance corpus that both are required to satisfy.

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

Errors: `404` unknown solver · `422` invalid geometry/params (pydantic detail format).

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
  "artifacts": [
    { "name": "solution.vtk", "content_type": "model/vnd.vtk", "size": 191234,
      "url": "/api/v1/jobs/j-8f3a.../artifacts/solution.vtk" }
  ]
}
```

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
live on the server filesystem under `FENIXSPOON_DATA_DIR` and share the job's lifetime
(persistence is roadmap M3).

## Conventions

- All floats are IEEE-754 doubles in JSON; binary framing (msgpack / typed-array over WS) is a
  planned optimization, negotiated via `Accept`, never the default.
- Timestamps are RFC 3339 UTC.
- CORS is open in dev images; production deployments configure allowed origins explicitly (M3).

## Planned extensions to the domain contract

Not implemented; recorded here so the models can grow compatibly rather than being duplicated in a
second protocol. Each is driven by
[M2.5](03-roadmap.md#m25--local-automation-and-agent-interface) and detailed in the
[local agent interface draft](05-local-agent-interface.md).

- **Declared metrics.** `SolverInfo` describes parameters but not outputs, so a caller cannot know
  what a solve will report until it reads one. A `metrics` section (name, unit, description) on
  the solver description, and a `metrics` map on the result envelope, would let both a form
  generator and a non-visual caller work from the same declaration.
- **Diagnostics.** Mesh cells/dofs, iteration count, final residual, convergence flag and warnings
  are today either absent or buried in progress events. They belong on the result as a small
  structured object — this also covers the "surface mesh statistics" part of M1 #6.
- **Object references.** A geometry that has already been sent should be referenceable rather than
  resent. Whatever identifier scheme the workspace settles on must be expressible in a job request
  on every transport, not only in the local one.
- **Result levels.** `status` / `metrics` / `diagnostics` / `fields` / `artifacts` as separately
  requestable levels, so a caller can ask for a summary without the arrays. The HTTP binding of
  this is likely a query parameter on the result endpoint; it must not change the default payload
  the browser SDK already relies on.

Until these land, the result envelope is exactly what is documented above: `job_id`, `kind`,
`data`, `artifacts`.
