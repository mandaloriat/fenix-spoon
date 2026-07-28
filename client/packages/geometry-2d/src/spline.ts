/**
 * Closed centripetal Catmull-Rom sampling.
 *
 * Control points are what the engineer drags; the *sampled* polygon is what the solver
 * meshes. Centripetal (alpha = 0.5) rather than uniform parameterisation because uniform
 * Catmull-Rom overshoots into cusps and self-intersections on the uneven point spacing a
 * hand-edited airfoil always has — and a self-intersecting outline is rejected by the
 * protocol, so the overshoot would surface as a validation error the user can't explain.
 */

export type Point = [number, number];

const ALPHA = 0.5;

function knot(t: number, a: Point, b: Point): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  return t + Math.hypot(dx, dy) ** ALPHA;
}

/** Sample a closed Catmull-Rom spline through `points`, `perSegment` samples per span. */
export function sampleClosedSpline(points: Point[], perSegment = 8): Point[] {
  const n = points.length;
  if (n < 3 || perSegment < 1) return points.map((p) => [...p] as Point);

  const out: Point[] = [];
  for (let i = 0; i < n; i += 1) {
    const p0 = points[(i - 1 + n) % n]!;
    const p1 = points[i]!;
    const p2 = points[(i + 1) % n]!;
    const p3 = points[(i + 2) % n]!;

    const t0 = 0;
    const t1 = knot(t0, p0, p1);
    const t2 = knot(t1, p1, p2);
    const t3 = knot(t2, p2, p3);
    // Coincident points make the knot spacing degenerate; fall back to the control point.
    if (!(t1 > t0) || !(t2 > t1) || !(t3 > t2)) {
      out.push([...p1] as Point);
      continue;
    }

    for (let s = 0; s < perSegment; s += 1) {
      const t = t1 + ((t2 - t1) * s) / perSegment;
      const a1 = lerp(p0, p1, (t1 - t) / (t1 - t0), (t - t0) / (t1 - t0));
      const a2 = lerp(p1, p2, (t2 - t) / (t2 - t1), (t - t1) / (t2 - t1));
      const a3 = lerp(p2, p3, (t3 - t) / (t3 - t2), (t - t2) / (t3 - t2));
      const b1 = lerp(a1, a2, (t2 - t) / (t2 - t0), (t - t0) / (t2 - t0));
      const b2 = lerp(a2, a3, (t3 - t) / (t3 - t1), (t - t1) / (t3 - t1));
      out.push(lerp(b1, b2, (t2 - t) / (t2 - t1), (t - t1) / (t2 - t1)));
    }
  }
  return out;
}

function lerp(a: Point, b: Point, wa: number, wb: number): Point {
  return [a[0] * wa + b[0] * wb, a[1] * wa + b[1] * wb];
}

/** SVG path data for a closed outline. */
export function toPathData(points: Point[], toPx: (p: Point) => Point): string {
  if (points.length === 0) return '';
  const px = points.map(toPx);
  const [first, ...rest] = px;
  return `M ${first![0]} ${first![1]} ${rest.map((p) => `L ${p[0]} ${p[1]}`).join(' ')} Z`;
}
