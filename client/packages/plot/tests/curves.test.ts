import type { Series1DData } from '@fenix-spoon/client';
import { describe, expect, it } from 'vitest';

import {
  axisLabel,
  extentX,
  extentY,
  nearestPoint,
  resolveTraces,
  sharedUnit,
} from '../src/curves.js';

/** The shape `aerodynamics.py` emits: a shared abscissa and two curves over it. */
const SHARED: Series1DData = {
  name: 'surface_cp',
  description: 'Pressure coefficient along the surface',
  x: { name: 'x/c', unit: '1', values: [0, 0.5, 1] },
  traces: [
    { name: 'cp_upper', unit: '1', values: [-1.2, -0.6, 0.1] },
    { name: 'cp_lower', unit: '1', values: [0.4, 0.2, 0.1] },
  ],
};

/** The case the protocol added a per-trace abscissa for: surfaces nobody resampled. */
const PER_TRACE: Series1DData = {
  name: 'surface_cp',
  traces: [
    {
      name: 'cp_upper',
      unit: '1',
      values: [-1.2, -0.6],
      x: { name: 'x/c', unit: '1', values: [0, 1] },
    },
    {
      name: 'cp_lower',
      unit: '1',
      values: [0.4, 0.3, 0.2],
      x: { name: 'x/c', unit: '1', values: [0, 0.5, 1] },
    },
  ],
};

describe('resolveTraces', () => {
  it('pairs values with the shared abscissa', () => {
    const traces = resolveTraces(SHARED);
    expect(traces).toHaveLength(2);
    expect(traces[0]!.points).toEqual([
      { x: 0, y: -1.2 },
      { x: 0.5, y: -0.6 },
      { x: 1, y: 0.1 },
    ]);
  });

  it("honours a trace's own abscissa over the shared one", () => {
    // The whole reason the protocol has both. Two surfaces sampled differently must not be
    // forced onto one x, and a plot that used the shared abscissa would silently stretch one.
    const traces = resolveTraces(PER_TRACE);
    expect(traces[0]!.points.map((p) => p.x)).toEqual([0, 1]);
    expect(traces[1]!.points.map((p) => p.x)).toEqual([0, 0.5, 1]);
  });

  it('drops a trace whose abscissa does not line up instead of drawing it short', () => {
    // The server refuses this at validation, so a payload reaching here with it did not come
    // from one. Half a curve is worse than none, because nothing about it looks wrong.
    const broken: Series1DData = {
      name: 'bad',
      x: { name: 'x', unit: '1', values: [0, 1, 2] },
      traces: [{ name: 'short', unit: '1', values: [1, 2] }],
    };
    expect(resolveTraces(broken)).toHaveLength(0);
  });

  it('drops a trace with no abscissa at all', () => {
    const bare: Series1DData = { name: 'bad', traces: [{ name: 'x', unit: '1', values: [1] }] };
    expect(resolveTraces(bare)).toHaveLength(0);
  });

  it('keeps the original index so a caller can map back', () => {
    const mixed: Series1DData = {
      name: 'mixed',
      x: { name: 'x', unit: '1', values: [0, 1] },
      traces: [
        { name: 'short', unit: '1', values: [1] },
        { name: 'good', unit: '1', values: [1, 2] },
      ],
    };
    const traces = resolveTraces(mixed);
    expect(traces).toHaveLength(1);
    expect(traces[0]!.index).toBe(1);
  });
});

describe('extents', () => {
  it('spans every trace, not just the first', () => {
    const traces = resolveTraces(SHARED);
    expect(extentX(traces)).toEqual({ min: 0, max: 1 });
    expect(extentY(traces)).toEqual({ min: -1.2, max: 0.4 });
  });

  it('is null when there is nothing drawable', () => {
    expect(extentX([])).toBeNull();
    expect(extentY([])).toBeNull();
  });
});

describe('sharedUnit', () => {
  it('reports the unit when every trace agrees', () => {
    expect(sharedUnit(resolveTraces(SHARED))).toBe('1');
  });

  it('reports null when they disagree, which is the case that matters', () => {
    // `series.py` names it: a harmonic result is a magnitude and a phase, which want two
    // axes. One axis cannot be captioned for both, so the element leaves it blank and the
    // legend carries the units instead.
    const harmonic: Series1DData = {
      name: 'response',
      x: { name: 'f', unit: 'Hz', values: [1, 2] },
      traces: [
        { name: 'magnitude', unit: 'm', values: [1, 2] },
        { name: 'phase', unit: 'deg', values: [0, 90] },
      ],
    };
    expect(sharedUnit(resolveTraces(harmonic))).toBeNull();
  });
});

describe('axisLabel', () => {
  it('appends a unit when there is one', () => {
    expect(axisLabel('alpha', 'deg')).toBe('alpha [deg]');
  });

  it('leaves a dimensionless quantity bare', () => {
    // `"1"` is how the protocol spells dimensionless, and `x/c [1]` is noise.
    expect(axisLabel('x/c', '1')).toBe('x/c');
    expect(axisLabel('x/c', null)).toBe('x/c');
  });
});

describe('nearestPoint', () => {
  const traces = resolveTraces(SHARED);
  // A projection with wildly different scales per axis — which is the point.
  const project = (p: { x: number; y: number }) => ({ x: p.x * 400, y: 200 - p.y * 100 });

  it('decides in screen space, not data space', () => {
    // Query near (x=1, y=0.1) on cp_upper. In *data* space the nearest point is on
    // cp_lower — |0.1 - 0.1| = 0 — but both share that value, and what separates them on
    // screen is the projection. Adding metres to pascals is what a data-space distance does.
    const found = nearestPoint(traces, { x: 400, y: 190 }, project);
    expect(found).not.toBeNull();
    expect(found!.x).toBe(1);
    expect(found!.distance).toBeLessThan(1);
  });

  it('respects the radius, so a pointer far from the curve reports nothing', () => {
    expect(nearestPoint(traces, { x: 0, y: 0 }, project, 5)).toBeNull();
  });

  it('skips non-finite points rather than returning NaN as the closest thing', () => {
    const gapped = resolveTraces({
      name: 'gap',
      x: { name: 'x', unit: '1', values: [0, 1, 2] },
      traces: [{ name: 'g', unit: '1', values: [0, NaN, 2] }],
    });
    const found = nearestPoint(gapped, { x: 400, y: 200 }, project);
    expect(found).not.toBeNull();
    expect(Number.isFinite(found!.y)).toBe(true);
  });

  it('reports which trace and which index, so a caller can look the point up', () => {
    const found = nearestPoint(traces, { x: 200, y: 260 }, project);
    expect(found!.trace.name).toBe('cp_upper');
    expect(found!.at).toBe(1);
  });
});
