/**
 * Result-kind-agnostic field access, contouring and probing.
 *
 * `grid2d` and `mesh2d` describe the same thing two ways, and a viewer shouldn't care
 * which it got. These helpers normalise both to "values at positions" plus the topology
 * needed to interpolate between them — all without touching a canvas, so they're
 * directly testable.
 */

import type { Grid2DData, JobResult, Mesh2DData } from '@fenix-spoon/client';

export type Point = [number, number];
export type Segment = [Point, Point];

export function resultBounds(result: JobResult): [number, number, number, number] {
  return result.data.bounds;
}

export function resultFieldNames(result: JobResult): string[] {
  return Object.keys(
    result.kind === 'grid2d' ? result.data.fields : result.data.point_fields,
  );
}

export function resultFieldValues(result: JobResult, field: string): number[] | undefined {
  return result.kind === 'grid2d'
    ? result.data.fields[field]
    : result.data.point_fields[field];
}

export function resultMask(result: JobResult): number[] | undefined {
  return result.kind === 'grid2d' ? result.data.mask : undefined;
}

/**
 * Sample the field at a domain position, or `undefined` outside it / inside a hole.
 *
 * Grids use nearest-node lookup; meshes use barycentric interpolation inside the
 * containing triangle, which is what makes a probe readout on an unstructured mesh
 * agree with what the colours show rather than snapping to the nearest vertex.
 */
export function probe(result: JobResult, field: string, at: Point): number | undefined {
  const values = resultFieldValues(result, field);
  if (!values) return undefined;
  return result.kind === 'grid2d'
    ? probeGrid(result.data, values, resultMask(result), at)
    : probeMesh(result.data, values, at);
}

function probeGrid(
  data: Grid2DData,
  values: readonly number[],
  mask: readonly number[] | undefined,
  [x, y]: Point,
): number | undefined {
  const [xmin, ymin, xmax, ymax] = data.bounds;
  if (x < xmin || x > xmax || y < ymin || y > ymax) return undefined;
  const [ny, nx] = data.shape;
  const ix = Math.round(((x - xmin) / (xmax - xmin)) * (nx - 1));
  const iy = Math.round(((y - ymin) / (ymax - ymin)) * (ny - 1));
  const index = iy * nx + ix;
  if (mask?.[index]) return undefined;
  return values[index];
}

function probeMesh(data: Mesh2DData, values: readonly number[], at: Point): number | undefined {
  for (const [i, j, k] of data.triangles) {
    const a = data.points[i];
    const b = data.points[j];
    const c = data.points[k];
    if (!a || !b || !c) continue;
    const weights = barycentric(at, a, b, c);
    if (!weights) continue;
    const [wa, wb, wc] = weights;
    return wa * values[i]! + wb * values[j]! + wc * values[k]!;
  }
  return undefined;
}

/** Barycentric weights, or null if the point lies outside the triangle. */
export function barycentric(
  p: Point,
  a: Point,
  b: Point,
  c: Point,
  epsilon = 1e-12,
): [number, number, number] | null {
  const area2 = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]);
  if (Math.abs(area2) < epsilon) return null; // degenerate triangle
  const wa = ((b[0] - p[0]) * (c[1] - p[1]) - (c[0] - p[0]) * (b[1] - p[1])) / area2;
  const wb = ((c[0] - p[0]) * (a[1] - p[1]) - (a[0] - p[0]) * (c[1] - p[1])) / area2;
  const wc = 1 - wa - wb;
  const tolerance = -epsilon;
  if (wa < tolerance || wb < tolerance || wc < tolerance) return null;
  return [wa, wb, wc];
}

/**
 * Iso-contour segments at `level`, in domain coordinates.
 *
 * Marching triangles for meshes, marching squares (split into triangles) for grids —
 * splitting keeps one implementation and sidesteps the saddle-point ambiguity that
 * plain marching squares has to special-case.
 */
export function contourSegments(result: JobResult, field: string, level: number): Segment[] {
  const values = resultFieldValues(result, field);
  if (!values) return [];
  return result.kind === 'mesh2d'
    ? contourMesh(result.data, values, level)
    : contourGrid(result.data, values, resultMask(result), level);
}

function contourMesh(data: Mesh2DData, values: readonly number[], level: number): Segment[] {
  const segments: Segment[] = [];
  for (const [i, j, k] of data.triangles) {
    const segment = triangleSegment(
      [data.points[i]!, data.points[j]!, data.points[k]!],
      [values[i]!, values[j]!, values[k]!],
      level,
    );
    if (segment) segments.push(segment);
  }
  return segments;
}

function contourGrid(
  data: Grid2DData,
  values: readonly number[],
  mask: readonly number[] | undefined,
  level: number,
): Segment[] {
  const [xmin, ymin, xmax, ymax] = data.bounds;
  const [ny, nx] = data.shape;
  const dx = (xmax - xmin) / (nx - 1 || 1);
  const dy = (ymax - ymin) / (ny - 1 || 1);
  const at = (ix: number, iy: number): Point => [xmin + ix * dx, ymin + iy * dy];
  const segments: Segment[] = [];

  for (let iy = 0; iy < ny - 1; iy += 1) {
    for (let ix = 0; ix < nx - 1; ix += 1) {
      const corners = [iy * nx + ix, iy * nx + ix + 1, (iy + 1) * nx + ix + 1, (iy + 1) * nx + ix];
      if (mask && corners.some((c) => mask[c])) continue;
      const p = [at(ix, iy), at(ix + 1, iy), at(ix + 1, iy + 1), at(ix, iy + 1)] as Point[];
      const v = corners.map((c) => values[c]!) as number[];
      for (const [a, b, c] of [
        [0, 1, 2],
        [0, 2, 3],
      ]) {
        const segment = triangleSegment(
          [p[a]!, p[b]!, p[c]!],
          [v[a]!, v[b]!, v[c]!],
          level,
        );
        if (segment) segments.push(segment);
      }
    }
  }
  return segments;
}

/** The single segment where `level` crosses one triangle, if it crosses at all. */
function triangleSegment(
  points: [Point, Point, Point],
  values: [number, number, number],
  level: number,
): Segment | null {
  const crossings: Point[] = [];
  for (const [i, j] of [
    [0, 1],
    [1, 2],
    [2, 0],
  ] as [number, number][]) {
    const vi = values[i]!;
    const vj = values[j]!;
    if (!Number.isFinite(vi) || !Number.isFinite(vj)) return null;
    if (vi > level === vj > level) continue;
    const t = (level - vi) / (vj - vi || 1e-30);
    const pi = points[i]!;
    const pj = points[j]!;
    crossings.push([pi[0] + t * (pj[0] - pi[0]), pi[1] + t * (pj[1] - pi[1])]);
  }
  return crossings.length === 2 ? [crossings[0]!, crossings[1]!] : null;
}

/** `count` evenly spaced iso levels strictly inside the range. */
export function isoLevels(min: number, max: number, count: number): number[] {
  const levels: number[] = [];
  for (let i = 1; i <= count; i += 1) levels.push(min + ((max - min) * i) / (count + 1));
  return levels;
}
