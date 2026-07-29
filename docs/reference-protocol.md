<!--
  GENERATED FILE — do not edit.
  Regenerate with: python server/tools/generate_protocol_reference.py
  CI fails if this file is out of date with the models.
-->

# Protocol reference

Every table below is generated from the pydantic models the server validates against, so
this page cannot describe a field the code does not have. The prose companion —
what the endpoints do, what the lifecycle looks like, what the guarantees are — is in
[the wire protocol guide](04-wire-protocol.md).

Solver parameters are not here: which solvers exist depends on what a deployment has
installed, and each one publishes its own JSON Schema at `GET /api/v1/solvers`.


# Geometry

## `Polygon2D`

A closed *simple* polygon; the last point connects back to the first implicitly.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | `'polygon2d'` | no | `'polygon2d'` |  |
| `points` | `list[tuple[float, float]]` | yes |  | Outline vertices in order, as [x, y] pairs. The closing edge is implicit; do not repeat the first point. |

## `Domain2D`

A rectangular computational domain with a polygonal obstacle (hole) inside it.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | `'domain2d'` | no | `'domain2d'` |  |
| `bounds` | `tuple[float, float, float, float]` | no | `(-2.0, -1.5, 4.0, 1.5)` | Outer rectangle as [xmin, ymin, xmax, ymax], in metres. |
| `obstacle` | `Polygon2D` | yes |  | The hole cut out of the domain. Its points must lie strictly inside `bounds`. |

## `Region2D`

A named material region: a polygon plus solver-interpreted material properties.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Unique within one geometry; used to label results. |
| `shape` | `Polygon2D` | yes |  | The region outline, strictly inside `bounds`. |
| `material` | `dict[str, float]` | no | `{}` | Solver-interpreted scalar properties, e.g. `mu_r` and `current_density` for magnetostatics. Keys a solver does not recognise are ignored. |

## `Regions2D`

A rectangular domain filled with material regions over a background material.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | `'regions2d'` | no | `'regions2d'` |  |
| `bounds` | `tuple[float, float, float, float]` | no | `(-0.1, -0.1, 0.1, 0.1)` | Outer rectangle as [xmin, ymin, xmax, ymax], in metres. |
| `regions` | `list[Region2D]` | yes |  | Material regions in painter's order: where two nest, the later one wins. Partially overlapping outlines are rejected. |
| `background` | `dict[str, float]` | no | `{}` | Material outside every region (typically air) |


# Jobs

## `JobRequest`

What `POST /jobs` accepts.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `solver` | `str` | yes |  | A `name` from `GET /solvers`. |
| `geometry` | `Domain2D \| Regions2D` | yes |  | Geometry to solve on; its `type` must be one the solver accepts. |
| `params` | `dict[str, Any]` | no | `{}` | Solver parameters, validated against that solver's schema. |

## `JobCreated`

The 202 from `POST /jobs`. The job has been accepted, not finished.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | Use it to poll status, stream events and fetch the result. |
| `status` | `str` | yes |  | Always `queued` at this point. |

## `JobStatus`

A job's current state, as `GET /jobs/{id}` returns it.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | Identifier assigned at submit. |
| `solver` | `str` | yes |  | Which solver is running it. |
| `status` | `str` | yes |  | `queued`, `running`, or the terminal `done` / `failed` / `cancelled`. |
| `error` | `str \| null` | yes |  | Why it failed. Null unless `status` is `failed`. |
| `created_at` | `datetime` | yes |  | When the job was accepted (RFC 3339, UTC). |
| `finished_at` | `datetime \| null` | yes |  | When it reached a terminal status; null while it is still running. |

## `JobList`

One page of job history, newest first.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `jobs` | `list[JobStatus]` | yes |  | This page of jobs, newest first. |
| `total` | `int` | yes |  | Total stored jobs for this principal, not just this page. |
| `limit` | `int` | yes |  | Page size that was applied. |
| `offset` | `int` | yes |  | Offset that was applied. |


# Events

## `ProgressEvent`

One tick of solver progress, streamed to WebSocket subscribers.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | `str` | no | `'progress'` |  |
| `iteration` | `int` | yes |  | How far the solve has got, in solver-defined units. |
| `total` | `int \| null` | no | `None` | Expected final `iteration`, when the solver can predict it. |
| `residual` | `float \| null` | no | `None` | Convergence measure for iterative solvers; null otherwise. |
| `message` | `str \| null` | no | `None` | Human-readable stage, e.g. `meshing with Gmsh`. |

## `StatusEvent`

A job's lifecycle transition. The stream ends after a terminal one.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | `'status'` | yes |  |  |
| `status` | `'running' \| 'done' \| 'failed' \| 'cancelled'` | yes |  | `done`, `failed` and `cancelled` are terminal; the stream closes after. |
| `error` | `str \| null` | no | `None` | Why the job failed. Null unless `status` is `failed`. |


# Results

## `ResultEnvelope`

What `GET /jobs/{id}/result` returns once a job is `done`.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | The job this result belongs to. |
| `kind` | `'grid2d' \| 'mesh2d'` | yes |  | Selects the schema of `data`: `Grid2DData` or `Mesh2DData`. |
| `data` | `dict[str, Any]` | yes |  | The field data, shaped according to `kind`. |
| `stats` | `dict[str, float]` | no | `{}` | What the solve cost — `cells`, `dofs`, `iterations`, `seconds`. Keys are server-defined and all optional: display them, never branch on one existing. |
| `artifacts` | `list[ArtifactRef]` | no | `[]` | Files the solver wrote, downloadable from the artifact endpoint. |

## `Grid2DData`

Fields sampled on a regular grid. Arrays are row-major, y increasing upward.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `bounds` | `tuple[float, float, float, float]` | yes |  | Area the grid covers, as [xmin, ymin, xmax, ymax]. |
| `shape` | `tuple[int, int]` | yes |  | Grid size as [ny, nx], rows before columns. |
| `fields` | `dict[str, list[float]]` | yes |  | Field name to ny*nx values, indexed `[iy * nx + ix]`. |
| `mask` | `list[int]` | yes |  | One entry per grid point, 1 inside the obstacle. Same indexing as a field. |

## `Mesh2DData`

An unstructured triangle mesh with one value per node per field.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `bounds` | `tuple[float, float, float, float]` | yes |  | Bounding box of the mesh, as [xmin, ymin, xmax, ymax]. |
| `points` | `list[tuple[float, float]]` | yes |  | Node coordinates as [x, y] pairs. |
| `triangles` | `list[tuple[int, int, int]]` | yes |  | Each triangle as three indices into `points`. |
| `point_fields` | `dict[str, list[float]]` | yes |  | Field name to one value per node, in `points` order. |

## `ArtifactRef`

A downloadable file the solver produced alongside the inline result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Bare filename, e.g. `solution.vtk`. |
| `content_type` | `str` | yes |  | MIME type to serve it with. |
| `size` | `int` | yes |  | Size on disk in bytes. |
| `url` | `str` | yes |  | Server-relative download path; join with the API base URL. |


# Discovery

## `SolverInfo`

What ``GET /api/v1/solvers`` returns per solver.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Identifier to pass as `solver` when submitting a job. |
| `title` | `str` | yes |  | Short human-readable name for a picker. |
| `description` | `str` | yes |  | What this solver computes, and how. |
| `geometry_types` | `list[str]` | yes |  | Geometry `type` values this solver accepts, e.g. `["domain2d"]`. |
| `params_schema` | `dict[str, Any]` | yes |  | JSON Schema for this solver's `params`; build a form from it. |
