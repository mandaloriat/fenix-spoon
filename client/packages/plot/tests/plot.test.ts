/**
 * `<fs-plot>` as a page uses it.
 *
 * jsdom has no canvas, so `getContext('2d')` returns null and every paint is a no-op. That
 * bounds what this file can claim and the tests are written to that bound: they check what the
 * element *decides* — which curve set, which captions, what it announces, what it refuses —
 * and never that a pixel was set. Anything that could be asserted about geometry lives in
 * `scale.test.ts` and `curves.test.ts`, which is why those two files are the long ones.
 */

import type { JobResult, Series1DData } from '@fenix-spoon/client';
import { beforeAll, describe, expect, it } from 'vitest';

import { PlotElement, definePlot } from '../src/element.js';

beforeAll(() => {
  definePlot();
});

const SURFACE: Series1DData = {
  name: 'surface_cp',
  description: 'Pressure coefficient along the surface',
  x: { name: 'x/c', unit: '1', values: [0, 0.5, 1] },
  traces: [
    { name: 'cp_upper', unit: '1', values: [-1.2, -0.6, 0.1] },
    { name: 'cp_lower', unit: '1', values: [0.4, 0.2, 0.1] },
  ],
};

const CONVERGENCE: Series1DData = {
  name: 'residual',
  x: { name: 'iteration', unit: '1', values: [1, 2, 3] },
  traces: [{ name: 'residual', unit: '1', values: [1e-1, 1e-4, 1e-7] }],
};

/** A field result carrying curves beside `data` — what the airfoil adapters return. */
const FIELD_RESULT = {
  job_id: 'j-1',
  kind: 'mesh2d',
  data: { bounds: [0, 0, 1, 1], points: [], triangles: [], point_fields: {} },
  series: [SURFACE, CONVERGENCE],
} as unknown as JobResult;

/** A `series1d` result, where the curve set *is* the payload. */
const SERIES_RESULT = {
  job_id: 'j-2',
  kind: 'series1d',
  data: SURFACE,
} as unknown as JobResult;

function mount(attributes: Record<string, string> = {}): PlotElement {
  const element = document.createElement('fs-plot') as PlotElement;
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  document.body.appendChild(element);
  return element;
}

describe('choosing what to draw', () => {
  it('reads the curves out of a field result', () => {
    const plot = mount();
    plot.result = FIELD_RESULT;
    expect(plot.available).toEqual(['surface_cp', 'residual']);
    expect(plot.curves?.name).toBe('surface_cp');
  });

  it('reads them out of a series1d result, where the payload is the curve set', () => {
    // Two homes on the wire, one accessor: the element never branches on `kind` itself.
    const plot = mount();
    plot.result = SERIES_RESULT;
    expect(plot.available).toEqual(['surface_cp']);
    expect(plot.curves?.name).toBe('surface_cp');
  });

  it('selects by name', () => {
    const plot = mount({ series: 'residual' });
    plot.result = FIELD_RESULT;
    expect(plot.curves?.name).toBe('residual');
  });

  it('draws nothing rather than something else when the name is unknown', () => {
    // Falling back to the first curve set would put a convergence history under a caption
    // asking for a surface distribution.
    const plot = mount({ series: 'nope' });
    plot.result = FIELD_RESULT;
    expect(plot.curves).toBeNull();
    expect(plot.capabilities.draw.available).toBe(false);
  });

  it('takes a curve set directly, for a page that assembled its own', () => {
    const plot = mount();
    plot.curves = CONVERGENCE;
    expect(plot.curves?.name).toBe('residual');
  });
});

describe('captions', () => {
  it('labels the x axis from the abscissa, and omits a dimensionless unit', () => {
    const plot = mount();
    plot.result = FIELD_RESULT;
    const label = plot.querySelector('canvas')!.getAttribute('aria-label')!;
    expect(label).toContain('x axis x/c');
    expect(label).not.toContain('[1]');
  });

  it('names a lone trace on the y axis, with its unit', () => {
    const plot = mount();
    plot.curves = {
      name: 'sweep',
      x: { name: 'alpha', unit: 'deg', values: [0, 5] },
      traces: [{ name: 'c_l', unit: '1', values: [0.1, 0.6] }],
    };
    const label = plot.querySelector('canvas')!.getAttribute('aria-label')!;
    expect(label).toContain('x axis alpha [deg]');
    expect(label).toContain('y axis c_l');
  });

  it('leaves the y axis unlabelled when the traces carry different units', () => {
    const plot = mount();
    plot.curves = {
      name: 'response',
      x: { name: 'f', unit: 'Hz', values: [1, 2] },
      traces: [
        { name: 'magnitude', unit: 'm', values: [1, 2] },
        { name: 'phase', unit: 'deg', values: [0, 90] },
      ],
    };
    expect(plot.querySelector('canvas')!.getAttribute('aria-label')).toContain(
      'y axis unlabelled',
    );
  });

  it('lets the page override both', () => {
    const plot = mount({ 'x-label': 'chord fraction', 'y-label': 'pressure' });
    plot.result = FIELD_RESULT;
    const label = plot.querySelector('canvas')!.getAttribute('aria-label')!;
    expect(label).toContain('x axis chord fraction');
    expect(label).toContain('y axis pressure');
  });
});

describe('invert-y', () => {
  it('is off unless the page asks', () => {
    // The convention for C_p is real, and inferring it from a trace called `cp_upper` is the
    // class of guess ADR 0001 records the viewer refusing. A name is not a quantity.
    const plot = mount();
    plot.result = FIELD_RESULT;
    expect(plot.invertY).toBe(false);
    expect(plot.querySelector('canvas')!.getAttribute('aria-label')).not.toContain('inverted');
  });

  it('is announced when it is on, because a reader cannot see the axis', () => {
    const plot = mount({ 'invert-y': '' });
    plot.result = FIELD_RESULT;
    expect(plot.querySelector('canvas')!.getAttribute('aria-label')).toContain('inverted');
  });
});

describe('capabilities', () => {
  it('refuses to draw with no curves, and says why', () => {
    const plot = mount();
    expect(plot.capabilities.draw.available).toBe(false);
    expect(plot.capabilities.draw.reason).toBeTruthy();
  });

  it('allows a log y axis on strictly positive data', () => {
    const plot = mount();
    plot.curves = CONVERGENCE;
    expect(plot.capabilities.logY.available).toBe(true);
  });

  it('refuses a log y axis where the curve reaches zero, naming the clip', () => {
    // A page offering a log toggle needs this *before* drawing a frame that silently
    // relocated half the curve to the floor.
    const plot = mount();
    plot.curves = SURFACE;
    expect(plot.capabilities.logY.available).toBe(false);
    expect(plot.capabilities.logY.reason).toContain('positive');
  });
});

describe('accessibility', () => {
  it('describes the canvas, which is otherwise opaque', () => {
    const plot = mount();
    plot.result = FIELD_RESULT;
    const canvas = plot.querySelector('canvas')!;
    expect(canvas.getAttribute('role')).toBe('img');
    expect(canvas.getAttribute('aria-label')).toContain('2 curves');
    expect(canvas.getAttribute('aria-label')).toContain('cp_upper, cp_lower');
    expect(canvas.getAttribute('aria-label')).toContain('6 points');
  });

  it('says so when there is nothing to describe', () => {
    const plot = mount();
    expect(plot.querySelector('canvas')!.getAttribute('aria-label')).toBe(
      'Plot with no curves.',
    );
  });

  it('carries a live region for the hover readout', () => {
    // A hover that only paints pixels is invisible to a screen reader, and the number under
    // the pointer is the entire answer a probe gives.
    const plot = mount();
    const readout = plot.querySelector('[aria-live]');
    expect(readout).not.toBeNull();
    expect(readout!.getAttribute('aria-live')).toBe('polite');
  });

  it('redescribes when the curve set changes', () => {
    const plot = mount();
    plot.result = FIELD_RESULT;
    plot.series = 'residual';
    expect(plot.querySelector('canvas')!.getAttribute('aria-label')).toContain('1 curve');
  });
});

describe('attributes and properties agree', () => {
  it('reflects the boolean ones', () => {
    const plot = mount();
    plot.invertY = true;
    expect(plot.hasAttribute('invert-y')).toBe(true);
    plot.legend = true;
    expect(plot.hasAttribute('legend')).toBe(true);
    plot.invertY = false;
    expect(plot.hasAttribute('invert-y')).toBe(false);
  });

  it('falls back to linear on an unrecognised scale rather than throwing mid-paint', () => {
    const plot = mount({ 'y-scale': 'sqrt' });
    expect(plot.yScale).toBe('linear');
  });

  it('round-trips the scale kinds it does know', () => {
    const plot = mount();
    plot.yScale = 'log';
    expect(plot.getAttribute('y-scale')).toBe('log');
    expect(plot.yScale).toBe('log');
  });
});

describe('painting is coalesced', () => {
  const frame = () => new Promise((done) => requestAnimationFrame(() => done(null)));

  it('paints at most once per frame however many times it is asked', async () => {
    // A pointer crossing a dense curve resolves a different nearest point many times per
    // frame, and each one used to repaint the whole thing — axes, ticks, every trace.
    const plot = mount();
    plot.result = FIELD_RESULT;
    let paints = 0;
    const draw = plot.draw.bind(plot);
    plot.draw = () => {
      paints += 1;
      draw();
    };
    for (let i = 0; i < 20; i += 1) plot.render();
    expect(paints).toBe(0);
    await frame();
    expect(paints).toBe(1);
  });

  it('still describes the canvas synchronously', () => {
    // The accessible name is a function of the data, so deferring it to a frame would be a
    // milder version of deferring it to a rendering context — the bug already fixed once.
    const plot = mount();
    plot.result = FIELD_RESULT;
    expect(plot.querySelector('canvas')!.getAttribute('aria-label')).toContain('2 curves');
  });

  it('drops a scheduled frame when the element leaves the document', () => {
    // Otherwise a frame paints into a canvas nobody can see, and keeps the element alive
    // until it runs.
    const plot = mount();
    plot.result = FIELD_RESULT;
    plot.render();
    expect(() => plot.remove()).not.toThrow();
  });
});
