/**
 * TypeScript mirror of the Fenix Spoon wire protocol (docs/04-wire-protocol.md).
 *
 * The pydantic models in `server/fenixspoon/` are the source of truth; these types
 * mirror them, and the shared fixture corpus in `protocol/fixtures/` is validated
 * against *both* sides in CI so they cannot drift silently.
 */

/**
 * The wire-contract version this SDK is written against, `MAJOR.MINOR`.
 *
 * MAJOR is the compatibility boundary and is mirrored in the path (`/api/v1`); MINOR
 * changes are additive, so a client built for 1.0 keeps working against a 1.3 server.
 * `checkProtocolCompatibility` is the check; the shared fixture corpus asserts this
 * constant matches the server's `PROTOCOL_VERSION`, so the two cannot drift apart.
 *
 * 1.1 added vector fields to both result kinds, which this SDK carries. 1.2 added the
 * progressive discovery operations (`/environment`, `/capabilities`); those serve local
 * callers rather than browsers, so the SDK tracks the version without typing them — the
 * fixtures for those payloads are validated on the server side only, and say so.
 *
 * 1.3 added `metrics` and `diagnostics` to the result envelope, which this SDK does type:
 * a metric is the number a page shows, and the heat-sink demo reads its temperature rise
 * from there now that `stats` no longer carries it.
 *
 * 1.4 added `provenance` and the result cache. Two things a browser client should know:
 * `POST /jobs` can now come back `done` immediately with `cached: true`, so a page that
 * assumes a fresh submission is always `queued` will wait for a transition that already
 * happened; and `provenance.cached` says whether the numbers on screen were computed for
 * this request.
 *
 * 1.5 added the `series1d` result kind and the `series` key beside `data`, both typed here.
 * A curve *does* have a browser consumer — `<fs-viewer>` draws fields, and `C_p(x/c)` needs a
 * plot — which is why this half of 1.5 is mirrored where the 1.2 discovery operations are only
 * tracked. It also added the `assumptions` section to `capability.describe`, which is a local
 * caller's concern and is not typed here for the same reason the rest of discovery is not.
 *
 * 1.9 said what happens *on* a named boundary: a `load_case` workspace object, and
 * `conditions` on a submission. Typed here on `JobRequest`, because the editor is the place
 * a boundary gets named and it would be odd to let a page name one and not load it. The
 * values are an open map of scalars per capability — deliberately not an enum, so a new
 * physics is not a protocol change — which is why `ConditionValues` is `Record<string,
 * number>` rather than a union this file would have to grow.
 *
 * 1.13 added the `axisymmetric2d` geometry kind — a meridian (r, z) half-section of a body of
 * revolution — and it is typed here because the geometry union is a shape this file already
 * carries and the validator already enforces its rules. What it does *not* add is a field
 * saying the horizontal coordinate is a radius: the discriminator says that, and
 * `axisLabels()` below is the one place the mapping lives. See ADR 0003.
 */
export const PROTOCOL_VERSION = '1.13';

/** What `GET /api/v1/version` returns. The one endpoint that never requires a key. */
export interface ProtocolVersion {
  /** Wire-contract version as `MAJOR.MINOR`. */
  protocol: string;
  /** Version of the server package, independent of the protocol version. */
  implementation: string;
  /** Path prefix this protocol version is served under. */
  api_path: string;
}

export interface CompatibilityVerdict {
  compatible: boolean;
  /** Present when incompatible, or when the server is newer in a way worth knowing. */
  reason?: string;
}

/**
 * Compare a server's protocol version against the one this SDK was written for.
 *
 * A different MAJOR is incompatible: the path would differ too, so the request would not
 * have reached this version of the contract anyway. A *higher* MINOR on the server is
 * fine — additive by definition — and is reported so a caller can mention that newer
 * features exist. A higher MINOR on the *client* is the interesting case: the SDK may
 * send a field the server does not know, so it is flagged rather than silently allowed.
 */
export function checkProtocolCompatibility(
  serverVersion: string,
  sdkVersion: string = PROTOCOL_VERSION,
): CompatibilityVerdict {
  // Both halves required, both integers. Defaulting a missing or unparseable minor to 0
  // was a guess dressed as a parse: `1.banana` came out as `{major: 1, minor: 0}` and
  // compared *equal* to 1.0, so a server announcing nonsense read as fully compatible.
  const parse = (v: string) => {
    const match = /^(\d+)\.(\d+)$/.exec(v);
    return match ? { major: Number(match[1]), minor: Number(match[2]) } : null;
  };
  const server = parse(serverVersion);
  const sdk = parse(sdkVersion);
  if (server === null || sdk === null) {
    return {
      compatible: false,
      reason:
        `cannot parse ${server === null ? "the server's" : "the SDK's"} protocol version ` +
        `${JSON.stringify(server === null ? serverVersion : sdkVersion)}; expected MAJOR.MINOR`,
    };
  }
  if (server.major !== sdk.major) {
    return {
      compatible: false,
      reason:
        `server speaks protocol ${serverVersion}, this SDK speaks ${sdkVersion}; ` +
        'a major difference is a breaking change and the path differs too',
    };
  }
  if (server.minor < sdk.minor) {
    return {
      compatible: true,
      reason:
        `server is at ${serverVersion}, older than this SDK's ${sdkVersion}; ` +
        'requests using features added after ' + serverVersion + ' will be rejected',
    };
  }
  if (server.minor > sdk.minor) {
    return {
      compatible: true,
      reason: `server is at ${serverVersion}, newer than this SDK's ${sdkVersion}; additive only`,
    };
  }
  return { compatible: true };
}

// ---------------------------------------------------------------- geometry

/** `[xmin, ymin, xmax, ymax]` */
export type Bounds2D = [number, number, number, number];

export interface Polygon2D {
  type: 'polygon2d';
  /** At least 3 vertices, implicitly closed, and non-self-intersecting. */
  points: [number, number][];
  /**
   * Stable identifiers for the vertices, one per point in the same order (protocol 1.8).
   *
   * Optional, and absent unless a boundary is being named. They exist because *indices*
   * cannot survive editing: insert a control point and every index after it shifts, moving a
   * named boundary onto a different edge with nothing to notice. An editor that carries these
   * through an edit is what makes `PointsSelector` mean the same edge afterwards.
   */
  point_ids?: string[] | null;
}

/** The outer rectangle, a hole, or a named region's outline. */
export interface PartSelector {
  type: 'part';
  /** `outer`, `obstacle`, or `region:<name>`. */
  of: string;
}

/** The edges spanned by named vertices — the selector that follows the shape. */
export interface PointsSelector {
  type: 'points';
  ids: string[];
}

/** Everything within `tol` of a coordinate value — the selector that follows the space. */
export interface NearSelector {
  type: 'near';
  axis: 'x' | 'y';
  value: number;
  tol?: number;
}

/** Everything inside an axis-aligned rectangle. */
export interface BoxSelector {
  type: 'box';
  bounds: Bounds2D;
}

/** Intersection of the selectors it holds. Deliberately not an expression language. */
export interface AllOfSelector {
  type: 'all_of';
  of: BoundarySelector[];
}

export type BoundarySelector =
  | PartSelector
  | PointsSelector
  | NearSelector
  | BoxSelector
  | AllOfSelector;

/**
 * A named piece of a geometry's boundary (protocol 1.8).
 *
 * The geometry says *where*; what happens there belongs to a load case, so that three load
 * cases can share one shape instead of minting a geometry revision per load change.
 */
export interface BoundarySpec {
  name: string;
  select: BoundarySelector;
  description?: string | null;
}

/** A rectangular domain with an obstacle cut out of it (flow around a body). */
export interface Domain2D {
  type: 'domain2d';
  bounds: Bounds2D;
  obstacle: Polygon2D;
  /** Named pieces of the boundary a load case can refer to; empty unless used. */
  boundaries?: BoundarySpec[];
}

/**
 * A named material region. `material` is an open dict of scalars: the protocol is
 * physics-agnostic and each solver documents the keys it reads, ignoring the rest.
 */
export interface Region2D {
  name: string;
  shape: Polygon2D;
  material?: Record<string, number>;
}

/**
 * A rectangular domain filled with material regions over a background material.
 * Regions may be nested — later entries win where they overlap (painter's order).
 */
export interface Regions2D {
  type: 'regions2d';
  bounds: Bounds2D;
  regions: Region2D[];
  background?: Record<string, number>;
  /** Named pieces of the boundary a load case can refer to; empty unless used. */
  boundaries?: BoundarySpec[];
}

/**
 * A meridian (r, z) half-section of a body of revolution (protocol 1.13).
 *
 * The same filled region map as `Regions2D`, read against different physics: `bounds` is
 * `[rmin, zmin, rmax, zmax]` with `rmin >= 0`, and a solver integrates with the cylindrical
 * `r` weight — so a result is for the whole revolved body, not per unit depth.
 *
 * A region may lie *on* r = 0 when the section reaches the axis (a solid shaft is bounded by
 * it); a section that does not reach the axis is equally legitimate.
 */
export interface Axisymmetric2D {
  type: 'axisymmetric2d';
  /** `[rmin, zmin, rmax, zmax]`, in metres, with `rmin >= 0`. */
  bounds: Bounds2D;
  regions: Region2D[];
  background?: Record<string, number>;
  /** Named pieces of the boundary a load case can refer to; empty unless used. */
  boundaries?: BoundarySpec[];
}

export type Geometry = Domain2D | Regions2D | Axisymmetric2D;
export type GeometryKind = Geometry['type'];

/**
 * What the two coordinates of a geometry *are*, for a page that draws or labels one.
 *
 * A meridian section drawn on axes marked x and y teaches the wrong picture, and a viewer
 * cannot infer the difference — so it is read off the discriminator, which is the one claim
 * the server refuses payloads over (`rmin >= 0`, the `r` weight in the integrand). Here
 * rather than in each page for the reason [ADR 0003](../../../docs/adr/0003-axisymmetric-axis-label.md)
 * gives: two implementations of one mapping is how they come to disagree, silently.
 *
 * Labels, not units or display names — those stay the page's, per ADR 0001.
 */
export function axisLabels(geometry: Pick<Geometry, 'type'>): readonly ['r', 'z'] | readonly ['x', 'y'] {
  return geometry.type === 'axisymmetric2d' ? (['r', 'z'] as const) : (['x', 'y'] as const);
}

// ----------------------------------------------------------------- solvers

export interface SolverInfo {
  name: string;
  title: string;
  description: string;
  geometry_types: GeometryKind[];
  /** JSON Schema for this solver's params — drive your UI forms from it. */
  params_schema: Record<string, unknown>;
}

// -------------------------------------------------------------------- jobs

export type JobState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

/** States a job never leaves. */
export const TERMINAL_STATES = ['done', 'failed', 'cancelled'] as const;
export type TerminalState = (typeof TERMINAL_STATES)[number];

export function isTerminal(status: JobState): status is TerminalState {
  return (TERMINAL_STATES as readonly string[]).includes(status);
}

/**
 * What a load case says on one boundary: an open map of scalars, per capability.
 *
 * Open on purpose (protocol 1.9). A typed union of condition kinds would put physics into
 * the protocol, so every new physics would become a protocol change — the coupling the
 * server has consistently refused. Read `capability.describe(["conditions"])` for the keys a
 * given capability accepts and their units; a key it does not declare is refused at submit
 * rather than ignored, which is what keeps the openness from costing a silent typo.
 */
export type ConditionValues = Record<string, number>;

/** The form every version before 1.10 accepted, and the only one before the workspace. */
export interface InlineJobRequest {
  solver: string;
  geometry: Geometry;
  params?: Record<string, unknown>;
  /**
   * An inline load case (protocol 1.9): boundary name to the scalars in force there. Every
   * name must be one this geometry's `boundaries` declares — the refusal is a 422, not a
   * silent no-op, because a condition applied to nothing produces a solve that runs and
   * answers a different problem.
   */
  conditions?: Record<string, ConditionValues>;
  design?: never;
}

/** Protocol 1.10: name a design instead of describing a solve. */
export interface DesignJobRequest {
  /**
   * A `design` reference — `design:d-18`, optionally pinned. The design names its geometry,
   * params and load cases, so none of the inline fields are used beside it.
   *
   * Prefer this in a page that iterates: an inline geometry is resent whole on every solve,
   * can never hit the result cache on an unchanged design, and leaves the result unable to
   * say which design revision produced it.
   */
  design: string;
  solver?: never;
  geometry?: never;
  params?: never;
  conditions?: never;
}

/**
 * A union, rather than one interface with everything optional — which is what this was until a
 * review of #21 pointed out that the loose form types `{ params: {} }` as a valid request. The
 * server has refused that since 1.10, it being neither form, and a client type that permits
 * what the server refuses is a type that has stopped describing the protocol. The `?: never`
 * arms are what make "one form or the other, never both" a compile error rather than a 422 a
 * round trip later.
 */
export type JobRequest = InlineJobRequest | DesignJobRequest;

export interface JobCreated {
  job_id: string;
  status: JobState;
  /**
   * True when an identical solve had already been run and this submission was answered
   * from it (protocol 1.4). `status` is then already terminal — do not wait for a
   * `queued` → `running` transition that will not come.
   */
  cached?: boolean;
}

/** Where a result came from, and whether anything ran for it. Protocol 1.4. */
export interface JobProvenance {
  job_id: string;
  /** The bit worth reading: false means these numbers were computed for this request. */
  cached: boolean;
  solver: string;
  solver_version: string;
  /** Content-addressed identity of the inputs; null when the solve was not cacheable. */
  cache_key?: string | null;
  computed_at?: string | null;
  seconds?: number | null;
  environment?: Record<string, string>;
  /** Pinned workspace object revisions, when the job came from a design. */
  inputs?: Record<string, unknown>;
}

export interface JobStatus {
  job_id: string;
  solver: string;
  status: JobState;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

/** One page of job history, newest first. `total` counts every stored job, not the page. */
export interface JobPage {
  jobs: JobStatus[];
  total: number;
  limit: number;
  offset: number;
}

// ------------------------------------------------------------------ events

export interface ProgressEvent {
  type: 'progress';
  iteration: number;
  total?: number | null;
  residual?: number | null;
  message?: string | null;
}

export interface StatusEvent {
  type: 'status';
  status: Exclude<JobState, 'queued'>;
  error?: string | null;
}

export type JobEvent = ProgressEvent | StatusEvent;

export function isStatusEvent(event: JobEvent): event is StatusEvent {
  return event.type === 'status';
}

export function isProgressEvent(event: JobEvent): event is ProgressEvent {
  return event.type === 'progress';
}

// ----------------------------------------------------------------- results

/** Fields sampled on a regular grid. Arrays are row-major, index `iy * nx + ix`, y up. */
export interface Grid2DData {
  bounds: Bounds2D;
  /** `[ny, nx]` */
  shape: [number, number];
  fields: Record<string, number[]>;
  /** Name to one `[x, y]` pair per grid point, indexed like `fields`. Added in protocol 1.1. */
  vector_fields?: Record<string, [number, number][]>;
  /** 1 inside the obstacle. */
  mask: number[];
}

/** An unstructured triangle mesh with nodal fields. */
export interface Mesh2DData {
  bounds: Bounds2D;
  points: [number, number][];
  triangles: [number, number, number][];
  point_fields: Record<string, number[]>;
  /** Name to one `[x, y]` pair per node, in `points` order. Added in protocol 1.1. */
  point_vector_fields?: Record<string, [number, number][]>;
  cell_fields?: Record<string, number[]>;
}

/**
 * The abscissa of a curve: what is on the x axis, in what unit, at which samples.
 *
 * Units travel on the wire for curves and not for fields, which looks inconsistent and is not:
 * a field is coloured and `<fs-viewer>` takes its colourbar caption from an attribute the page
 * sets, while a curve is drawn with axis labels the client has no other way to know. Added in
 * protocol 1.5.
 */
export interface SeriesAxis {
  /** Axis label, e.g. `x/c`, `alpha`, `cells`. */
  name: string;
  /** Display string — `deg`, `m`, `Hz`; `1` if dimensionless. */
  unit: string;
  /**
   * Sample positions in draw order. Monotonic for a sweep or a convergence history, and
   * **not** for a surface distribution, which traverses a closed contour and revisits x.
   */
  values: number[];
}

/** One curve. `values` line up with the shared abscissa unless it brings its own `x`. */
export interface SeriesTrace {
  name: string;
  unit: string;
  values: number[];
  /**
   * This trace's own abscissa, when it is not sampled like its siblings — the upper and lower
   * surface of an airfoil, which a staircase does not sample at matching stations. Absent means
   * it uses the series-level `x`.
   */
  x?: SeriesAxis | null;
}

/**
 * A named set of curves sharing an abscissa. The `series1d` payload, and what rides in a field
 * result's `series` list. Added in protocol 1.5.
 *
 * Complex values are two real traces rather than a complex type: a harmonic result is plotted
 * as a magnitude and a phase, with different units on different axes, and JSON has no complex
 * number to be faithful to.
 */
export interface Series1DData {
  name: string;
  description?: string;
  /** Shared by every trace that does not carry its own; absent only when all of them do. */
  x?: SeriesAxis | null;
  traces: SeriesTrace[];
}

/** Read the curves off a result, whichever of the two places they are in. */
export function resultSeries(result: JobResult): Series1DData[] {
  return result.kind === 'series1d' ? [result.data] : (result.series ?? []);
}

export interface ArtifactRef {
  name: string;
  content_type: string;
  size: number;
  /** Server-relative; join with the client's base URL to download. */
  url: string;
  /**
   * The instant this file holds, for a time-dependent solve; absent for everything else.
   * The artifacts carrying one are the result's `frames` (protocol 1.7).
   */
  t?: number;
}

/**
 * One stored instant of a time-dependent solve.
 *
 * The whole time dimension is this index: the field history crosses as *references*, and a
 * viewer fetches the instant it is showing rather than receiving the run. There is
 * deliberately no server-side reduction at a chosen instant — a scalar against time is a
 * `Series1DData` curve, and a field question means downloading the frame.
 */
export interface FrameRef {
  /** In the solver's time unit. */
  t: number;
  /** Name of the artifact holding it; always one this result also lists. */
  artifact: string;
}

/**
 * What the solve actually cost. Every key is optional and server-defined: `cells` and
 * `seconds` are conventional, adapters add their own (`iterations`, `dofs`). Read it for
 * display, never branch on a key being present.
 */
export type JobStats = Record<string, number>;

/**
 * The engineering answer, as opposed to what it cost. Keys are whatever the capability
 * declared — ask `GET /capabilities/{name}?sections=metrics` for their units and meanings.
 * Added in protocol 1.3; a 1.2 server sends `{}`, so read it defensively.
 *
 * This is where `t_max` and `t_rise` live now. They were `stats` keys until 1.3, which
 * conflated the cost of a solve with its result.
 */
export type JobMetrics = Record<string, number>;

/** How the solve went, as distinct from what it produced. Added in protocol 1.3. */
export interface JobDiagnostics {
  /** Whether an iterative solve reached tolerance; null where it does not apply. */
  converged?: boolean | null;
  /** Final residual, when the solver iterates. */
  residual?: number | null;
  /** Non-fatal things worth surfacing — an iteration cap hit, a coarsened mesh. */
  warnings?: string[];
}

export interface Grid2DResult {
  job_id: string;
  kind: 'grid2d';
  data: Grid2DData;
  stats: JobStats;
  metrics?: JobMetrics;
  diagnostics?: JobDiagnostics;
  provenance?: JobProvenance;
  /** Curves produced beside the field — the airfoil adapters send the surface `C_p` here. */
  series?: Series1DData[];
  artifacts: ArtifactRef[];
  /** Stored instants, in time order — derived from the artifacts carrying a `t`. */
  frames?: FrameRef[];
}

export interface Mesh2DResult {
  job_id: string;
  kind: 'mesh2d';
  data: Mesh2DData;
  stats: JobStats;
  metrics?: JobMetrics;
  diagnostics?: JobDiagnostics;
  provenance?: JobProvenance;
  series?: Series1DData[];
  artifacts: ArtifactRef[];
  /** Stored instants, in time order — derived from the artifacts carrying a `t`. */
  frames?: FrameRef[];
}

/**
 * A result whose answer *is* curves: a parameter sweep, a convergence history, a modal list.
 *
 * The curves are in `data` rather than in `series`, because `kind` selects the schema of `data`
 * and that invariant is worth more than one accessor — `resultSeries` is the accessor. A
 * `series1d` result carries no `series`; the server rejects a payload that fills both.
 */
export interface Series1DResult {
  job_id: string;
  kind: 'series1d';
  data: Series1DData;
  stats: JobStats;
  metrics?: JobMetrics;
  diagnostics?: JobDiagnostics;
  /** As on the field kinds. `GET /jobs/{id}/result` sends it for every kind, since 1.4. */
  provenance?: JobProvenance;
  series?: never[];
  artifacts: ArtifactRef[];
  /** Stored instants, in time order — derived from the artifacts carrying a `t`. */
  frames?: FrameRef[];
}

export type JobResult = Grid2DResult | Mesh2DResult | Series1DResult;
export type ResultKind = JobResult['kind'];

/** A result that is a field over a 2-D domain, as opposed to a curve. */
export type FieldResult = Grid2DResult | Mesh2DResult;

export function isGrid2D(result: JobResult): result is Grid2DResult {
  return result.kind === 'grid2d';
}

export function isMesh2D(result: JobResult): result is Mesh2DResult {
  return result.kind === 'mesh2d';
}

export function isSeries1D(result: JobResult): result is Series1DResult {
  return result.kind === 'series1d';
}

/** Whether this result carries a 2-D field, i.e. whether `<fs-viewer>` can draw it. */
export function isFieldResult(result: JobResult): result is FieldResult {
  return result.kind === 'grid2d' || result.kind === 'mesh2d';
}

/**
 * Read a scalar field from either *field* result kind without narrowing at the call site —
 * useful for viewers that render both. `undefined` for a `series1d` result, which has no
 * field: use `resultSeries` for those.
 */
export function fieldValues(result: JobResult, name: string): number[] | undefined {
  if (isGrid2D(result)) return result.data.fields[name];
  if (isMesh2D(result)) return result.data.point_fields[name];
  return undefined;
}

/** Names of the scalar fields carried by a result; empty for a `series1d`. */
export function fieldNames(result: JobResult): string[] {
  if (isGrid2D(result)) return Object.keys(result.data.fields);
  if (isMesh2D(result)) return Object.keys(result.data.point_fields);
  return [];
}

/** The abscissa a trace is drawn against: its own if it has one, else the series-level one. */
export function traceAbscissa(
  series: Series1DData,
  trace: SeriesTrace,
): SeriesAxis | undefined {
  return trace.x ?? series.x ?? undefined;
}


// --------------------------------------------------------------- workspace (1.10, 1.11)

/** The workspace object types a caller can create. */
export type ObjectType =
  | 'geometry'
  | 'material'
  | 'boundary_condition'
  | 'load_case'
  | 'design'
  | 'study'
  | 'optimization';

/** One object revision, body included — what create, get and patch return. */
export interface ObjectView {
  /** Stable identifier without a revision, e.g. `geometry:g-1`. */
  ref: string;
  /** This exact revision, `geometry:g-1@3` — quote it to freeze an input. */
  pinned: string;
  type: string;
  revision: number;
  label?: string | null;
  created_at: string;
  body: Record<string, unknown>;
}

/** One line of a workspace listing. No body: that is the payload references exist to avoid. */
export interface ObjectSummary {
  ref: string;
  type: string;
  revision: number;
  label?: string | null;
  created_at: string;
}

/** What `study.run` answers with: what was started, and what the cache made free. */
export interface StudyRun {
  study: string;
  jobs: string[];
  submitted: number;
  reused: number;
  refused: number;
}

/** One row of a study's table, whichever kind produced it. */
export interface StudyVariation {
  job_id?: string | null;
  status: string;
  cached: boolean;
  metrics: Record<string, number>;
  error?: string | null;
  /** A ladder's rung: the single value it was solved at. */
  value?: number;
  /** A sweep's point: every parameter it set. */
  values?: Record<string, number>;
}

/** How one metric behaved up a convergence ladder. */
export interface MetricConvergence {
  metric: string;
  values: (number | null)[];
  relative_change: (number | null)[];
  settled_at?: number | null;
}

/**
 * What `GET /api/v1/studies/{id}` returns, discriminated on `kind` exactly as a result is.
 *
 * A ladder reports where each metric settled; a sweep reports response curves as
 * {@link Series1DData} — the same model a `series1d` result carries, so `<fs-plot>` draws a
 * sweep with nothing in between.
 */
export type StudyReport = ConvergenceReport | SweepReport;

export interface ConvergenceReport {
  kind: 'mesh_convergence';
  study: string;
  design: string;
  parameter: string;
  solver: string;
  rungs: StudyVariation[];
  convergence: MetricConvergence[];
  complete: boolean;
}

export interface SweepReport {
  kind: 'sweep';
  study: string;
  design: string;
  parameters: string[];
  solver: string;
  points: StudyVariation[];
  curves: Series1DData[];
  complete: boolean;
}

/** The rows of a report, whichever kind it is — `rungs` on a ladder, `points` on a sweep. */
export function studyVariations(report: StudyReport): StudyVariation[] {
  return report.kind === 'sweep' ? report.points : report.rungs;
}

// ---------------------------------------------------------------- optimizations (1.12)

/** What better meant: a declared metric, and which way to move it. */
export interface Objective {
  metric: string;
  sense: 'minimize' | 'maximize' | 'target';
  target?: number | null;
}

/** One row of a search's trajectory: where it looked, and what came back. */
export interface Evaluation {
  iteration: number;
  value: number;
  job_id?: string | null;
  status: string;
  cached?: boolean;
  metric?: number | null;
  objective?: number | null;
  error?: string | null;
}

/**
 * What `POST /api/v1/optimizations/{id}/run` returns — that the search *started*.
 *
 * No job ids, unlike a study's receipt, and the absence is structural: a search chooses each
 * point from the last answer, so it cannot say in advance which solves it will ask for.
 * `started: false` means a search for this revision was already in flight and this call
 * joined it.
 */
export interface OptimizationRun {
  optimization: string;
  started: boolean;
}

/** What `GET /api/v1/optimizations/{id}` returns: the trajectory, and why it stopped. */
export interface OptimizationReport {
  optimization: string;
  design: string;
  solver: string;
  parameter: string;
  objective: Objective;
  evaluations: Evaluation[];
  best?: Evaluation | null;
  /** Where the minimum is *known* to be, which is not where the best value was seen. */
  bracket?: [number, number] | null;
  evaluations_spent: number;
  stopped: 'converged' | 'budget' | 'stalled' | 'incomplete' | 'not_run';
  /** The convergence history, drawable by `<fs-plot>` like any other `series1d`. */
  history?: Series1DData | null;
  /**
   * Whether a search is in flight **in the process that answered this call** — the one field
   * here that is not a replay of the object and its jobs. Poll on it, and read it as the
   * local statement it is: behind two API replicas a search driven by the other one reads as
   * `false` with `stopped: "incomplete"`.
   */
  running: boolean;
}
