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

The 202 from `POST /api/v1/jobs`. The job has been accepted; it may already be done.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | Use it to poll status, stream events and fetch the result. |
| `status` | `str` | yes |  | `queued` for work that will run. **Since protocol 1.4 it can be `done` or `running` immediately**: an identical solve is answered from the result cache (#47), and what comes back is the job that already has the answer. |
| `cached` | `bool` | no | `False` | True when this submission was answered from an earlier identical solve rather than starting one. Added in protocol 1.4. Still a `202`: the submission was accepted, and giving it a different status code would be reusing a code to mean something new. |

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
| `kind` | `'grid2d' \| 'mesh2d' \| 'series1d'` | yes |  | Selects the schema of `data`: `Grid2DData`, `Mesh2DData` or `Series1DData`. `series1d` was added in protocol 1.5 for answers that are curves rather than fields — a sweep, a convergence history. |
| `data` | `dict[str, Any]` | yes |  | The field data, shaped according to `kind`. |
| `stats` | `dict[str, float]` | no | `{}` | What the solve cost — `cells`, `dofs`, `iterations`, `seconds`. Keys are server-defined and all optional: display them, never branch on one existing. |
| `metrics` | `dict[str, float]` | no | `{}` | The engineering answer: the scalars this capability declared, keyed by the names `GET /capabilities/{name}?sections=metrics` reports. Added in protocol 1.3. Distinct from `stats`, which is what the solve *cost* — `t_max` and `t_rise` moved here from `stats` in 1.3 for exactly that reason. |
| `diagnostics` | `dict[str, Any]` | no | `{}` | How the solve went: `converged`, `residual`, `warnings`. Added in protocol 1.3, because `stats` is typed `dict[str, float]` and a flag or a warning string had nowhere to go in it. |
| `provenance` | `dict[str, Any]` | no | `{}` | Where the answer came from: `cached`, the solver and its declared version, the content-addressed `cache_key`, when it was computed, and the workspace object revisions it resolved. Added in protocol 1.4. `cached` is the one to read — it is the difference between a number that reflects the edit you just made and one answering a question you asked earlier. |
| `series` | `list[Series1DData]` | no | `[]` | Curves this solve produced *alongside* its field — the airfoil adapters return the flow field and the surface `C_p` from one solve. Added in protocol 1.5. Empty on a result that carries none, and empty on a `series1d` result, whose curves are in `data` because that is what `kind` selects. |
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

## `Series1DData`

A named set of curves sharing an abscissa: the `series1d` payload, or one entry of a field result's `series` list.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Identifier for this curve set, e.g. `surface_cp`. |
| `description` | `str` | no | `''` | One line on what the curves are, for a legend or a caption. |
| `x` | `SeriesAxis \| null` | no | `None` | Abscissa shared by every trace that does not carry its own. May be null only when all of them do. |
| `traces` | `list[SeriesTrace]` | yes |  | The curves, at least one. |

## `SeriesAxis`

The abscissa of a curve: what is on the x axis, in what unit, at which samples.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Axis label, e.g. `x/c`, `alpha`, `cells`. |
| `unit` | `str` | yes |  | Unit as a display string — `"deg"`, `"m"`, `"Hz"`; `"1"` if dimensionless. |
| `values` | `list[float]` | yes |  | Sample positions, in the order they should be drawn. Monotonic for a sweep or a convergence history; **not** for a surface distribution, which traverses a closed contour and revisits x. |

## `SeriesTrace`

One curve. `values` line up with the shared abscissa unless it brings its own `x`.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Identifier for this curve, e.g. `cp_upper`. |
| `unit` | `str` | yes |  | Unit as a display string; `"1"` if dimensionless. |
| `values` | `list[float]` | yes |  | One ordinate per abscissa sample, in that order. |
| `x` | `SeriesAxis \| null` | no | `None` | This trace's own abscissa, when it is not sampled like its siblings. Null means it uses the series-level `x`, which is the common case. |

## `ArtifactRef`

A downloadable file the solver produced alongside the inline result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Bare filename, e.g. `solution.vtk`. |
| `content_type` | `str` | yes |  | MIME type to serve it with. |
| `size` | `int` | yes |  | Size on disk in bytes. |
| `url` | `str` | yes |  | Server-relative download path; join with the API base URL. |
| `t` | `float \| null` | no | `None` | The instant this file holds, for a time-dependent solve; null for everything else. Added in protocol 1.7 (#86) — the artifacts carrying one are the result's `frames`, and putting the time on the file itself is what makes the index and the files unable to disagree. |


# Compact results

## `LeveledResult`

What `result.get` returns. Unrequested levels are absent, not null.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | The job this describes. |
| `solver` | `str` | yes |  | Capability that produced it. |
| `status` | `StatusView \| null` | no | `None` | Level `status`. |
| `metrics` | `dict[str, float] \| null` | no | `None` | Level `metrics`: the declared engineering scalars, keyed by the names `capability.describe` reports. Empty if the capability declares none. |
| `diagnostics` | `DiagnosticsView \| null` | no | `None` | Level `diagnostics`. |
| `provenance` | `Provenance \| null` | no | `None` | Level `provenance`. |
| `series` | `list[Series1DData] \| null` | no | `None` | Level `series`: the curves this solve produced. An empty list means the capability produced none, which is a different answer from the level being absent because nobody asked for it. |
| `fields` | `FieldsView \| null` | no | `None` | Level `fields`. |
| `artifacts` | `list[ArtifactView] \| null` | no | `None` | Level `artifacts`. |

## `StatusView`

The `status` level: did it finish, when, and why not.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `status` | `str` | yes |  | `done`, `failed` or `cancelled`. |
| `error` | `str \| null` | no | `None` | Why it failed; null otherwise. |
| `created_at` | `str` | yes |  | When the job was accepted (RFC 3339, UTC). |
| `finished_at` | `str \| null` | no | `None` | When it reached a terminal status. |
| `seconds` | `float \| null` | no | `None` | Wall-clock seconds the solve took, when the run recorded it. |

## `DiagnosticsView`

The `diagnostics` level: what the solve cost and how well it went.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `stats` | `dict[str, float]` | yes |  | What it cost: `cells`, `dofs`, `iterations`, `seconds`. Server-defined. |
| `converged` | `bool \| null` | no | `None` | Whether an iterative solve reached tolerance. Null where it does not apply. |
| `residual` | `float \| null` | no | `None` | Final residual, when iterative. |
| `warnings` | `list[str]` | no | `[]` | Non-fatal things worth knowing about this solve. |

## `Provenance`

Where a result came from (roadmap M2.5, issue #47).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | The job that produced these numbers. |
| `cached` | `bool` | yes |  | True when this answer came from an earlier identical solve rather than from one run for this request. The single most useful bit in an iterative loop. |
| `solver` | `str` | yes |  | Capability that ran it. |
| `solver_version` | `str` | yes |  | The adapter's declared `version` — what a cache key is invalidated by. |
| `cache_key` | `str \| null` | no | `None` | Content-addressed identity of the inputs. Null when this solve was not cacheable: the adapter does not declare itself deterministic, or the server has caching switched off. |
| `computed_at` | `str \| null` | no | `None` | When the underlying solve finished (RFC 3339, UTC). On a cache hit this is older than the request, and how much older is the point. |
| `seconds` | `float \| null` | no | `None` | What the original solve cost in wall-clock seconds. |
| `environment` | `dict[str, str]` | no | `{}` | Package versions the answer depends on, as they were when it ran. Part of the cache key, so a dolfinx upgrade produces a different key rather than a stale hit. |
| `inputs` | `dict[str, Any]` | no | `{}` | Pinned workspace object revisions this job resolved, when it came from a design: `design:d-1@2`, `geometry:g-1@3`. Empty for an inline submission — the design → job → result relation, recorded where it is knowable. |

## `FieldsView`

The `fields` level: the arrays, in full, because somebody asked for them by name.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `kind` | `str` | yes |  | `grid2d` or `mesh2d`; selects the shape of `data`. |
| `data` | `dict[str, Any]` | yes |  | The full field payload. |

## `ArtifactView`

One file, by reference. The `artifacts` level never inlines bytes.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `t` | `float \| null` | no | `None` | The instant this file holds, for a time-dependent solve; null otherwise. The artifacts carrying one are the result's frames (#86). |
| `name` | `str` | yes |  | Bare filename, e.g. `solution.vtk`. |
| `content_type` | `str` | yes |  | MIME type. |
| `size` | `int` | yes |  | Size on disk in bytes. |
| `path` | `str` | yes |  | Absolute path on the machine that ran it. A local caller opens this directly; an HTTP adapter replaces it with a URL to a route it serves. |

## `FieldQuery`

One bounded question about one field.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `field` | `str` | yes |  | Scalar field name, as the result carries it (`T`, `speed`). |
| `op` | `'max' \| 'min' \| 'mean' \| 'integral' \| 'at_point' \| 'over_region' \| 'section' \| 'sample' \| 'hotspots'` | yes |  | What to compute. |
| `at` | `list[float] \| null` | no | `None` | `[x, y]` for `at_point`. |
| `start` | `list[float] \| null` | no | `None` | `[x, y]` start for `section`. |
| `end` | `list[float] \| null` | no | `None` | `[x, y]` end for `section`. |
| `samples` | `int \| null` | no | `None` | Sample budget for `section` and `sample`. Capped server-side — an uncapped budget is the whole field with extra steps. |
| `count` | `int \| null` | no | `None` | How many `hotspots` to return. |
| `minimum` | `bool` | no | `False` | For `hotspots`: find the coldest rather than the hottest. |
| `region` | `str \| null` | no | `None` | Region name for `over_region`. Resolved against the geometry the job recorded in its workspace provenance. |

## `FieldQueryResult`

A query's answer. `value` is whatever the operation returns — a scalar or a short list.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_id` | `str` | yes |  | Job that was queried. |
| `field` | `str` | yes |  | Field that was queried. |
| `op` | `str` | yes |  | Operation that was run. |
| `result` | `dict[str, Any]` | yes |  | The answer. `max`/`min` give `value` and `at`; `mean`/`integral` give `value`; `section`/`sample` give short parallel arrays; `hotspots` gives a handful of points. Every one is bounded. |


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
| `cache` | `CacheInfo \| null` | no | `None` | Content-addressed result cache state (#47). Null on a server too old to have one, which is why it stays optional — a caller can tell 'no cache here' from 'this server did not say'. |

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

## `CacheInfo`

State of the content-addressed result cache (roadmap M2.5, issue #47).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `enabled` | `bool` | yes |  | Whether identical resubmissions are reused (`FENIXSPOON_CACHE`). |
| `scheme` | `str` | yes |  | Version of the hashing rule. Keys from a different scheme are never matched against these, because they describe inputs by a rule that no longer applies. |
| `cacheable_capabilities` | `list[str]` | yes |  | Capabilities that declare themselves deterministic and so may be cached. The rest always recompute — see `Solver.deterministic`. Listed rather than counted because 'why did my resubmission not hit' is the question this answers. |
| `retention` | `str` | yes |  | What expires a cache entry. The entry *is* the job, so the job TTL is the cache TTL; there is no second lifetime to reason about. |


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
| `assumptions` | `list[Assumption] \| null` | no | `None` | Section `assumptions`: the modelling assumptions in force, and the quantities they put out of reach. Next to `metrics` because it is what qualifies them. |
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
| `boundary` | `str \| null` | no | `None` | Boundary this metric integrates over, when it is a boundary integral rather than a reduction of a field — `body` for the surface of a `domain2d` obstacle. Mutually exclusive with `field`: a quantity is one or the other. |
| `over` | `'payload' \| 'run'` | no | `'payload'` | What the number is taken over. `payload` — the result that came back, which for a steady solve is the whole answer and for a transient is the final instant. `run` — the whole solve: a peak over its history, a time to reach a level, or a property of the configuration. A `run` metric can only be supplied by the adapter, because it is not in the payload to be reduced. |

## `Assumption`

A modelling assumption in force, and where it stops applying (issue #70).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Short identifier a caller can filter on, e.g. `inviscid`. |
| `statement` | `str` | yes |  | What is assumed, and where it stops holding, in one or two sentences. |
| `quantity` | `str \| null` | no | `None` | Physical quantity the limit applies to, when the assumption has a numeric edge — `mach`, `reynolds`, `b_max`. Null for a structural assumption like `2-D`. |
| `limit` | `float \| null` | no | `None` | Value of `quantity` at which the assumption fails. Null when there is no single number, which is most of them. |
| `comparator` | `'<' \| '<=' \| '>' \| '>=' \| null` | no | `None` | Which side of `limit` is valid: `<` means the assumption holds while `quantity` is below it. Null exactly when `limit` is null. |
| `excludes` | `list[str]` | no | `[]` | Quantities this assumption puts out of reach entirely — `drag` for an inviscid model. A caller asking for one of these should be told no, not given a zero. |
| `when` | `str \| null` | no | `None` | Name of the boolean param that puts this assumption in force. Null — the usual case — means it always applies. Read `when_value` for which setting arms it. |
| `when_value` | `bool` | no | `True` | The value of `when` that puts this assumption in force. `false` is how an assumption in force when a feature is *disabled* is declared. Ignored when `when` is null. |

## `ArtifactSpec`

A file this capability may write alongside the inline result (issue #43).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `str` | yes |  | Filename the solver registers, e.g. `solution.vtk`. May contain `{index}` for a file written once per stored instant — `frame_{index}.vtk` — which is how a transient declares an output whose count depends on its parameters (#86). |
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
