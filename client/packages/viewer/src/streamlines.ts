/**
 * Integral curves of a stationary 2-D vector field.
 *
 * **The name is `integralCurves`, and the attribute is `streamlines`, on purpose.** What
 * this module computes is a mathematical object: the trajectories tangent everywhere to a
 * vector field that does not change while you look at it. Whether such a curve is a *flow*
 * line depends entirely on what the vector means — it is one when the field is a velocity,
 * it is a field line when the field is `B`, and it is neither when the field is a
 * temperature gradient. Fenix Spoon has no way to know which, and inventing one would put
 * discipline-specific physics in a rendering package. The consumer application knows what
 * it asked the solver for, and it is the one that gets to call these flow lines.
 *
 * **Nothing here reconstructs a vector field.** If a result carries only scalars, curves
 * are unavailable and {@link streamlineAvailability} says why in a sentence a page can put
 * on screen. Deriving a "velocity" from a streamfunction, or a direction from a gradient of
 * whatever scalar happens to be selected, would produce a picture that looks like physics
 * and is not — and the protocol already distinguishes the two cases, precisely so that a
 * viewer does not have to guess (see the wire protocol's *Vector fields* section).
 *
 * The integration is RK4 on the **normalised** direction field. Normalising is what makes a
 * step a distance rather than a duration: the geometry of the curve is identical either
 * way, and a fixed distance step keeps the sampling even through a stagnation region
 * instead of stalling there and burning the step budget.
 */

import type { Bounds2D, FieldResult } from '@fenix-spoon/client';

import {
  type Point,
  resultBounds,
  resultFieldNames,
  resultVectorFieldNames,
  sampleVectorAt,
} from './field.js';

/** Seed lattice columns across the seeding window. An end stop for a density slider. */
export const MAX_STREAMLINE_DENSITY = 64;

/** Steps one curve may take in one direction before it is cut off. */
export const MAX_STREAMLINE_STEPS = 4000;

/**
 * Total points across every curve of one call.
 *
 * The three limits are not the same guard wearing three hats: density bounds how many
 * curves are attempted, steps bound how long one may run, and this bounds their product —
 * which is the only one of the three that a caller cannot compute in advance, because how
 * far a curve runs before it leaves the domain depends on the field.
 */
export const MAX_STREAMLINE_POINTS = 250_000;

export interface IntegralCurve {
  /** Where integration started. Always the point a caller seeded, not a resampled one. */
  seed: Point;
  /** Ordered polyline in **domain** coordinates, backward tail first. */
  points: Point[];
  /** Arc length, in domain units. */
  length: number;
  /** True when the curve came back to its own seed — a closed orbit. */
  closed: boolean;
}

export interface StreamlineOptions {
  /** Seed lattice columns across the seeding window. Ignored when `seeds` is given. */
  density?: number;
  /** Explicit seed points, in domain coordinates. Overrides the lattice. */
  seeds?: readonly Point[];
  /** Region to seed in, defaulting to the whole domain. */
  window?: Bounds2D;
  /** Integrate backwards as well as forwards. Default true. */
  bothWays?: boolean;
  /** Steps per direction. Capped at {@link MAX_STREAMLINE_STEPS}. */
  maxSteps?: number;
  /** Step length as a multiple of the data's own sample spacing. Default 1. */
  stepFraction?: number;
  /**
   * How close two curves may come, in domain units. Defaults to half the seed spacing,
   * which is the classic evenly-spaced-streamlines choice and the reason the picture
   * reads as a field rather than as a bundle of lines that all found the same attractor.
   */
  minSeparation?: number;
}

export interface StreamlineAvailability {
  available: boolean;
  /** Vector fields this result carries — what a picker should offer. */
  fields: string[];
  /** Why not, in a sentence meant to be shown to a person. Absent when available. */
  reason?: string;
}

/**
 * Whether integral curves can be drawn, and if not, why.
 *
 * The refusal is the feature. A viewer that silently draws nothing leaves a page unable to
 * tell "this solver does not report a vector" from "the overlay is broken", and a viewer
 * that draws *something* from a scalar field would be worse than either.
 */
export function streamlineAvailability(
  result: FieldResult | null,
  field?: string | null,
): StreamlineAvailability {
  if (!result) return { available: false, fields: [], reason: 'No result is loaded.' };
  const fields = resultVectorFieldNames(result);
  if (fields.length === 0) {
    return {
      available: false,
      fields,
      reason:
        'This result carries no vector field, so there is nothing to integrate. Integral ' +
        'curves cannot be reconstructed from a scalar field — a solver has to send the ' +
        'components for them to exist.',
    };
  }
  if (field == null || field === '') {
    return { available: true, fields };
  }
  if (fields.includes(field)) return { available: true, fields };
  const scalars = resultFieldNames(result);
  return {
    available: false,
    fields,
    reason: scalars.includes(field)
      ? `"${field}" is a scalar field; integrating it would draw curves that look like ` +
        `physics and are not. Vector fields in this result: ${fields.join(', ')}.`
      : `"${field}" is not a vector field of this result. Available: ${fields.join(', ')}.`,
  };
}

/**
 * The data's own sample spacing — the length scale an integration step should be measured
 * against, so the step is fine relative to what the solver resolved rather than to how big
 * the domain happens to be.
 */
function sampleSpacing(result: FieldResult): number {
  const [xmin, ymin, xmax, ymax] = resultBounds(result);
  const width = Math.max(xmax - xmin, Number.EPSILON);
  const height = Math.max(ymax - ymin, Number.EPSILON);
  if (result.kind === 'grid2d') {
    const [ny, nx] = result.data.shape;
    return Math.min(nx > 1 ? width / (nx - 1) : width, ny > 1 ? height / (ny - 1) : height);
  }
  const triangles = result.data.triangles.length;
  // Root of the mean element area: the equivalent edge length of a uniform triangulation
  // covering the same box, which is the right order even for a graded mesh.
  return triangles > 0 ? Math.sqrt((width * height) / triangles) : Math.min(width, height);
}

/** Evenly spaced seed points at the centres of a lattice over `window`. */
export function seedLattice(window: Bounds2D, density: number): Point[] {
  const columns = Math.min(MAX_STREAMLINE_DENSITY, Math.max(1, Math.floor(density) || 0));
  const width = window[2] - window[0];
  const height = window[3] - window[1];
  if (!(width > 0) || !(height > 0)) return [];
  const cell = width / columns;
  const rows = Math.max(1, Math.round(height / cell));
  const seeds: Point[] = [];
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      seeds.push([
        window[0] + (column + 0.5) * cell,
        window[1] + ((row + 0.5) / rows) * height,
      ]);
    }
  }
  return seeds;
}

/**
 * Integrate `field` into a set of curves.
 *
 * Returns an empty array — never a fabricated one — when the field is not a vector field
 * of this result. Ask {@link streamlineAvailability} first if you want to say why.
 */
export function integralCurves(
  result: FieldResult,
  field: string,
  options: StreamlineOptions = {},
): IntegralCurve[] {
  if (!streamlineAvailability(result, field).available) return [];

  const domain = resultBounds(result);
  const window = clipWindow(options.window ?? domain, domain);
  if (!window) return [];

  const density = Math.min(
    MAX_STREAMLINE_DENSITY,
    Math.max(1, Math.floor(options.density ?? 18) || 1),
  );
  const seeds = options.seeds?.length ? options.seeds : seedLattice(window, density);
  if (!seeds.length) return [];

  const spacing = (window[2] - window[0]) / density;
  const separation = Math.max(
    Number.EPSILON,
    options.minSeparation ?? spacing * 0.5,
  );
  const step = Math.max(
    Number.EPSILON,
    sampleSpacing(result) * Math.max(0.05, options.stepFraction ?? 1),
  );
  const maxSteps = Math.min(
    MAX_STREAMLINE_STEPS,
    Math.max(2, Math.floor(options.maxSteps ?? 1500) || 2),
  );
  const bothWays = options.bothWays ?? true;

  const direction = (p: Point): Point | null => {
    // Strict: a curve may only step where the whole interpolation stencil was solved.
    // The lenient rule would let a line creep one cell into an obstacle before stopping,
    // which reads as a streamline entering a solid body.
    const v = sampleVectorAt(result, field, p, { strict: true });
    if (!v) return null;
    const magnitude = Math.hypot(v[0], v[1]);
    // A stagnation point has no direction. Continuing through one on the last known
    // heading would draw a line the field does not contain.
    if (!(magnitude > 0)) return null;
    return [v[0] / magnitude, v[1] / magnitude];
  };

  const occupancy = new Occupancy(domain, separation);
  const curves: IntegralCurve[] = [];
  let budget = MAX_STREAMLINE_POINTS;

  for (const seed of seeds) {
    if (budget <= 0) break;
    if (!inside(seed, window)) continue;
    if (occupancy.crowded(seed)) continue;
    if (!direction(seed)) continue;

    const forward = march(seed, step, maxSteps, direction, domain, occupancy);
    const backward = bothWays
      ? march(seed, -step, maxSteps, direction, domain, occupancy)
      : { points: [] as Point[], closed: false };

    const points = [...backward.points.reverse(), seed, ...forward.points];
    if (points.length < 2) continue;
    const closed = forward.closed || backward.closed;

    let length = 0;
    for (let i = 1; i < points.length; i += 1) {
      length += Math.hypot(points[i]![0] - points[i - 1]![0], points[i]![1] - points[i - 1]![1]);
    }
    for (const point of points) occupancy.add(point);
    budget -= points.length;
    curves.push({ seed: [seed[0], seed[1]], points, length, closed });
  }
  return curves;
}

/** One direction of integration, stopping at the first thing that ends a curve. */
function march(
  seed: Point,
  step: number,
  maxSteps: number,
  direction: (p: Point) => Point | null,
  domain: Bounds2D,
  occupancy: Occupancy,
): { points: Point[]; closed: boolean } {
  const points: Point[] = [];
  let position: Point = [seed[0], seed[1]];
  const half = step / 2;

  for (let i = 0; i < maxSteps; i += 1) {
    // Classic RK4 on the unit direction field. Any stage falling outside the solved
    // region ends the curve where it still had data, rather than extrapolating into a
    // hole and drawing a line through the middle of an obstacle.
    const k1 = direction(position);
    if (!k1) break;
    const k2 = direction([position[0] + half * k1[0], position[1] + half * k1[1]]);
    if (!k2) break;
    const k3 = direction([position[0] + half * k2[0], position[1] + half * k2[1]]);
    if (!k3) break;
    const k4 = direction([position[0] + step * k3[0], position[1] + step * k3[1]]);
    if (!k4) break;

    const next: Point = [
      position[0] + (step / 6) * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]),
      position[1] + (step / 6) * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]),
    ];
    if (!Number.isFinite(next[0]) || !Number.isFinite(next[1])) break;
    if (!inside(next, domain)) break;
    // The point must be *in* the solved region, not merely inside the bounding box: a
    // `grid2d` obstacle is a hole in the middle of its own bounds.
    if (!direction(next)) break;
    // Running into a curve that is already there is what keeps the picture evenly spaced
    // instead of letting every seed converge onto the same attractor.
    if (occupancy.crowded(next)) break;

    points.push(next);
    if (i > 3 && Math.hypot(next[0] - seed[0], next[1] - seed[1]) < Math.abs(step) / 2) {
      return { points, closed: true };
    }
    position = next;
  }
  return { points, closed: false };
}

function inside([x, y]: Point, box: Bounds2D): boolean {
  return x >= box[0] && x <= box[2] && y >= box[1] && y <= box[3];
}

function clipWindow(window: Bounds2D, domain: Bounds2D): Bounds2D | null {
  const clipped: Bounds2D = [
    Math.max(domain[0], Math.min(window[0], window[2])),
    Math.max(domain[1], Math.min(window[1], window[3])),
    Math.min(domain[2], Math.max(window[0], window[2])),
    Math.min(domain[3], Math.max(window[1], window[3])),
  ];
  return clipped[2] > clipped[0] && clipped[3] > clipped[1] ? clipped : null;
}

/**
 * A hash grid of already-drawn curve points, at the separation distance.
 *
 * Bucketed at exactly `separation`, so "is anything within separation of this point" is
 * nine bucket lookups rather than a scan of every point drawn so far. Without it the
 * even-spacing test is quadratic in the number of points, which for a dense field is the
 * dominant cost of the whole overlay.
 */
class Occupancy {
  readonly #cell: number;
  readonly #origin: Point;
  readonly #buckets = new Map<number, Point[]>();
  readonly #columns: number;

  constructor(domain: Bounds2D, separation: number) {
    this.#cell = separation;
    this.#origin = [domain[0], domain[1]];
    this.#columns = Math.max(1, Math.ceil((domain[2] - domain[0]) / separation) + 2);
  }

  #key(x: number, y: number): number {
    const cx = Math.floor((x - this.#origin[0]) / this.#cell);
    const cy = Math.floor((y - this.#origin[1]) / this.#cell);
    return cy * this.#columns + cx;
  }

  add(point: Point): void {
    const key = this.#key(point[0], point[1]);
    const bucket = this.#buckets.get(key);
    if (bucket) bucket.push(point);
    else this.#buckets.set(key, [point]);
  }

  crowded(point: Point): boolean {
    const cx = Math.floor((point[0] - this.#origin[0]) / this.#cell);
    const cy = Math.floor((point[1] - this.#origin[1]) / this.#cell);
    const limit = this.#cell * this.#cell;
    for (let dy = -1; dy <= 1; dy += 1) {
      for (let dx = -1; dx <= 1; dx += 1) {
        const bucket = this.#buckets.get((cy + dy) * this.#columns + (cx + dx));
        if (!bucket) continue;
        for (const other of bucket) {
          const ex = other[0] - point[0];
          const ey = other[1] - point[1];
          if (ex * ex + ey * ey < limit) return true;
        }
      }
    }
    return false;
  }
}
