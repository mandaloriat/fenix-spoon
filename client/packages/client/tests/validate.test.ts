/**
 * Validator behaviour that the fixture corpus cannot express.
 *
 * The fixtures assert accept-or-reject; these assert what comes *out* of a validator.
 * That matters because `validateJobResult` rebuilds the envelope field by field, so a
 * field nobody wired up is dropped silently rather than failing loudly.
 */

import { describe, expect, it } from 'vitest';

import { validateJobResult } from '../src/validate.js';

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
