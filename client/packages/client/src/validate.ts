/**
 * Runtime validators for protocol payloads.
 *
 * TypeScript types vanish at runtime, so these exist for two reasons: consumers get a
 * real error on a malformed payload instead of `undefined` three layers later, and the
 * shared fixture corpus in `protocol/fixtures/` can be checked against the SDK exactly
 * as it is against the server's pydantic models — the two sides cannot drift silently.
 *
 * The rules mirror the pydantic validators; where the server rejects, these reject.
 */

import type {
  Bounds2D,
  Geometry,
  JobEvent,
  JobRequest,
  JobResult,
  Polygon2D,
  Regions2D,
} from './types.js';

export class ProtocolValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ProtocolValidationError';
  }
}

function fail(message: string): never {
  throw new ProtocolValidationError(message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requireNumber(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    fail(`${what} must be a finite number, got ${JSON.stringify(value)}`);
  }
  return value;
}

/**
 * Integer fields, matching pydantic: a lossless float like `5.0` is fine (JSON has no
 * int/float distinction anyway), a fractional one like `5.5` is not.
 */
function requireInteger(value: unknown, what: string, { min }: { min?: number } = {}): number {
  const parsed = requireNumber(value, what);
  if (!Number.isInteger(parsed)) {
    fail(`${what} must be an integer, got ${JSON.stringify(value)}`);
  }
  if (min !== undefined && parsed < min) {
    fail(`${what} must be >= ${min}, got ${parsed}`);
  }
  return parsed;
}

function requireBounds(value: unknown): Bounds2D {
  if (!Array.isArray(value) || value.length !== 4) {
    fail('bounds must be [xmin, ymin, xmax, ymax]');
  }
  const [xmin, ymin, xmax, ymax] = value.map((v, i) => requireNumber(v, `bounds[${i}]`));
  if (!(xmax! > xmin!) || !(ymax! > ymin!)) {
    fail('bounds must satisfy xmin < xmax and ymin < ymax');
  }
  return [xmin!, ymin!, xmax!, ymax!];
}

function requireScalarMap(value: unknown, what: string): Record<string, number> {
  if (value === undefined) return {};
  if (!isRecord(value)) fail(`${what} must be an object of numbers`);
  for (const [key, entry] of Object.entries(value)) requireNumber(entry, `${what}.${key}`);
  return value as Record<string, number>;
}

/** Do segments p1-p2 and p3-p4 cross at an interior point of both? */
function properlyIntersect(
  p1: readonly [number, number],
  p2: readonly [number, number],
  p3: readonly [number, number],
  p4: readonly [number, number],
): boolean {
  const orient = (
    a: readonly [number, number],
    b: readonly [number, number],
    c: readonly [number, number],
  ) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const d1 = orient(p3, p4, p1);
  const d2 = orient(p3, p4, p2);
  const d3 = orient(p1, p2, p3);
  const d4 = orient(p1, p2, p4);
  return d1 > 0 !== d2 > 0 && d3 > 0 !== d4 > 0;
}

export function validatePolygon2D(value: unknown, what = 'polygon'): Polygon2D {
  if (!isRecord(value) || value.type !== 'polygon2d') fail(`${what} must have type "polygon2d"`);
  const points = value.points;
  if (!Array.isArray(points) || points.length < 3) {
    fail(`${what} needs at least 3 points`);
  }
  const parsed = points.map((point, i) => {
    if (!Array.isArray(point) || point.length !== 2) fail(`${what} point ${i} must be [x, y]`);
    return [
      requireNumber(point[0], `${what} point ${i} x`),
      requireNumber(point[1], `${what} point ${i} y`),
    ] as [number, number];
  });

  const n = parsed.length;
  for (let i = 0; i < n; i += 1) {
    const a = parsed[i]!;
    const b = parsed[(i + 1) % n]!;
    if (a[0] === b[0] && a[1] === b[1]) fail(`${what} has duplicate consecutive points at ${i}`);
  }
  // Simple polygons only: self-intersections can hang downstream meshers, so the
  // server rejects them and so do we, rather than let the round trip fail late.
  for (let i = 0; i < n; i += 1) {
    for (let j = i + 1; j < n; j += 1) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue;
      if (properlyIntersect(parsed[i]!, parsed[(i + 1) % n]!, parsed[j]!, parsed[(j + 1) % n]!)) {
        fail(`${what} must not self-intersect (edges ${i} and ${j} cross)`);
      }
    }
  }
  return { type: 'polygon2d', points: parsed };
}

function requireInsideBounds(polygon: Polygon2D, bounds: Bounds2D, what: string): void {
  const [xmin, ymin, xmax, ymax] = bounds;
  for (const [x, y] of polygon.points) {
    if (!(x > xmin && x < xmax && y > ymin && y < ymax)) {
      fail(`${what} points must lie strictly inside the domain bounds`);
    }
  }
}

export function validateGeometry(value: unknown): Geometry {
  if (!isRecord(value)) fail('geometry must be an object');
  if (value.type === 'domain2d') {
    const bounds = requireBounds(value.bounds);
    const obstacle = validatePolygon2D(value.obstacle, 'obstacle');
    requireInsideBounds(obstacle, bounds, 'obstacle');
    return { type: 'domain2d', bounds, obstacle };
  }
  if (value.type === 'regions2d') {
    const bounds = requireBounds(value.bounds);
    const regions = value.regions;
    if (!Array.isArray(regions) || regions.length < 1) fail('regions2d needs at least one region');
    const parsed = regions.map((region, i) => {
      if (!isRecord(region)) fail(`region ${i} must be an object`);
      if (typeof region.name !== 'string' || region.name.length === 0) {
        fail(`region ${i} needs a non-empty name`);
      }
      const shape = validatePolygon2D(region.shape, `region ${region.name}`);
      requireInsideBounds(shape, bounds, `region ${region.name}`);
      return {
        name: region.name,
        shape,
        material: requireScalarMap(region.material, `region ${region.name} material`),
      };
    });
    const names = new Set(parsed.map((r) => r.name));
    if (names.size !== parsed.length) fail('region names must be unique');
    for (let i = 0; i < parsed.length; i += 1) {
      for (let j = i + 1; j < parsed.length; j += 1) {
        if (outlinesCross(parsed[i]!.shape.points, parsed[j]!.shape.points)) {
          fail(
            `regions ${parsed[i]!.name} and ${parsed[j]!.name} overlap partially; ` +
              'regions must be disjoint or fully nested',
          );
        }
      }
    }
    const geometry: Regions2D = {
      type: 'regions2d',
      bounds,
      regions: parsed,
      background: requireScalarMap(value.background, 'background'),
    };
    return geometry;
  }
  fail(`unknown geometry type ${JSON.stringify(value.type)}`);
}

function outlinesCross(a: [number, number][], b: [number, number][]): boolean {
  for (let i = 0; i < a.length; i += 1) {
    for (let j = 0; j < b.length; j += 1) {
      if (properlyIntersect(a[i]!, a[(i + 1) % a.length]!, b[j]!, b[(j + 1) % b.length]!)) {
        return true;
      }
    }
  }
  return false;
}

export function validateJobRequest(value: unknown): JobRequest {
  if (!isRecord(value)) fail('job request must be an object');
  if (typeof value.solver !== 'string' || value.solver.length === 0) {
    fail('job request needs a solver name');
  }
  const geometry = validateGeometry(value.geometry);
  if (value.params !== undefined && !isRecord(value.params)) fail('params must be an object');
  return {
    solver: value.solver,
    geometry,
    params: (value.params as Record<string, unknown>) ?? {},
  };
}

const JOB_STATES = new Set(['running', 'done', 'failed', 'cancelled']);

export function validateJobEvent(value: unknown): JobEvent {
  if (!isRecord(value)) fail('event must be an object');
  if (value.type === 'progress') {
    return {
      type: 'progress',
      iteration: requireInteger(value.iteration, 'iteration'),
      total: value.total === undefined || value.total === null
        ? null
        : requireInteger(value.total, 'total'),
      residual: value.residual === undefined || value.residual === null
        ? null
        : requireNumber(value.residual, 'residual'),
      message: value.message == null ? null : String(value.message),
    };
  }
  if (value.type === 'status') {
    if (typeof value.status !== 'string' || !JOB_STATES.has(value.status)) {
      fail(`unknown job status ${JSON.stringify(value.status)}`);
    }
    return {
      type: 'status',
      status: value.status as 'running' | 'done' | 'failed' | 'cancelled',
      error: value.error == null ? null : String(value.error),
    };
  }
  fail(`unknown event type ${JSON.stringify(value.type)}`);
}

function requireNumberArray(value: unknown, what: string): number[] {
  if (!Array.isArray(value)) fail(`${what} must be an array of numbers`);
  return value.map((entry, i) => requireNumber(entry, `${what}[${i}]`));
}


/**
 * Vector fields: a map of name to `[x, y]` pairs, one per sample. Mirrors the server's
 * check exactly — the shared corpus asserts both sides reject the same payloads, and this
 * function exists because updating the *types* alone left the SDK accepting three fixtures
 * the server refuses.
 */
function requireVectorFields(
  value: unknown,
  expected: number,
  label: string,
): Record<string, [number, number][]> {
  if (value === undefined) return {};
  if (!isRecord(value)) fail(`${label} must be an object`);
  const out: Record<string, [number, number][]> = {};
  for (const [name, vectors] of Object.entries(value)) {
    if (!Array.isArray(vectors)) fail(`${label} ${name} must be an array`);
    if (vectors.length !== expected) {
      fail(`${label} ${name} has ${vectors.length} entries, expected ${expected}`);
    }
    out[name] = vectors.map((vector, i) => {
      if (!Array.isArray(vector) || vector.length !== 2) {
        fail(`${label} ${name}[${i}] must be [x, y]`);
      }
      return [
        requireNumber(vector[0], `${label} ${name}[${i}] x`),
        requireNumber(vector[1], `${label} ${name}[${i}] y`),
      ] as [number, number];
    });
  }
  return out;
}

export function validateJobResult(value: unknown): JobResult {
  if (!isRecord(value)) fail('result must be an object');
  if (typeof value.job_id !== 'string') fail('result needs a job_id');
  if (!isRecord(value.data)) fail('result needs a data object');
  const artifacts = validateArtifacts(value.artifacts);
  const stats = validateStats(value.stats);

  if (value.kind === 'grid2d') {
    const data = value.data;
    const bounds = requireBounds(data.bounds);
    if (!Array.isArray(data.shape) || data.shape.length !== 2) fail('shape must be [ny, nx]');
    const ny = requireInteger(data.shape[0], 'shape[0] (ny)', { min: 1 });
    const nx = requireInteger(data.shape[1], 'shape[1] (nx)', { min: 1 });
    const expected = ny * nx;
    if (!isRecord(data.fields)) fail('grid2d needs a fields object');
    const fields: Record<string, number[]> = {};
    for (const [name, values] of Object.entries(data.fields)) {
      const parsed = requireNumberArray(values, `field ${name}`);
      if (parsed.length !== expected) {
        fail(`field ${name} has ${parsed.length} entries, expected ny*nx=${expected}`);
      }
      fields[name] = parsed;
    }
    const vectorFields = requireVectorFields(data.vector_fields, expected, 'vector field');
    const mask = requireNumberArray(data.mask, 'mask');
    if (mask.length !== expected) {
      fail(`mask has ${mask.length} entries, expected ny*nx=${expected}`);
    }
    return {
      job_id: value.job_id,
      kind: 'grid2d',
      data: { bounds, shape: [ny, nx], fields, vector_fields: vectorFields, mask },
      stats,
      artifacts,
    };
  }

  if (value.kind === 'mesh2d') {
    const data = value.data;
    const bounds = requireBounds(data.bounds);
    if (!Array.isArray(data.points)) fail('mesh2d needs points');
    const points = data.points.map((point, i) => {
      if (!Array.isArray(point) || point.length !== 2) fail(`point ${i} must be [x, y]`);
      return [
        requireNumber(point[0], `point ${i} x`),
        requireNumber(point[1], `point ${i} y`),
      ] as [number, number];
    });
    if (!Array.isArray(data.triangles)) fail('mesh2d needs triangles');
    const triangles = data.triangles.map((triangle, i) => {
      if (!Array.isArray(triangle) || triangle.length !== 3) {
        fail(`triangle ${i} must be [i, j, k]`);
      }
      return triangle.map((index, k) => {
        const parsed = requireNumber(index, `triangle ${i}[${k}]`);
        if (!Number.isInteger(parsed) || parsed < 0 || parsed >= points.length) {
          fail(`triangle ${i} references node ${parsed} outside 0..${points.length - 1}`);
        }
        return parsed;
      }) as [number, number, number];
    });
    if (!isRecord(data.point_fields)) fail('mesh2d needs a point_fields object');
    const pointFields: Record<string, number[]> = {};
    for (const [name, values] of Object.entries(data.point_fields)) {
      const parsed = requireNumberArray(values, `point field ${name}`);
      if (parsed.length !== points.length) {
        fail(`point field ${name} has ${parsed.length} entries, expected ${points.length}`);
      }
      pointFields[name] = parsed;
    }
    const pointVectorFields = requireVectorFields(
      data.point_vector_fields,
      points.length,
      'point vector field',
    );
    return {
      job_id: value.job_id,
      kind: 'mesh2d',
      data: {
        bounds,
        points,
        triangles,
        point_fields: pointFields,
        point_vector_fields: pointVectorFields,
      },
      stats,
      artifacts,
    };
  }

  fail(`unknown result kind ${JSON.stringify(value.kind)}`);
}

function validateStats(value: unknown): Record<string, number> {
  // Absent on servers older than the field; an empty map keeps consumers from null-checking.
  if (value === undefined || value === null) return {};
  if (!isRecord(value)) fail('stats must be an object');
  const stats: Record<string, number> = {};
  for (const [key, entry] of Object.entries(value)) {
    stats[key] = requireNumber(entry, `stats.${key}`);
  }
  return stats;
}

function validateArtifacts(value: unknown) {
  if (value === undefined) return [];
  if (!Array.isArray(value)) fail('artifacts must be an array');
  return value.map((artifact, i) => {
    if (!isRecord(artifact)) fail(`artifact ${i} must be an object`);
    for (const key of ['name', 'content_type', 'url'] as const) {
      if (typeof artifact[key] !== 'string') fail(`artifact ${i} needs a string ${key}`);
    }
    return {
      name: artifact.name as string,
      content_type: artifact.content_type as string,
      size: requireInteger(artifact.size, `artifact ${i} size`, { min: 0 }),
      url: artifact.url as string,
    };
  });
}
