/**
 * Validator behaviour that the fixture corpus cannot express.
 *
 * The fixtures assert accept-or-reject; these assert what comes *out* of a validator.
 * That matters because `validateJobResult` rebuilds the envelope field by field, so a
 * field nobody wired up is dropped silently rather than failing loudly.
 */

import { describe, expect, it } from 'vitest';

import { axisLabels } from '../src/types.js';
import { validateGeometry, validateJobResult } from '../src/validate.js';

const GRID = {
  job_id: 'j-1',
  kind: 'grid2d',
  data: { bounds: [0, 0, 1, 1], shape: [1, 2], fields: { psi: [0, 1] }, mask: [0, 0] },
  artifacts: [],
};

describe('validateJobResult', () => {
  it('carries solve statistics through', () => {
    const stats = { cells: 2, iterations: 50, seconds: 0.5 };
    expect(validateJobResult({ ...GRID, stats }).stats).toEqual(stats);
  });

  it('defaults stats to {} when the server does not send it', () => {
    // Servers older than the field omit it; consumers should not have to null-check.
    expect(validateJobResult(GRID).stats).toEqual({});
  });

  it('rejects a non-numeric stat rather than passing it to a chart', () => {
    expect(() => validateJobResult({ ...GRID, stats: { cells: 'many' } })).toThrow(/stats.cells/);
  });

  it('keeps stats on mesh2d results too', () => {
    const mesh = {
      job_id: 'j-2',
      kind: 'mesh2d',
      data: {
        bounds: [0, 0, 1, 1],
        points: [[0, 0], [1, 0], [0, 1]],
        triangles: [[0, 1, 2]],
        point_fields: { psi: [0, 0, 1] },
      },
      stats: { cells: 1, dofs: 3 },
    };
    expect(validateJobResult(mesh).stats).toEqual({ cells: 1, dofs: 3 });
  });
});

const SECTION = {
  type: 'axisymmetric2d',
  bounds: [0, -0.01, 0.02, 0.01],
  regions: [
    {
      name: 'rod',
      shape: {
        type: 'polygon2d',
        points: [
          [0, -0.005],
          [0.005, -0.005],
          [0.005, 0.005],
          [0, 0.005],
        ],
      },
      material: { voltage: 1 },
    },
  ],
};

describe('axisymmetric2d (protocol 1.13)', () => {
  it('keeps the regions and the background it was given', () => {
    const parsed = validateGeometry({ ...SECTION, background: { eps_r: 1 } });
    expect(parsed.type).toBe('axisymmetric2d');
    expect(parsed).toMatchObject({ background: { eps_r: 1 } });
    expect('regions' in parsed && parsed.regions[0]!.material).toEqual({ voltage: 1 });
  });

  it('refuses a negative rmin, because the first coordinate is a radius', () => {
    // The rule the kind exists for. Without it the payload is an ordinary plane section
    // that a solver would answer rather than refuse.
    expect(() => validateGeometry({ ...SECTION, bounds: [-0.01, -0.01, 0.02, 0.01] })).toThrow(
      /rmin must be >= 0/,
    );
  });

  it('allows a region on the axis and only on the axis', () => {
    // Above: a rod whose outline lies on r = 0, accepted. Below: the same outline against a
    // section starting at r = 1 mm, where that edge is a truncation rather than a symmetry.
    expect(() => validateGeometry(SECTION)).not.toThrow();
    expect(() =>
      validateGeometry({
        ...SECTION,
        bounds: [0.001, -0.01, 0.02, 0.01],
        regions: [
          {
            ...SECTION.regions[0]!,
            shape: {
              type: 'polygon2d',
              points: [
                [0.001, -0.005],
                [0.005, -0.005],
                [0.005, 0.005],
                [0.001, 0.005],
              ],
            },
          },
        ],
      }),
    ).toThrow(/strictly inside/);
  });

  it('enforces the same region rules as regions2d', () => {
    expect(() =>
      validateGeometry({ ...SECTION, regions: [SECTION.regions[0]!, SECTION.regions[0]!] }),
    ).toThrow(/unique/);
  });
});

describe('axisLabels', () => {
  it('reads the coordinates off the kind, which is the one claim the server checks', () => {
    expect(axisLabels({ type: 'axisymmetric2d' })).toEqual(['r', 'z']);
    expect(axisLabels({ type: 'regions2d' })).toEqual(['x', 'y']);
    expect(axisLabels({ type: 'domain2d' })).toEqual(['x', 'y']);
  });

  it('is what a page labels a meridian section with', () => {
    // The point of the function existing at all (ADR 0003): a page drawing this geometry
    // must not mark its horizontal axis x, and must not have to know why.
    expect(axisLabels(validateGeometry(SECTION))).toEqual(['r', 'z']);
  });
});
