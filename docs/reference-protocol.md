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
| `type` | `'domain2d'` | yes |  |  |
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
| `type` | `'regions2d'` | yes |  |  |
| `bounds` | `tuple[float, float, float, float]` | no | `(-0.1, -0.1, 0.1, 0.1)` | Outer rectangle as [xmin, ymin, xmax, ymax], in metres. |
| `regions` | `list[Region2D]` | yes |  | Material regions in painter's order: where two nest, the later one wins. Partially overlapping outlines are rejected. |
| `background` | `dict[str, float]` | no | `{}` | Material outside every region (typically air) |


# Jobs

## `JobRequest`

What `POST /api/v1/jobs` accepts.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `solver` | `str` | yes |  | A `name` from `GET /solvers`. |
| `geometry` | `Domain2D \| Regions2D` | yes |  | Geometry to solve on; its `type` must be one the solver accepts. |
| `params` | `dict[str, Any]` | no | `{}` | Solver parameters, validated against that solver's schema. |

## `JobCreated`

The 202 from `POST /api/v1/jobs`. The job has been accepted, not finished.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | Use it to poll status, stream events and fetch the result. |
| `status` | `str` | yes |  | Always `queued` at this point. |

## `JobStatus`

A job's current state, as `GET /api/v1/jobs/{id}` returns it.

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

What `GET /api/v1/jobs/{id}/result` returns once a job is `done`.

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
| `vector_fields` | `dict[str, list[tuple[float, float]]]` | no | `{}` | Field name to ny*nx `[x, y]` pairs, indexed like `fields`. Separate from `fields` so a vector is one named thing rather than two conventions apart. |
| `mask` | `list[int]` | yes |  | One entry per grid point, 1 inside the obstacle. Same indexing as a field. |

## `Mesh2DData`

An unstructured triangle mesh with one value per node per field.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `bounds` | `tuple[float, float, float, float]` | yes |  | Bounding box of the mesh, as [xmin, ymin, xmax, ymax]. |
| `points` | `list[tuple[float, float]]` | yes |  | Node coordinates as [x, y] pairs. |
| `triangles` | `list[tuple[int, int, int]]` | yes |  | Each triangle as three indices into `points`. |
| `point_fields` | `dict[str, list[float]]` | yes |  | Field name to one value per node, in `points` order. |
| `point_vector_fields` | `dict[str, list[tuple[float, float]]]` | no | `{}` | Field name to one `[x, y]` pair per node, in `points` order. |

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


# Environment

## `EnvironmentInfo`

What `environment.inspect` returns: this installation, in a few hundred bytes.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `implementation` | `str` | yes |  | Version of the `fenixspoon` package. |
| `protocol` | `str` | yes |  | Wire-contract version, `MAJOR.MINOR`; same as `/version`. |
| `python` | `str` | yes |  | Interpreter version running the server. |
| `platform` | `str` | yes |  | OS and machine, as `platform.platform()` reports it. |
| `packages` | `list[PackageInfo]` | yes |  | Dependencies whose presence or version changes what a solve can do. |
| `mpi` | `MpiInfo` | yes |  | MPI availability and rank count. |
| `execution_backend` | `str` | yes |  | Where solves run: `in-process` (thread pool) or `arq` (worker containers). |
| `event_bus` | `str` | yes |  | How progress is delivered: `in-process` or `redis`. Reported next to the backend because the two must agree — the symptom of a mismatch is a progress stream that stays silent. |
| `store` | `str` | yes |  | Job store backend: `sqlite` or `memory`. |
| `data_dir` | `str` | yes |  | Absolute path holding the job database, results and artifacts. |
| `workspace` | `str` | yes |  | Absolute path holding the workspace object files. Under `data_dir` by construction — reported anyway because it is the directory a caller would commit to a repository, and deriving it from a convention is guesswork. |
| `capabilities` | `int` | yes |  | How many capabilities are installed. |
| `limits` | `LimitsInfo` | yes |  | Server-side caps applied to every submission. |
| `principal` | `str` | yes |  | Who the server thinks is asking. |
| `quotas` | `QuotaInfo` | yes |  | That principal's limits. |
| `usage` | `UsageInfo` | yes |  | That principal's current usage against them. |
| `cache` | `dict[str, Any] \| null` | no | `None` | Content-addressed result cache state. Null on this server: the cache is issue #47 and does not exist yet. Reported as null rather than omitted so a caller can tell 'no cache here' from 'this server is too old to say'. |

## `PackageInfo`

Whether one dependency imported in this process, and at what version.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Import name, e.g. `dolfinx`. |
| `imported` | `bool` | yes |  | True when the module is loaded in this process. Not the same as installed: `fenixspoon.solvers` attempts the FEniCSx imports at startup, so a broken install reports false here — which is the honest answer, because the FEniCSx capabilities are equally absent. |
| `version` | `str \| null` | no | `None` | The module's `__version__`, when it has one. |

## `MpiInfo`

Whether this process has MPI, and how many ranks it is running on.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `available` | `bool` | yes |  | True when `mpi4py` imported. |
| `size` | `int \| null` | no | `None` | Ranks in `COMM_WORLD`; 1 for the ordinary single-process server. Null when MPI is unavailable. Note no shipped capability declares `features.mpi`. |

## `LimitsInfo`

The server-side caps a caller will meet, so it can size a request in advance.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_timeout_seconds` | `float` | yes |  | Wall-clock ceiling on one solve; 0 when disabled (`FENIXSPOON_JOB_TIMEOUT`). |
| `max_cells` | `int` | yes |  | Submit-time cell budget; 0 when disabled (`FENIXSPOON_MAX_CELLS`). |
| `job_ttl_seconds` | `float` | yes |  | How long a finished job survives; 0 keeps it forever (`FENIXSPOON_JOB_TTL`). |
| `max_workers` | `int` | yes |  | Concurrent solves this process will run (`FENIXSPOON_MAX_WORKERS`). |

## `QuotaInfo`

This principal's per-user limits. `0` means unlimited, which is every default.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `concurrent_jobs` | `int` | yes |  | Jobs this principal may have in flight at once. |
| `jobs_per_hour` | `int` | yes |  | Submissions allowed in a rolling hour. |
| `artifact_bytes` | `int` | yes |  | Total artifact bytes this principal may store. |

## `UsageInfo`

What this principal is using right now, against :class:`QuotaInfo`.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `concurrent_jobs` | `int` | yes |  | Jobs currently queued or running. |
| `jobs_last_hour` | `int` | yes |  | Jobs submitted in the last hour. |
| `artifact_bytes` | `int` | yes |  | Bytes of stored artifacts. |


# Capabilities

## `CapabilitySummary`

One line of `capability.list`: enough to choose, not enough to submit.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Identifier to pass as `solver`, or to `capability.describe`. |
| `title` | `str` | yes |  | Short human-readable name. |
| `physics` | `str` | yes |  | Coarse tag to filter on: `potential-flow`, `magnetostatics`, ... |
| `geometry_types` | `list[str]` | yes |  | Geometry `type` values it accepts. |
| `availability` | `str` | yes |  | `mock` for the NumPy stand-in, `fenicsx` for a real FEniCSx solve. |

## `CapabilityDescription`

What `capability.describe` returns. Every section is optional and omitted unless asked.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | The capability this describes. |
| `title` | `str \| null` | no | `None` | Short human-readable name. |
| `description` | `str \| null` | no | `None` | What it computes, and how. |
| `physics` | `str \| null` | no | `None` | Coarse physics tag. |
| `availability` | `str \| null` | no | `None` | `mock` or `fenicsx`. |
| `geometries` | `GeometriesSection \| null` | no | `None` | Section `geometries`: which geometry kinds it accepts. |
| `params` | `ParamsSection \| null` | no | `None` | Section `params`: the parameters, and a schema reference. |
| `metrics` | `list[MetricSpec] \| null` | no | `None` | Section `metrics`: the engineering scalars it reports. |
| `artifacts` | `list[ArtifactSpec] \| null` | no | `None` | Section `artifacts`: files a solve may write. |
| `cost` | `CostSection \| null` | no | `None` | Section `cost`: whether a request can be sized in advance. |
| `features` | `CapabilityFeatures \| null` | no | `None` | Section `features`: sweep, gradient and MPI support. |
| `requirements` | `RequirementsSection \| null` | no | `None` | Section `requirements`: declared imports and their state here. |
| `examples` | `list[CapabilityExample] \| null` | no | `None` | Section `examples`: known-good parameter sets. |

## `GeometriesSection`

The `geometries` section: which geometry kinds this capability accepts.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `kinds` | `list[str]` | yes |  | Accepted `type` discriminator values, e.g. `["regions2d"]`. |
| `note` | `str` | yes |  | Where the geometry schemas live. They are protocol-level rather than per-capability — every capability accepting `regions2d` accepts the same `regions2d` — so they are not repeated here. |

## `ParamsSection`

The `params` section: a compact parameter list, plus a way to get the real schema.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `params` | `list[ParamSummary]` | yes |  | One entry per top-level parameter. |
| `schema_ref` | `str` | yes |  | Opaque reference to the full JSON Schema, `schema:params/<capability>`. Resolve it with `capability.schema` — over HTTP, `GET /api/v1/capabilities/{name}/schema`. |
| `json_schema` | `dict[str, Any] \| null` | no | `None` | The full JSON Schema, present only when `inline_schemas` was requested. Named `json_schema` because `schema` shadows a pydantic attribute. |

## `ParamSummary`

One parameter, flattened out of the JSON Schema into a line a caller can act on.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Parameter key as submitted in `params`. |
| `type` | `str` | yes |  | JSON Schema type, or `enum` for a closed set of values. |
| `required` | `bool` | yes |  | True when there is no default and it must be supplied. |
| `default` | `Any \| null` | no | `None` | Value used when it is omitted. |
| `description` | `str \| null` | no | `None` | What it controls. |
| `minimum` | `float \| null` | no | `None` | Inclusive or exclusive lower bound. |
| `maximum` | `float \| null` | no | `None` | Inclusive or exclusive upper bound. |
| `choices` | `list[Any] \| null` | no | `None` | Allowed values, when the parameter is an enum. |

## `MetricSpec`

A scalar engineering quantity this capability reports (roadmap M2.5, issue #43).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Identifier a caller asks for, e.g. `speed_max`. |
| `unit` | `str` | yes |  | Unit as a display string — `"T"`, `"K"`, `"m/s"`; `"1"` if dimensionless. |
| `description` | `str` | yes |  | What the number means, in one line. |
| `field` | `str \| null` | no | `None` | Result field this metric reduces, when it is a reduction of one. Null when the solver computes it some other way (an integral over a region, a ratio). |
| `reduction` | `'max' \| 'min' \| 'mean' \| 'integral' \| null` | no | `None` | How `field` becomes a scalar. Null exactly when `field` is null. |

## `ArtifactSpec`

A file this capability may write alongside the inline result (issue #43).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Filename the solver registers, e.g. `solution.vtk`. |
| `content_type` | `str` | yes |  | MIME type it is served with. |
| `description` | `str` | yes |  | What the file contains, and what opens it. |
| `when` | `str \| null` | no | `None` | Name of the boolean param that has to be true for this file to appear; null if it is always written. |

## `CostSection`

The `cost` section: can this request be sized before it is submitted?

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `estimates_cells` | `bool` | yes |  | True when the adapter implements `estimate_cells`, so an over-budget job is refused at submit with a number. False means the wall-clock timeout is the only backstop. |
| `max_cells` | `int` | yes |  | This server's cell budget; 0 when disabled. |
| `job_timeout_seconds` | `float` | yes |  | This server's wall-clock ceiling on one solve; 0 when disabled. |

## `CapabilityFeatures`

What a capability supports beyond a single solve (issue #43).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `sweep` | `bool` | no | `False` | Can be driven by a parameter study without a bespoke driver. |
| `gradient` | `bool` | no | `False` | Reports derivatives of a metric with respect to its params. |
| `mpi` | `bool` | no | `False` | Produces a correct result when the process is launched under `mpirun`. False for the FEniCSx adapters too: they mesh on `COMM_WORLD` but serialise partition-local arrays, so a multi-rank run would return one rank's slice. |

## `RequirementsSection`

The `requirements` section: what this capability needs, and what is here.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `packages` | `list[PackageInfo]` | yes |  | The adapter's declared imports, with their state in this process. |
| `satisfied` | `bool` | yes |  | True when every declared requirement imported. Always true for a listed capability; false would mean the registry and the process disagree. |

## `CapabilityExample`

A known-good parameter set, so a caller need not invent one (issue #43).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `title` | `str` | yes |  | Short label, e.g. `fast preview`. |
| `description` | `str` | yes |  | When to reach for this parameter set. |
| `params` | `dict[str, Any]` | yes |  | Params as submitted, valid against `Params`. |
