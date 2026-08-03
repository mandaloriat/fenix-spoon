/**
 * Turning a `Series1DData` into something drawable, and answering questions about it.
 *
 * The protocol lets a curve set share one abscissa *and* lets any trace bring its own — a
 * choice `series.py` made for the airfoil, whose upper and lower surface are not sampled alike
 * unless somebody resampled them. Everything downstream wants that difference already
 * resolved, so this is where it happens, once.
 *
 * No drawing context here either. The one thing a plot gets wrong in a way users notice is
 * *which point did I just hover*, and that is arithmetic.
 */

import {
  type Series1DData,
  type SeriesAxis,
  type SeriesTrace,
  traceAbscissa,
} from '@fenix-spoon/client';

import { type Range, extentOf } from './scale.js';

/** One trace with its abscissa resolved and its points paired up. */
export interface ResolvedTrace {
  name: string;
  unit: string;
  /** Index into the series' `traces`, so a caller can map back to what it was given. */
  index: number;
  points: { x: number; y: number }[];
}

/**
 * Pair each trace's values with its abscissa.
 *
 * A trace whose abscissa is missing or the wrong length is **dropped rather than drawn short**.
 * The server refuses both at validation, so reaching here means the payload did not come from
 * one — and half a curve is worse than no curve, because nothing about it looks wrong.
 */
export function resolveTraces(series: Series1DData): ResolvedTrace[] {
  const resolved: ResolvedTrace[] = [];
  series.traces.forEach((trace: SeriesTrace, index: number) => {
    const axis: SeriesAxis | undefined = traceAbscissa(series, trace);
    if (!axis || axis.values.length !== trace.values.length) return;
    const points = trace.values.map((y, i) => ({ x: axis.values[i]!, y }));
    resolved.push({ name: trace.name, unit: trace.unit, index, points });
  });
  return resolved;
}

/** The x range across every resolved trace, or null when there is nothing to draw. */
export function extentX(traces: readonly ResolvedTrace[]): Range | null {
  return extentOf(traces.flatMap((trace) => trace.points.map((point) => point.x)));
}

/** The y range across every resolved trace, or null when there is nothing to draw. */
export function extentY(traces: readonly ResolvedTrace[]): Range | null {
  return extentOf(traces.flatMap((trace) => trace.points.map((point) => point.y)));
}

/**
 * The unit every trace agrees on, or null when they do not.
 *
 * Null is the interesting answer and the reason this returns one. `series.py` names the case
 * outright — a harmonic result is a magnitude and a phase, "two curves with different units on
 * different axes" — and this element draws **one** y axis. So when the traces disagree the
 * axis goes unlabelled and each unit appears in the legend beside its own curve, which is
 * honest; labelling the axis with the first trace's unit would be a caption that is wrong for
 * every other curve on it. Two axes means two `<fs-plot>` elements, which is the same answer
 * the viewer gives for two fields.
 */
export function sharedUnit(traces: readonly ResolvedTrace[]): string | null {
  if (traces.length === 0) return null;
  const first = traces[0]!.unit;
  return traces.every((trace) => trace.unit === first) ? first : null;
}

/** `name [unit]`, or just the name when the unit is absent or dimensionless. */
export function axisLabel(name: string, unit: string | null | undefined): string {
  return !unit || unit === '1' ? name : `${name} [${unit}]`;
}

export interface NearestPoint {
  trace: ResolvedTrace;
  /** Index of the point within that trace. */
  at: number;
  x: number;
  y: number;
  /** Distance in **pixels** from the query, which is what decided it. */
  distance: number;
}

/**
 * The drawn point closest to a pixel position.
 *
 * **In screen space, not data space**, and the difference is not pedantic: the two axes carry
 * different quantities on different scales, so a data-space distance would add metres to
 * pascals and hand back whichever point happened to sit on the axis with the smaller numbers.
 * The user is pointing at pixels; the answer has to be computed in pixels.
 *
 * Linear over every point. The protocol caps a curve set at 4096 points per trace and 8192
 * across a result, so the worst case is a few thousand comparisons per pointer move — well
 * inside a frame, and not worth an index that could disagree with what was drawn.
 */
export function nearestPoint(
  traces: readonly ResolvedTrace[],
  at: { x: number; y: number },
  project: (point: { x: number; y: number }) => { x: number; y: number },
  within = Infinity,
): NearestPoint | null {
  let best: NearestPoint | null = null;
  for (const trace of traces) {
    for (let i = 0; i < trace.points.length; i += 1) {
      const point = trace.points[i]!;
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
      const screen = project(point);
      const distance = Math.hypot(screen.x - at.x, screen.y - at.y);
      if (distance > within) continue;
      if (best === null || distance < best.distance) {
        best = { trace, at: i, x: point.x, y: point.y, distance };
      }
    }
  }
  return best;
}
