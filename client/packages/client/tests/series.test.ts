/**
 * The `series1d` kind and the `series` key (protocol 1.4, #69).
 *
 * The fixture corpus already asserts accept-or-reject on both sides of the protocol. These
 * assert what comes *out* — because `validateJobResult` rebuilds the envelope field by field, so
 * a key nobody wired up is dropped silently rather than failing loudly, and that is exactly how
 * `metrics` would have been lost if nobody had looked.
 */

import { describe, expect, it } from 'vitest';

import type { Grid2DResult, Series1DData, Series1DResult } from '../src/types.js';
import {
  fieldNames,
  fieldValues,
  isFieldResult,
  isSeries1D,
  resultSeries,
  traceAbscissa,
} from '../src/types.js';
import { validateJobResult, validateSeries1D } from '../src/validate.js';

const SURFACE: Series1DData = {
  name: 'surface_cp',
  traces: [
    { name: 'cp_upper', unit: '1', values: [1, -0.9], x: { name: 'x/c', unit: '1', values: [0, 0.3] } },
    { name: 'cp_lower', unit: '1', values: [1, 0.1, 0], x: { name: 'x/c', unit: '1', values: [0, 0.4, 0.9] } },
  ],
};

const SWEEP: Series1DData = {
  name: 'lift_curve',
  x: { name: 'alpha', unit: 'deg', values: [-2, 0, 2] },
  traces: [{ name: 'c_l', unit: '1', values: [-0.24, 0, 0.24] }],
};

const GRID = {
  job_id: 'j-1',
  kind: 'grid2d',
  data: { bounds: [0, 0, 1, 1], shape: [1, 2], fields: { psi: [0, 1] }, mask: [0, 0] },
  artifacts: [],
};

describe('curves on a field result', () => {
  it('survives validation instead of being quietly dropped', () => {
    const result = validateJobResult({ ...GRID, series: [SURFACE] }) as Grid2DResult;
    expect(result.series).toHaveLength(1);
    expect(result.series?.[0]?.traces.map((t) => t.name)).toEqual(['cp_upper', 'cp_lower']);
  });

  it('comes back as an empty list when the server sent none', () => {
    expect((validateJobResult(GRID) as Grid2DResult).series).toEqual([]);
  });

  it('keeps the per-trace abscissa, which is the whole reason it exists', () => {
    const result = validateJobResult({ ...GRID, series: [SURFACE] }) as Grid2DResult;
    const [upper, lower] = result.series![0]!.traces;
    // A staircase does not put a face at the same x/c on both surfaces, so the two curves are
    // sampled differently and resampling one onto the other would invent data.
    expect(upper!.x!.values).not.toEqual(lower!.x!.values);
    expect(upper!.values).toHaveLength(upper!.x!.values.length);
  });
});

describe('a series1d result', () => {
  const result = validateJobResult({
    job_id: 'j-2',
    kind: 'series1d',
    data: SWEEP,
    artifacts: [],
  }) as Series1DResult;

  it('carries its curves in data, because that is what kind selects', () => {
    expect(result.kind).toBe('series1d');
    expect(result.data.name).toBe('lift_curve');
    expect(result.series ?? []).toEqual([]);
  });

  it('is not a field result, and the type guards say so', () => {
    expect(isSeries1D(result)).toBe(true);
    expect(isFieldResult(result)).toBe(false);
    // No field to read, and asking must give nothing rather than throwing three layers down.
    expect(fieldValues(result, 'psi')).toBeUndefined();
    expect(fieldNames(result)).toEqual([]);
  });
});

describe('resultSeries', () => {
  it('reads curves from whichever of the two places they are in', () => {
    const field = validateJobResult({ ...GRID, series: [SURFACE] });
    const curve = validateJobResult({
      job_id: 'j-3',
      kind: 'series1d',
      data: SWEEP,
      artifacts: [],
    });
    expect(resultSeries(field).map((s) => s.name)).toEqual(['surface_cp']);
    expect(resultSeries(curve).map((s) => s.name)).toEqual(['lift_curve']);
    expect(resultSeries(validateJobResult(GRID))).toEqual([]);
  });
});

describe('traceAbscissa', () => {
  it('prefers the trace’s own abscissa and falls back to the shared one', () => {
    expect(traceAbscissa(SWEEP, SWEEP.traces[0]!)?.name).toBe('alpha');
    expect(traceAbscissa(SURFACE, SURFACE.traces[0]!)?.values).toEqual([0, 0.3]);
    // Neither: un-plottable. The validator refuses such a payload, and the accessor must report
    // the absence rather than invent an abscissa from index order.
    expect(traceAbscissa({ name: 's', traces: [] }, SWEEP.traces[0]!)).toBeUndefined();
  });
});

describe('validateSeries1D', () => {
  it('rejects a curve with no abscissa anywhere rather than assuming index order', () => {
    expect(() =>
      validateSeries1D({ name: 's', traces: [{ name: 'c_l', unit: '1', values: [0, 1] }] }),
    ).toThrow(/no abscissa/);
  });

  it('requires a unit, unlike a field', () => {
    expect(() =>
      validateSeries1D({
        name: 's',
        x: { name: 'alpha', unit: 'deg', values: [0, 1] },
        traces: [{ name: 'c_l', values: [0, 1] }],
      }),
    ).toThrow(/unit/);
  });

  it('refuses a curve long enough to be a field', () => {
    const many = Array.from({ length: 5000 }, (_, i) => i);
    expect(() =>
      validateSeries1D({
        name: 's',
        x: { name: 'i', unit: '1', values: many },
        traces: [{ name: 'v', unit: '1', values: many }],
      }),
    ).toThrow(/over the/);
  });
});
