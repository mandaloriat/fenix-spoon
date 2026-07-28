/**
 * Colormaps and scalar-range handling.
 *
 * Pure functions on purpose: canvas doesn't exist in jsdom, so everything that can be
 * decided without a drawing context lives here and is unit-tested directly. The element
 * keeps only the thin layer that actually paints.
 */

export type RGB = [number, number, number];

/** Perceptually-ordered control points; interpolation between them is linear in sRGB. */
const STOPS: Record<string, RGB[]> = {
  viridis: [
    [68, 1, 84],
    [59, 82, 139],
    [33, 145, 140],
    [94, 201, 98],
    [253, 231, 37],
  ],
  plasma: [
    [13, 8, 135],
    [126, 3, 168],
    [204, 71, 120],
    [248, 149, 64],
    [240, 249, 33],
  ],
  // Diverging: use with a symmetric range so white sits at zero.
  coolwarm: [
    [59, 76, 192],
    [144, 178, 254],
    [220, 220, 220],
    [246, 158, 129],
    [180, 4, 38],
  ],
  greyscale: [
    [0, 0, 0],
    [255, 255, 255],
  ],
};

export type ColormapName = keyof typeof STOPS & string;

export const COLORMAP_NAMES = Object.keys(STOPS) as ColormapName[];

export function isColormapName(name: string): name is ColormapName {
  // `in` would also accept inherited names like "toString", whose value is a function
  // rather than an array of stops — sampling that reads past the end and throws.
  return Object.hasOwn(STOPS, name);
}

/** Sample a colormap at `t` in [0, 1]; out-of-range values clamp to the ends. */
export function sampleColormap(name: string, t: number): RGB {
  const stops = (isColormapName(name) ? STOPS[name] : undefined) ?? STOPS.viridis!;
  if (!Number.isFinite(t)) return stops[0]!;
  const scaled = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const index = Math.min(stops.length - 2, Math.floor(scaled));
  const frac = scaled - index;
  const a = stops[index]!;
  const b = stops[index + 1]!;
  return [
    Math.round(a[0] + frac * (b[0] - a[0])),
    Math.round(a[1] + frac * (b[1] - a[1])),
    Math.round(a[2] + frac * (b[2] - a[2])),
  ];
}

export interface Range {
  min: number;
  max: number;
}

/**
 * Finite range of `values`, ignoring entries masked out (1 = masked) and any NaN/Inf.
 * A constant field yields a degenerate range; callers widen it before dividing.
 */
export function fieldRange(values: readonly number[], mask?: readonly number[]): Range {
  let min = Infinity;
  let max = -Infinity;
  for (let i = 0; i < values.length; i += 1) {
    if (mask?.[i]) continue;
    const value = values[i]!;
    if (!Number.isFinite(value)) continue;
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (min > max) return { min: 0, max: 1 }; // everything masked or non-finite
  return { min, max };
}

/** Widen a degenerate range so normalisation never divides by zero. */
export function padRange({ min, max }: Range): Range {
  if (max > min) return { min, max };
  const pad = Math.abs(min) > 0 ? Math.abs(min) * 0.5 : 0.5;
  return { min: min - pad, max: max + pad };
}

/** Make a range symmetric about zero — what a diverging colormap needs to read right. */
export function symmetricRange({ min, max }: Range): Range {
  const extent = Math.max(Math.abs(min), Math.abs(max)) || 1;
  return { min: -extent, max: extent };
}

export function normalise(value: number, { min, max }: Range): number {
  return (value - min) / (max - min || 1);
}

/**
 * Tick values for a colorbar: `count` evenly spaced stops inclusive of both ends,
 * formatted compactly. Scientific notation once the magnitudes stop being readable.
 */
export function colorbarTicks({ min, max }: Range, count = 5): { value: number; label: string }[] {
  const ticks: { value: number; label: string }[] = [];
  const span = Math.max(Math.abs(min), Math.abs(max));
  const scientific = span !== 0 && (span >= 1e4 || span < 1e-3);
  for (let i = 0; i < count; i += 1) {
    const value = min + ((max - min) * i) / (count - 1);
    ticks.push({
      value,
      label: scientific ? value.toExponential(2) : formatFixed(value, max - min),
    });
  }
  return ticks;
}

function formatFixed(value: number, span: number): string {
  const digits = span >= 100 ? 0 : span >= 10 ? 1 : span >= 1 ? 2 : 3;
  const text = value.toFixed(digits);
  return text === `-${(0).toFixed(digits)}` ? (0).toFixed(digits) : text;
}
