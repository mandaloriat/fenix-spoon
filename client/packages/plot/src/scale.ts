/**
 * Mapping a data range onto pixels, and choosing the numbers to print along it.
 *
 * Pure functions, no drawing context — the same discipline `@fenix-spoon/viewer` applies to
 * its colormaps and viewport arithmetic, and for the same reason: the interesting mistakes
 * here are arithmetic, and arithmetic is worth testing without a canvas.
 *
 * ## Why not reuse the viewer's `colorbarTicks`
 *
 * It divides a range into `n` equal parts and prints whatever falls there — right for a
 * colourbar, where the strip is continuous and the labels annotate it. An axis is read the
 * other way round: somebody looks at a gridline and wants to know *which round number* it is.
 * `0.2, 0.4, 0.6` is an axis; `0.173, 0.346, 0.519` is a colourbar's labelling applied to one.
 */

/** A closed interval of data values. `min === max` is legal and handled by `scaleFor`. */
export interface Range {
  min: number;
  max: number;
}

export type ScaleKind = 'linear' | 'log';

/** A data range projected onto a pixel interval, in one direction. */
export interface Scale {
  kind: ScaleKind;
  domain: Range;
  /** Pixel at `domain.min`. For a y axis this is the *bottom*, so it exceeds `to`. */
  from: number;
  /** Pixel at `domain.max`. */
  to: number;
  /** Data value to pixel. */
  project(value: number): number;
  /** Pixel back to data value — what a hover readout needs. */
  invert(pixel: number): number;
}

/** Smallest positive value a log scale will accept, so `log(0)` cannot reach the arithmetic. */
export const LOG_FLOOR = 1e-12;

export function scaleFor(
  domain: Range,
  from: number,
  to: number,
  kind: ScaleKind = 'linear',
): Scale {
  const safe = usableDomain(domain, kind);
  const forward = kind === 'log' ? Math.log10 : (v: number) => v;
  const back = kind === 'log' ? (v: number) => 10 ** v : (v: number) => v;
  const lo = forward(safe.min);
  const hi = forward(safe.max);
  const span = hi - lo;

  return {
    kind,
    domain: safe,
    from,
    to,
    project(value: number): number {
      // No guard for `span === 0` here, and that is worth stating because the obvious code
      // has one. `usableDomain` already returns a strictly widened range for every input,
      // so a zero span cannot reach this line — and a second defence against it would be an
      // unreachable branch that quietly disagreed with the first. A constant trace lands in
      // the middle of the box because the widening is symmetric, not because of a special
      // case here.
      return from + ((forward(clampForScale(value, kind)) - lo) / span) * (to - from);
    },
    invert(pixel: number): number {
      if (to === from) return safe.min;
      return back(lo + ((pixel - from) / (to - from)) * span);
    },
  };
}

function clampForScale(value: number, kind: ScaleKind): number {
  return kind === 'log' ? Math.max(value, LOG_FLOOR) : value;
}

/**
 * The domain a scale can actually use.
 *
 * Two repairs, both of which would otherwise produce a blank plot rather than an error:
 * a zero-width range is widened around its value, and a log scale's non-positive bound is
 * lifted to the smallest positive thing it can represent. The second is a *clip*, and the
 * element says so in its refusal text rather than pretending the data started there.
 */
export function usableDomain(domain: Range, kind: ScaleKind): Range {
  let { min, max } = domain;
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 1 };
  if (kind === 'log') {
    max = Math.max(max, LOG_FLOOR);
    min = min > 0 ? min : Math.min(max, LOG_FLOOR);
  }
  if (min === max) {
    // Widen by a tenth of the magnitude, or by 1 at the origin. A relative pad keeps a
    // constant trace at 1e6 from being drawn against a range of [1e6 - 1, 1e6 + 1], where
    // floating point leaves the tick labels identical.
    const pad = min === 0 ? 1 : Math.abs(min) * 0.1;
    return kind === 'log' ? { min: min / 10, max: max * 10 } : { min: min - pad, max: max + pad };
  }
  return { min, max };
}

/** Union of ranges, or null when there is nothing to union. */
export function extentOf(values: Iterable<number>): Range | null {
  let min = Infinity;
  let max = -Infinity;
  for (const value of values) {
    if (!Number.isFinite(value)) continue;
    if (value < min) min = value;
    if (value > max) max = value;
  }
  return min <= max ? { min, max } : null;
}

/** Grow a range by a fraction of its span, so a curve does not touch the frame. */
export function padRange({ min, max }: Range, fraction = 0.05): Range {
  const pad = (max - min) * fraction;
  return { min: min - pad, max: max + pad };
}

/**
 * Padding that means the same thing on either scale.
 *
 * On a linear axis a fraction of the span is added at each end. On a log axis that is the
 * wrong operation and quietly ruins the plot: padding a residual history running from 1e-1
 * down to 1e-7 subtracts 0.005 from the lower bound, which is *negative*, so
 * :func:`usableDomain` lifts it to :data:`LOG_FLOOR` and six decades become twelve with the
 * whole curve crushed into the top half. Nothing errors and the axis labels look plausible.
 *
 * So a log axis is padded by a fraction of its **decades**, multiplicatively, which keeps a
 * positive range positive by construction. Having one function decide is the point — the
 * first version special-cased the x axis at the call site and left y additive, which is
 * exactly the asymmetry that produced the bug.
 */
export function padDomain(domain: Range, kind: ScaleKind, fraction = 0.05): Range {
  if (kind !== 'log') return padRange(domain, fraction);
  const safe = usableDomain(domain, 'log');
  const lo = Math.log10(safe.min);
  const hi = Math.log10(safe.max);
  const pad = (hi - lo) * fraction;
  return { min: 10 ** (lo - pad), max: 10 ** (hi + pad) };
}

export interface Tick {
  value: number;
  label: string;
}

/**
 * Round numbers inside a range, at a spacing chosen from {1, 2, 5} × 10^n.
 *
 * `count` is a *target*, not a promise: the whole point is that the spacing is round, and
 * insisting on an exact number of ticks is what forces the spacing to be arbitrary. Asking
 * for 6 across [0, 1] gives five at 0.2; across [0, 1.1] it gives six at 0.2.
 */
export function ticksFor(scale: Scale, count = 6): Tick[] {
  return scale.kind === 'log' ? logTicks(scale.domain) : linearTicks(scale.domain, count);
}

function linearTicks({ min, max }: Range, count: number): Tick[] {
  const step = niceStep((max - min) / Math.max(count, 2));
  const first = Math.ceil(min / step) * step;
  const ticks: Tick[] = [];
  // Bound the loop on the count rather than on the arithmetic: a step that underflows to
  // zero on a pathological range would otherwise spin forever inside a paint.
  for (let i = 0; i < count * 4; i += 1) {
    const value = first + i * step;
    if (value > max + step * 1e-9) break;
    // -0 prints as "-0", which is a distracting way to write zero on an axis.
    ticks.push({ value: value === 0 ? 0 : value, label: formatTick(value, step) });
  }
  return ticks;
}

function logTicks({ min, max }: Range): Tick[] {
  const ticks: Tick[] = [];
  const first = Math.floor(Math.log10(min));
  const last = Math.ceil(Math.log10(max));
  for (let power = first; power <= last && ticks.length < 24; power += 1) {
    const value = 10 ** power;
    if (value < min || value > max) continue;
    ticks.push({ value, label: formatExponent(power) });
  }
  return ticks;
}

function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(raw));
  const scaled = raw / power;
  const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return nice * power;
}

/**
 * A tick label with as many decimals as the *step* needs — not as many as the value has.
 *
 * Deriving the precision from the step is what keeps an axis from printing `0.30000000000004`
 * beside `0.2`: the numbers on an axis are a sequence, and they are read against each other.
 */
export function formatTick(value: number, step: number): string {
  const magnitude = Math.max(Math.abs(value), Math.abs(step));
  if (magnitude !== 0 && (magnitude >= 1e5 || magnitude < 1e-3)) {
    return value.toExponential(1).replace('e+', 'e');
  }
  const decimals = decimalsOf(step);
  const text = value.toFixed(decimals);
  return text === `-${(0).toFixed(decimals)}` ? (0).toFixed(decimals) : text;
}

/**
 * How many decimal places writing `step` exactly requires.
 *
 * Read off the number's own shortest representation rather than computed from its logarithm.
 * `-floor(log10(step))` is the tempting one-liner and it is right for every step
 * :func:`niceStep` produces — 1, 2 and 5 times a power of ten — which is exactly why it is
 * the wrong thing to rely on: it gives 1 for a step of 0.125 and prints an axis reading
 * `0.1, 0.1, 0.4`. This function is exported, so it has to be correct for a step nobody in
 * this module generates.
 */
function decimalsOf(step: number): number {
  const magnitude = Math.abs(step);
  if (!Number.isFinite(magnitude) || magnitude === 0) return 0;
  const [mantissa, exponent] = magnitude.toExponential().split('e');
  const digits = (mantissa ?? '').replace('-', '').replace('.', '').replace(/0+$/, '').length;
  // Digits after the point = significant digits - 1 - exponent, floored at zero.
  return Math.max(0, Math.min(8, digits - 1 - Number(exponent ?? 0)));
}

function formatExponent(power: number): string {
  if (power >= -3 && power <= 4) return String(10 ** power);
  return `1e${power}`;
}
