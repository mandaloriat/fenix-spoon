/**
 * Result-kind-agnostic field access, contouring and probing.
 *
 * `grid2d` and `mesh2d` describe the same thing two ways, and a viewer shouldn't care
 * which it got. These helpers normalise both to "values at positions" plus the topology
 * needed to interpolate between them — all without touching a canvas, so they're
 * directly testable.
 *
 * They take a `FieldResult` rather than a `JobResult`: protocol 1.4 added `series1d`, whose
 * answer is curves and which has no bounds, no topology and nothing to contour. That is a
 * *narrowing of the type*, not a runtime check — a curve is a different widget, not a mode of
 * this one, and the compiler is the right place to say so.
 */

import {
  type FieldResult,
  type Grid2DData,
  type Mesh2DData,
  fieldNames,
  fieldValues,
} from '@fenix-spoon/client';

export type Point = [number, number];
export type Segment = [Point, Point];

export function resultBounds(result: FieldResult): [number, number, number, number] {
  return result.data.bounds;
}

// Delegated rather than reimplemented. These were a byte-for-byte copy of the client
// package's pair, and this widget already imports values from it — so the copy bought
// nothing and cost a lockstep edit every time the result kinds change, which protocol 1.4
// duly demanded. Re-exported under the `result*` names the rest of this module uses.
export const resultFieldNames = fieldNames;
export const resultFieldValues = fieldValues;

export function resultMask(result: FieldResult): number[] | undefined {
  return result.kind === 'grid2d' ? result.data.mask : undefined;
}

/**
 * Sample the field at a domain position, or `undefined` outside it / inside a hole.
 *
 * Grids use nearest-node lookup; meshes use barycentric interpolation inside the
 * containing triangle, which is what makes a probe readout on an unstructured mesh
 * agree with what the colours show rather than snapping to the nearest vertex.
 */
export function probe(result: FieldResult, field: string, at: Point): number | undefined {
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
export function contourSegments(result: FieldResult, field: string, level: number): Segment[] {
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

// --------------------------------------------------------------- vector fields

export interface Glyph {
  /** Arrow tail, in domain coordinates. */
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** Magnitude, so a caller can colour or scale by it without recomputing. */
  magnitude: number;
}

export function resultVectorFieldNames(result: FieldResult): string[] {
  const map =
    result.kind === 'grid2d' ? result.data.vector_fields : result.data.point_vector_fields;
  return Object.keys(map ?? {});
}

export function resultVectorValues(
  result: FieldResult,
  field: string,
): [number, number][] | undefined {
  const map =
    result.kind === 'grid2d' ? result.data.vector_fields : result.data.point_vector_fields;
  return map?.[field];
}

/** Domain coordinates of sample `i`, for either result kind. */
function samplePoint(result: FieldResult, index: number): [number, number] {
  if (result.kind !== 'grid2d') return result.data.points[index]!;
  const [ny, nx] = result.data.shape;
  const [xmin, ymin, xmax, ymax] = result.data.bounds;
  const iy = Math.floor(index / nx);
  const ix = index % nx;
  // nx or ny of 1 would divide by zero; a degenerate axis collapses to its minimum.
  return [
    nx > 1 ? xmin + ((xmax - xmin) * ix) / (nx - 1) : xmin,
    ny > 1 ? ymin + ((ymax - ymin) * iy) / (ny - 1) : ymin,
  ];
}

/**
 * Resample a vector field onto a coarse lattice for drawing.
 *
 * **Glyph density must not follow the data's resolution.** One arrow per grid point is
 * unreadable at 512x341 and sparse at 16x16, and the same field would look like a
 * different physical situation at two mesh sizes. So the lattice is chosen from
 * `across` — roughly how many arrows to span the domain's width — and every sample
 * falling in a lattice cell is averaged into one arrow at that cell's centre.
 *
 * Averaging rather than nearest-sample is deliberate: on an unstructured mesh, node
 * density varies, and picking one node per cell would let a locally refined region
 * dominate the direction shown. Masked samples are excluded entirely, so arrows stop at
 * a hole's edge instead of being dragged toward zero by the interior.
 */
export function glyphSamples(result: FieldResult, field: string, across = 24): Glyph[] {
  const vectors = resultVectorValues(result, field);
  if (!vectors || across < 1) return [];
  const [xmin, ymin, xmax, ymax] = resultBounds(result);
  const width = xmax - xmin;
  const height = ymax - ymin;
  if (!(width > 0) || !(height > 0)) return [];

  const cell = width / across;
  const rows = Math.max(1, Math.round(height / cell));
  const mask = resultMask(result);

  // Sum vectors per lattice cell, then divide — one pass, no per-cell searching.
  const sumX = new Float64Array(across * rows);
  const sumY = new Float64Array(across * rows);
  const count = new Int32Array(across * rows);

  for (let i = 0; i < vectors.length; i += 1) {
    if (mask?.[i]) continue;
    const [vx, vy] = vectors[i]!;
    const [x, y] = samplePoint(result, i);
    const cx = Math.min(across - 1, Math.max(0, Math.floor(((x - xmin) / width) * across)));
    const cy = Math.min(rows - 1, Math.max(0, Math.floor(((y - ymin) / height) * rows)));
    const slot = cy * across + cx;
    sumX[slot]! += vx;
    sumY[slot]! += vy;
    count[slot]! += 1;
  }

  const glyphs: Glyph[] = [];
  for (let cy = 0; cy < rows; cy += 1) {
    for (let cx = 0; cx < across; cx += 1) {
      const slot = cy * across + cx;
      const n = count[slot]!;
      if (!n) continue; // no samples here — a hole, or outside an irregular mesh
      const vx = sumX[slot]! / n;
      const vy = sumY[slot]! / n;
      const magnitude = Math.hypot(vx, vy);
      if (!(magnitude > 0)) continue; // a zero vector has no direction to draw
      glyphs.push({
        x: xmin + ((cx + 0.5) / across) * width,
        y: ymin + ((cy + 0.5) / rows) * height,
        vx,
        vy,
        magnitude,
      });
    }
  }
  return glyphs;
}
