import { describe, expect, it } from 'vitest';

import {
  LOG_FLOOR,
  extentOf,
  formatTick,
  padRange,
  scaleFor,
  ticksFor,
  usableDomain,
} from '../src/scale.js';

describe('scaleFor', () => {
  it('projects the ends of the domain onto the ends of the pixel interval', () => {
    const scale = scaleFor({ min: 0, max: 10 }, 100, 500);
    expect(scale.project(0)).toBe(100);
    expect(scale.project(10)).toBe(500);
    expect(scale.project(5)).toBe(300);
  });

  it('inverts back to the value it projected', () => {
    const scale = scaleFor({ min: -3, max: 7 }, 40, 640);
    for (const value of [-3, -1, 0, 2.5, 7]) {
      expect(scale.invert(scale.project(value))).toBeCloseTo(value, 10);
    }
  });

  it('runs downwards when the pixel interval does, which is how invert-y is expressed', () => {
    // The element flips an axis by swapping `from` and `to` rather than by carrying a flag
    // into every drawing call, so this is the whole implementation of `invert-y`.
    const normal = scaleFor({ min: 0, max: 1 }, 300, 20);
    const inverted = scaleFor({ min: 0, max: 1 }, 20, 300);
    expect(normal.project(0)).toBeGreaterThan(normal.project(1));
    expect(inverted.project(0)).toBeLessThan(inverted.project(1));
  });

  it('puts a constant trace in the middle rather than on the frame', () => {
    // A converged residual and a flat C_p are real results. Projecting them to `from` draws
    // them along the axis line, where they read as missing. The centring comes from
    // `usableDomain` widening symmetrically, not from a special case in `project` — writing
    // this test is what found that the guard there was unreachable.
    const scale = scaleFor({ min: 4, max: 4 }, 300, 20);
    expect(scale.project(4)).toBeCloseTo(160, 6);
  });

  it('maps a log scale by decades', () => {
    const scale = scaleFor({ min: 1, max: 1000 }, 0, 300, 'log');
    expect(scale.project(1)).toBeCloseTo(0);
    expect(scale.project(10)).toBeCloseTo(100);
    expect(scale.project(1000)).toBeCloseTo(300);
  });

  it('clips a non-positive value on a log scale instead of producing -Infinity', () => {
    const scale = scaleFor({ min: 1e-6, max: 1 }, 0, 100, 'log');
    expect(Number.isFinite(scale.project(0))).toBe(true);
    expect(Number.isFinite(scale.project(-5))).toBe(true);
  });
});

describe('usableDomain', () => {
  it('widens a zero-width range relative to its magnitude', () => {
    // An absolute pad would give [999999, 1000001] around 1e6, where every tick label
    // rounds to the same string.
    const wide = usableDomain({ min: 1e6, max: 1e6 }, 'linear');
    expect(wide.max - wide.min).toBeCloseTo(2e5);
  });

  it('widens a zero-width range at the origin by one', () => {
    expect(usableDomain({ min: 0, max: 0 }, 'linear')).toEqual({ min: -1, max: 1 });
  });

  it('lifts a non-positive lower bound on a log scale', () => {
    const domain = usableDomain({ min: 0, max: 100 }, 'log');
    expect(domain.min).toBeGreaterThan(0);
    expect(domain.min).toBeLessThanOrEqual(LOG_FLOOR);
  });

  it('falls back to a unit range on non-finite input', () => {
    expect(usableDomain({ min: NaN, max: 1 }, 'linear')).toEqual({ min: 0, max: 1 });
  });
});

describe('ticksFor', () => {
  it('chooses round numbers rather than an exact count', () => {
    // The whole reason this is not the viewer's `colorbarTicks`: an axis is read by looking
    // at a gridline and asking which round number it is.
    const ticks = ticksFor(scaleFor({ min: 0, max: 1 }, 0, 100), 6).map((t) => t.value);
    expect(ticks).toEqual([0, 0.2, 0.4, 0.6000000000000001, 0.8, 1]);
    for (const tick of ticksFor(scaleFor({ min: 0, max: 1 }, 0, 100), 6)) {
      expect(tick.label).toMatch(/^-?\d+(\.\d+)?$/);
    }
  });

  it('stays inside the domain', () => {
    const scale = scaleFor({ min: -3.7, max: 8.2 }, 0, 100);
    for (const tick of ticksFor(scale)) {
      expect(tick.value).toBeGreaterThanOrEqual(-3.7);
      expect(tick.value).toBeLessThanOrEqual(8.2);
    }
  });

  it('gives decades on a log axis', () => {
    const ticks = ticksFor(scaleFor({ min: 1, max: 10000 }, 0, 100, 'log'));
    expect(ticks.map((t) => t.value)).toEqual([1, 10, 100, 1000, 10000]);
  });

  it('terminates on a degenerate range instead of spinning inside a paint', () => {
    // The loop is bounded on the count rather than on the arithmetic, because a step that
    // underflowed to zero would otherwise never reach its exit condition.
    expect(() => ticksFor(scaleFor({ min: 1e-320, max: 1e-320 }, 0, 100))).not.toThrow();
    expect(ticksFor(scaleFor({ min: 5, max: 5 }, 0, 100)).length).toBeLessThan(30);
  });
});

describe('formatTick', () => {
  it('takes its precision from the step, not from the value', () => {
    // Otherwise an axis prints 0.30000000000000004 next to 0.2.
    expect(formatTick(0.30000000000000004, 0.1)).toBe('0.3');
    expect(formatTick(2, 1)).toBe('2');
    expect(formatTick(0.125, 0.125)).toBe('0.125');
  });

  it('never prints negative zero', () => {
    expect(formatTick(-0, 0.1)).toBe('0.0');
  });

  it('goes exponential well away from unity', () => {
    expect(formatTick(1.2e8, 1e7)).toMatch(/e8$/);
    expect(formatTick(2.5e-6, 1e-6)).toContain('e-6');
  });
});

describe('extentOf and padRange', () => {
  it('ignores non-finite values', () => {
    expect(extentOf([1, NaN, 5, Infinity, 3])).toEqual({ min: 1, max: 5 });
  });

  it('returns null when nothing is finite', () => {
    expect(extentOf([NaN, Infinity])).toBeNull();
    expect(extentOf([])).toBeNull();
  });

  it('grows a range by a fraction of its span', () => {
    expect(padRange({ min: 0, max: 10 }, 0.1)).toEqual({ min: -1, max: 11 });
  });
});
