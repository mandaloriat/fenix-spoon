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
 */
export const PROTOCOL_VERSION = '1.1';

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

export interface Polygon2D {
  type: 'polygon2d';
  /** At least 3 vertices, implicitly closed, and non-self-intersecting. */
  points: [number, number][];
}

/** `[xmin, ymin, xmax, ymax]` */
export type Bounds2D = [number, number, number, number];

/** A rectangular domain with an obstacle cut out of it (flow around a body). */
export interface Domain2D {
  type: 'domain2d';
  bounds: Bounds2D;
  obstacle: Polygon2D;
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
}

export type Geometry = Domain2D | Regions2D;
export type GeometryKind = Geometry['type'];

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

export interface JobRequest {
  solver: string;
  geometry: Geometry;
  params?: Record<string, unknown>;
}

export interface JobCreated {
  job_id: string;
  status: JobState;
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

export interface ArtifactRef {
  name: string;
  content_type: string;
  size: number;
  /** Server-relative; join with the client's base URL to download. */
  url: string;
}

/**
 * What the solve actually cost. Every key is optional and server-defined: `cells` and
 * `seconds` are conventional, adapters add their own (`iterations`, `dofs`). Read it for
 * display, never branch on a key being present.
 */
export type JobStats = Record<string, number>;

export interface Grid2DResult {
  job_id: string;
  kind: 'grid2d';
  data: Grid2DData;
  stats: JobStats;
  artifacts: ArtifactRef[];
}

export interface Mesh2DResult {
  job_id: string;
  kind: 'mesh2d';
  data: Mesh2DData;
  stats: JobStats;
  artifacts: ArtifactRef[];
}

export type JobResult = Grid2DResult | Mesh2DResult;
export type ResultKind = JobResult['kind'];

export function isGrid2D(result: JobResult): result is Grid2DResult {
  return result.kind === 'grid2d';
}

export function isMesh2D(result: JobResult): result is Mesh2DResult {
  return result.kind === 'mesh2d';
}

/**
 * Read a scalar field from either result kind without narrowing at the call site —
 * useful for viewers that render both.
 */
export function fieldValues(result: JobResult, name: string): number[] | undefined {
  return result.kind === 'grid2d'
    ? result.data.fields[name]
    : result.data.point_fields[name];
}

/** Names of the scalar fields carried by a result. */
export function fieldNames(result: JobResult): string[] {
  return Object.keys(
    result.kind === 'grid2d' ? result.data.fields : result.data.point_fields,
  );
}
