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
  type Bounds2D,
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
 *
 * {@link sampleAt} is the interpolating sibling, which is what a section or a curve
 * integration wants. This one stays nearest-node on a grid because that is what a probe
 * readout has always reported here: it names a value the solver actually produced, and a
 * caller comparing the readout against `POST /jobs/{id}/query {"op": "at_point"}` — which
 * interpolates — should be able to tell which question it asked.
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
  const hit = locate(data, at);
  if (!hit) return undefined;
  const [[i, j, k], [wa, wb, wc]] = hit;
  return wa * values[i]! + wb * values[j]! + wc * values[k]!;
}

// ------------------------------------------------------------- mesh point location

/**
 * A uniform bucket grid over triangle bounding boxes.
 *
 * Point location used to be a linear scan, which is fine for the four triangles in a unit
 * test and quadratic-feeling on a real solve: one `pointermove` over a 60k-element mesh
 * tested 60k triangles, and a streamline integrating a few hundred steps did it a few
 * hundred times over. With roughly one triangle per bucket the scan becomes a constant
 * handful of tests, which is what makes probing and curve integration affordable on the
 * meshes the FEniCSx adapters actually return.
 */
interface MeshIndex {
  bounds: Bounds2D;
  nx: number;
  ny: number;
  cellX: number;
  cellY: number;
  buckets: number[][];
}

/** Built once per payload and kept for as long as the payload is; never invalidated. */
const MESH_INDEX = new WeakMap<Mesh2DData, MeshIndex>();

/** Cap on buckets per axis: past this the index costs more memory than the scan saves. */
const MAX_BUCKETS_PER_AXIS = 256;

function meshIndex(data: Mesh2DData): MeshIndex {
  const cached = MESH_INDEX.get(data);
  if (cached) return cached;

  const [xmin, ymin, xmax, ymax] = data.bounds;
  const width = xmax - xmin || 1;
  const height = ymax - ymin || 1;
  const count = Math.max(1, data.triangles.length);
  // Aim at ~1 triangle per bucket, with the bucket count split between the axes in the
  // domain's own proportion so buckets stay roughly square whatever shape the domain is.
  const aspect = width / height;
  const nx = clampInt(Math.round(Math.sqrt(count * aspect)), 1, MAX_BUCKETS_PER_AXIS);
  const ny = clampInt(Math.round(Math.sqrt(count / aspect)), 1, MAX_BUCKETS_PER_AXIS);
  const cellX = width / nx;
  const cellY = height / ny;
  const buckets: number[][] = Array.from({ length: nx * ny }, () => []);

  data.triangles.forEach(([i, j, k], index) => {
    const a = data.points[i];
    const b = data.points[j];
    const c = data.points[k];
    if (!a || !b || !c) return;
    const x0 = bucketIndex(Math.min(a[0], b[0], c[0]), xmin, cellX, nx);
    const x1 = bucketIndex(Math.max(a[0], b[0], c[0]), xmin, cellX, nx);
    const y0 = bucketIndex(Math.min(a[1], b[1], c[1]), ymin, cellY, ny);
    const y1 = bucketIndex(Math.max(a[1], b[1], c[1]), ymin, cellY, ny);
    for (let by = y0; by <= y1; by += 1) {
      for (let bx = x0; bx <= x1; bx += 1) buckets[by * nx + bx]!.push(index);
    }
  });

  const index: MeshIndex = { bounds: data.bounds, nx, ny, cellX, cellY, buckets };
  MESH_INDEX.set(data, index);
  return index;
}

function bucketIndex(value: number, origin: number, cell: number, count: number): number {
  return clampInt(Math.floor((value - origin) / cell), 0, count - 1);
}

function clampInt(value: number, low: number, high: number): number {
  if (!Number.isFinite(value)) return low;
  return Math.min(high, Math.max(low, Math.floor(value)));
}

/**
 * The triangle containing `at` and the barycentric weights there, or null outside the mesh.
 *
 * Exported because both the scalar and the vector sampler want the same answer, and so
 * does anything downstream that needs "is this point in the solved region at all" — the
 * question a streamline asks at every step.
 */
export function locate(
  data: Mesh2DData,
  at: Point,
): [[number, number, number], [number, number, number]] | null {
  const [xmin, ymin, xmax, ymax] = data.bounds;
  if (at[0] < xmin || at[0] > xmax || at[1] < ymin || at[1] > ymax) return null;
  const index = meshIndex(data);
  const bx = bucketIndex(at[0], xmin, index.cellX, index.nx);
  const by = bucketIndex(at[1], ymin, index.cellY, index.ny);
  for (const triangle of index.buckets[by * index.nx + bx] ?? []) {
    const nodes = data.triangles[triangle]!;
    const a = data.points[nodes[0]];
    const b = data.points[nodes[1]];
    const c = data.points[nodes[2]];
    if (!a || !b || !c) continue;
    const weights = barycentric(at, a, b, c);
    if (weights) return [nodes, weights];
  }
  return null;
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

/**
 * Ceiling on glyph lattice columns.
 *
 * A density control that a page can wire to a slider needs an end stop, and this is it:
 * 128 columns across is already more arrows than any 2-D picture can carry legibly, and
 * the lattice allocates three typed arrays proportional to its cell count. An attribute
 * asking for 100000 gets 128 rather than a frozen tab.
 */
export const MAX_GLYPHS_ACROSS = 128;

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

// -------------------------------------------------------- interpolated sampling

/**
 * Interpolate a scalar field at a domain position — bilinear on a grid, barycentric on a
 * mesh — or `undefined` where nothing was solved.
 *
 * This mirrors the server's `at_point` query operation, corner renormalisation included:
 * next to an obstacle some of the four surrounding grid nodes are masked, and averaging
 * their placeholder into the answer would report a number the solver never computed. The
 * two implementations agreeing is what lets an application draw a section in the browser
 * from data it already has, and get the same curve the server would have returned.
 */
export function sampleAt(result: FieldResult, field: string, at: Point): number | undefined {
  const values = resultFieldValues(result, field);
  if (!values) return undefined;
  return result.kind === 'grid2d'
    ? sampleGrid(result.data, values, resultMask(result), at, scalarLerp)
    : sampleMesh(result.data, values, at, scalarLerp);
}

/**
 * Interpolate a *vector* field the same way. `undefined` where the field does not exist,
 * which is the honest answer for a result that carries no vectors at all — see
 * `streamlines.ts` for why that case is a refusal rather than a fallback.
 *
 * `strict` narrows what counts as inside the solved region: normally a point whose stencil
 * is only partly masked is answered from the live part, matching the server's `at_point`;
 * under `strict` it is refused. Curve integration wants the strict rule, because
 * "renormalise around the masked corners" is right for reporting one value near a wall and
 * wrong for deciding whether a curve may take another step into it.
 */
export function sampleVectorAt(
  result: FieldResult,
  field: string,
  at: Point,
  options: { strict?: boolean } = {},
): [number, number] | undefined {
  const vectors = resultVectorValues(result, field);
  if (!vectors) return undefined;
  return result.kind === 'grid2d'
    ? sampleGrid(result.data, vectors, resultMask(result), at, vectorLerp, options.strict)
    : sampleMesh(result.data, vectors, at, vectorLerp);
}

/** How a set of weighted samples combines. One generic pass serves scalars and vectors. */
type Combine<T> = (samples: readonly T[], weights: readonly number[]) => T;

const scalarLerp: Combine<number> = (samples, weights) => {
  let total = 0;
  for (let i = 0; i < samples.length; i += 1) total += samples[i]! * weights[i]!;
  return total;
};

const vectorLerp: Combine<[number, number]> = (samples, weights) => {
  let x = 0;
  let y = 0;
  for (let i = 0; i < samples.length; i += 1) {
    x += samples[i]![0] * weights[i]!;
    y += samples[i]![1] * weights[i]!;
  }
  return [x, y];
};

function sampleGrid<T>(
  data: Grid2DData,
  values: readonly T[],
  mask: readonly number[] | undefined,
  [x, y]: Point,
  combine: Combine<T>,
  strict = false,
): T | undefined {
  const [xmin, ymin, xmax, ymax] = data.bounds;
  if (!(x >= xmin && x <= xmax && y >= ymin && y <= ymax)) return undefined;
  const [ny, nx] = data.shape;
  const dx = nx > 1 ? (xmax - xmin) / (nx - 1) : 1;
  const dy = ny > 1 ? (ymax - ymin) / (ny - 1) : 1;
  const fx = (x - xmin) / dx;
  const fy = (y - ymin) / dy;
  const ix = nx > 1 ? Math.min(Math.max(0, Math.floor(fx)), nx - 2) : 0;
  const iy = ny > 1 ? Math.min(Math.max(0, Math.floor(fy)), ny - 2) : 0;
  const tx = nx > 1 ? fx - ix : 0;
  const ty = ny > 1 ? fy - iy : 0;

  const corners = [
    [iy, ix, (1 - tx) * (1 - ty)],
    [iy, Math.min(ix + 1, nx - 1), tx * (1 - ty)],
    [Math.min(iy + 1, ny - 1), ix, (1 - tx) * ty],
    [Math.min(iy + 1, ny - 1), Math.min(ix + 1, nx - 1), tx * ty],
  ] as [number, number, number][];

  const samples: T[] = [];
  const weights: number[] = [];
  let live = 0;
  for (const [cy, cx, weight] of corners) {
    const index = cy * nx + cx;
    if (mask?.[index]) {
      if (strict) return undefined;
      continue;
    }
    const value = values[index];
    if (value === undefined) continue;
    samples.push(value);
    weights.push(weight);
    live += weight;
  }
  if (!(live > 0)) return undefined;
  return combine(
    samples,
    weights.map((w) => w / live),
  );
}

function sampleMesh<T>(
  data: Mesh2DData,
  values: readonly T[],
  at: Point,
  combine: Combine<T>,
): T | undefined {
  const hit = locate(data, at);
  if (!hit) return undefined;
  const [nodes, weights] = hit;
  const samples: T[] = [];
  for (const node of nodes) {
    const value = values[node];
    if (value === undefined) return undefined;
    samples.push(value);
  }
  return combine(samples, weights);
}

// ------------------------------------------------------------------- sections

/**
 * Ceiling on how many points one section may return.
 *
 * 4096 is the protocol's own per-trace limit for `series1d`, and that is the reason for
 * the number rather than a coincidence: a section is exactly the shape of a curve trace,
 * so an application that samples one here and hands it to a plot — or sends it back as a
 * series — cannot produce something the protocol would refuse.
 */
export const MAX_SECTION_SAMPLES = 4096;

/** Matches the server's `section` default, so the two answer the same question by default. */
export const DEFAULT_SECTION_SAMPLES = 64;

/**
 * A field sampled along a straight line, in the shape the server's `section` query returns.
 *
 * Deliberately the same keys: an application that already reads
 * `POST /jobs/{id}/query {"op": "section"}` can point the same plotting code at this,
 * and the only difference is that this one costs no round trip and no job — it reads the
 * arrays the page already has.
 *
 * `skipped` is not an error count. A section drawn across an airfoil crosses the body,
 * where nothing was solved; dropping those samples and saying how many beats interpolating
 * filler through the hole, and beats failing the whole section because one end left the
 * domain.
 */
export interface Section {
  /** Distance along the line from `start`, one entry per returned sample. */
  distance: number[];
  value: number[];
  at: Point[];
  /** Samples asked for, after the budget cap. */
  requested: number;
  /** Samples that fell outside the solved region and were dropped. */
  skipped: number;
}

export function sampleSection(
  result: FieldResult,
  field: string,
  start: Point,
  end: Point,
  samples: number = DEFAULT_SECTION_SAMPLES,
): Section | undefined {
  if (!resultFieldValues(result, field)) return undefined;
  const count = Math.min(
    MAX_SECTION_SAMPLES,
    Math.max(2, Number.isFinite(samples) ? Math.floor(samples) : DEFAULT_SECTION_SAMPLES),
  );
  const section: Section = { distance: [], value: [], at: [], requested: count, skipped: 0 };
  for (let i = 0; i < count; i += 1) {
    const t = count > 1 ? i / (count - 1) : 0;
    const point: Point = [
      start[0] + t * (end[0] - start[0]),
      start[1] + t * (end[1] - start[1]),
    ];
    const value = sampleAt(result, field, point);
    if (value === undefined) {
      section.skipped += 1;
      continue;
    }
    section.distance.push(Math.hypot(point[0] - start[0], point[1] - start[1]));
    section.value.push(value);
    section.at.push(point);
  }
  return section;
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
export function glyphSamples(
  result: FieldResult,
  field: string,
  across = 24,
  window?: Bounds2D,
): Glyph[] {
  const vectors = resultVectorValues(result, field);
  if (!vectors) return [];
  const columns = clampInt(across, 0, MAX_GLYPHS_ACROSS);
  if (columns < 1) return [];
  const [xmin, ymin, xmax, ymax] = resultBounds(result);
  if (!(xmax > xmin) || !(ymax > ymin)) return [];

  // The visible part of the domain, clipped to it. Cell *size* comes from the window, so
  // on-screen arrow density is constant at any zoom; cell *indices* are counted from the
  // domain origin, so the arrows stay put while panning instead of crawling with the
  // window. Getting only one of those right is what makes a glyph overlay feel unstable.
  const view = window ?? [xmin, ymin, xmax, ymax];
  const viewXmin = Math.max(xmin, Math.min(view[0], view[2]));
  const viewXmax = Math.min(xmax, Math.max(view[0], view[2]));
  const viewYmin = Math.max(ymin, Math.min(view[1], view[3]));
  const viewYmax = Math.min(ymax, Math.max(view[1], view[3]));
  if (!(viewXmax > viewXmin) || !(viewYmax > viewYmin)) return [];

  const cell = (viewXmax - viewXmin) / columns;
  if (!(cell > 0)) return [];
  const c0 = Math.floor((viewXmin - xmin) / cell);
  const r0 = Math.floor((viewYmin - ymin) / cell);
  // `ceil - 1` rather than `floor`, so a view whose far edge lands exactly on a cell
  // boundary does not gain a one-sample-wide column of arrows outside the picture.
  const nx = Math.max(1, Math.ceil((viewXmax - xmin) / cell) - c0);
  const ny = Math.max(1, Math.ceil((viewYmax - ymin) / cell) - r0);
  const mask = resultMask(result);

  // Sum vectors per lattice cell, then divide — one pass, no per-cell searching.
  const sumX = new Float64Array(nx * ny);
  const sumY = new Float64Array(nx * ny);
  const count = new Int32Array(nx * ny);

  const visit = (i: number): void => {
    if (mask?.[i]) return;
    const vector = vectors[i];
    if (!vector) return;
    const [x, y] = samplePoint(result, i);
    if (x < viewXmin || x > viewXmax || y < viewYmin || y > viewYmax) return;
    // Clamped rather than dropped: a sample sitting exactly on the far edge belongs to
    // the last cell, not to a cell one past the end that nothing will ever draw.
    const cx = clampInt(Math.floor((x - xmin) / cell) - c0, 0, nx - 1);
    const cy = clampInt(Math.floor((y - ymin) / cell) - r0, 0, ny - 1);
    const slot = cy * nx + cx;
    sumX[slot]! += vector[0];
    sumY[slot]! += vector[1];
    count[slot]! += 1;
  };

  if (result.kind === 'grid2d') {
    // A grid knows where its samples are, so a zoomed-in view visits only the rows and
    // columns it can see rather than the whole array.
    const [rows, cols] = result.data.shape;
    const [ix0, ix1] = indexRange(viewXmin, viewXmax, xmin, xmax, cols);
    const [iy0, iy1] = indexRange(viewYmin, viewYmax, ymin, ymax, rows);
    for (let iy = iy0; iy <= iy1; iy += 1) {
      for (let ix = ix0; ix <= ix1; ix += 1) visit(iy * cols + ix);
    }
  } else {
    for (let i = 0; i < vectors.length; i += 1) visit(i);
  }

  const glyphs: Glyph[] = [];
  for (let cy = 0; cy < ny; cy += 1) {
    for (let cx = 0; cx < nx; cx += 1) {
      const slot = cy * nx + cx;
      const n = count[slot]!;
      if (!n) continue; // no samples here — a hole, or outside an irregular mesh
      const vx = sumX[slot]! / n;
      const vy = sumY[slot]! / n;
      const magnitude = Math.hypot(vx, vy);
      if (!(magnitude > 0)) continue; // a zero vector has no direction to draw
      glyphs.push({
        x: xmin + (c0 + cx + 0.5) * cell,
        y: ymin + (r0 + cy + 0.5) * cell,
        vx,
        vy,
        magnitude,
      });
    }
  }
  return glyphs;
}

/** Inclusive index range of the grid samples covering `[low, high]` on one axis. */
function indexRange(
  low: number,
  high: number,
  min: number,
  max: number,
  count: number,
): [number, number] {
  if (count <= 1 || !(max > min)) return [0, Math.max(0, count - 1)];
  const step = (max - min) / (count - 1);
  return [
    clampInt(Math.floor((low - min) / step), 0, count - 1),
    clampInt(Math.ceil((high - min) / step), 0, count - 1),
  ];
}
