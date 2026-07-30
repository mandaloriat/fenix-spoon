import { describe, expect, it } from 'vitest';

import type { Grid2DResult, Mesh2DResult } from '@fenix-spoon/client';

import { sampleAt, sampleSection, sampleVectorAt } from '../src/field.js';
import {
  MAX_STREAMLINE_DENSITY,
  integralCurves,
  seedLattice,
  streamlineAvailability,
} from '../src/streamlines.js';

/**
 * A grid on `[-1, 1]^2` carrying an analytic vector field.
 *
 * Analytic on purpose: an integral curve of a known field has a known shape, so the test
 * is an assertion about geometry rather than about pixels. `psi` rides along as a scalar,
 * because the interesting refusal is "you asked me to integrate a scalar".
 */
function analyticGrid(
  n: number,
  vector: (x: number, y: number) => [number, number],
  options: { mask?: (x: number, y: number) => boolean } = {},
): Grid2DResult {
  const fields: number[] = [];
  const vectors: [number, number][] = [];
  const mask: number[] = [];
  for (let iy = 0; iy < n; iy += 1) {
    for (let ix = 0; ix < n; ix += 1) {
      const x = -1 + (2 * ix) / (n - 1);
      const y = -1 + (2 * iy) / (n - 1);
      fields.push(x * x + y * y);
      vectors.push(vector(x, y));
      mask.push(options.mask?.(x, y) ? 1 : 0);
    }
  }
  return {
    job_id: `j-${n}`,
    kind: 'grid2d',
    data: {
      bounds: [-1, -1, 1, 1],
      shape: [n, n],
      fields: { r2: fields },
      vector_fields: { field: vectors },
      mask,
    },
    stats: {},
    artifacts: [],
  };
}

/**
 * A triangulated unit square, `n` nodes per side, with `psi = y` and a uniform rightward
 * vector at every node.
 *
 * A real triangulation rather than the two-triangle square the older tests use: the step
 * length is derived from the mesh's own element size, so a mesh of two elements takes
 * steps the size of the domain and tests nothing about integration. It also puts the
 * bucket index through a case where it matters.
 */
function analyticMesh(n: number): Mesh2DResult {
  const points: [number, number][] = [];
  const psi: number[] = [];
  const vectors: [number, number][] = [];
  for (let iy = 0; iy < n; iy += 1) {
    for (let ix = 0; ix < n; ix += 1) {
      const x = ix / (n - 1);
      const y = iy / (n - 1);
      points.push([x, y]);
      psi.push(y);
      vectors.push([1, 0]);
    }
  }
  const triangles: [number, number, number][] = [];
  for (let iy = 0; iy < n - 1; iy += 1) {
    for (let ix = 0; ix < n - 1; ix += 1) {
      const a = iy * n + ix;
      triangles.push([a, a + 1, a + n + 1], [a, a + n + 1, a + n]);
    }
  }
  return {
    job_id: `j-mesh-${n}`,
    kind: 'mesh2d',
    data: {
      bounds: [0, 0, 1, 1],
      points,
      triangles,
      point_fields: { psi },
      point_vector_fields: { u: vectors },
    },
    stats: {},
    artifacts: [],
  };
}

const MESH = analyticMesh(17);

describe('availability', () => {
  it('refuses a result that carries no vector field, and says why', () => {
    const scalarOnly = analyticGrid(9, () => [0, 0]);
    delete (scalarOnly.data as { vector_fields?: unknown }).vector_fields;
    const verdict = streamlineAvailability(scalarOnly);
    expect(verdict.available).toBe(false);
    expect(verdict.fields).toEqual([]);
    expect(verdict.reason).toMatch(/no vector field/i);
    expect(verdict.reason).toMatch(/cannot be reconstructed/i);
    expect(integralCurves(scalarOnly, 'r2')).toEqual([]);
  });

  it('refuses to integrate a scalar field even when a vector one exists', () => {
    // The failure this guards against is the tempting one: a page passes whatever is in
    // the field picker, and a viewer that shrugged and integrated it would draw a picture
    // that looks like physics and is not.
    const grid = analyticGrid(9, (x, y) => [-y, x]);
    const verdict = streamlineAvailability(grid, 'r2');
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toMatch(/scalar field/);
    expect(verdict.reason).toContain('field'); // names the vector field it does have
    expect(integralCurves(grid, 'r2')).toEqual([]);
  });

  it('names the available vector fields for an unknown name', () => {
    const verdict = streamlineAvailability(analyticGrid(9, () => [1, 0]), 'nope');
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toMatch(/not a vector field/);
    expect(verdict.fields).toEqual(['field']);
  });

  it('has an answer with no result at all', () => {
    expect(streamlineAvailability(null).available).toBe(false);
    expect(streamlineAvailability(null).reason).toMatch(/No result/);
  });
});

describe('integration against known fields', () => {
  it('traces circles through a rigid-rotation field', () => {
    // v = (-y, x) has circles about the origin as its integral curves, exactly. Seeding at
    // radius 0.5 must stay at radius 0.5 for the whole curve — that is the analytic claim
    // an RK4 integrator either satisfies or does not.
    const grid = analyticGrid(129, (x, y) => [-y, x]);
    const [curve] = integralCurves(grid, 'field', { seeds: [[0.5, 0]], bothWays: false });
    expect(curve).toBeDefined();
    expect(curve!.points.length).toBeGreaterThan(50);
    for (const [x, y] of curve!.points) {
      expect(Math.hypot(x, y)).toBeCloseTo(0.5, 2);
    }
    // It comes back to where it started, and says so.
    expect(curve!.closed).toBe(true);
    // One turn of a radius-0.5 circle is pi; the curve stops at the first return.
    expect(curve!.length).toBeGreaterThan(2.8);
    expect(curve!.length).toBeLessThan(3.5);
  });

  it('goes the right way round', () => {
    // Counter-clockwise for v = (-y, x): from (0.5, 0) — which is `points[0]`, the seed —
    // the next point must have y > 0.
    const grid = analyticGrid(65, (x, y) => [-y, x]);
    const [curve] = integralCurves(grid, 'field', { seeds: [[0.5, 0]], bothWays: false });
    expect(curve!.points[0]).toEqual([0.5, 0]);
    expect(curve!.points[1]![1]).toBeGreaterThan(0);
  });

  it('traces straight lines through a uniform field and stops at the boundary', () => {
    const grid = analyticGrid(33, () => [1, 0]);
    const [curve] = integralCurves(grid, 'field', { seeds: [[-0.9, 0.25]], bothWays: false });
    expect(curve).toBeDefined();
    for (const [, y] of curve!.points) expect(y).toBeCloseTo(0.25, 6);
    const last = curve!.points.at(-1)!;
    // It must reach the far edge and not step over it.
    expect(last[0]).toBeGreaterThan(0.9);
    expect(last[0]).toBeLessThanOrEqual(1);
  });

  it('never leaves the domain, whatever the seed', () => {
    const grid = analyticGrid(65, (x, y) => [1 + 0.5 * y, 0.5 * x]);
    for (const curve of integralCurves(grid, 'field', { density: 8 })) {
      for (const [x, y] of curve.points) {
        expect(x).toBeGreaterThanOrEqual(-1);
        expect(x).toBeLessThanOrEqual(1);
        expect(y).toBeGreaterThanOrEqual(-1);
        expect(y).toBeLessThanOrEqual(1);
      }
    }
  });

  it('stops at a hole instead of drawing through it', () => {
    // A square obstacle in the middle of a rightward flow. Nothing was solved inside it,
    // so no curve point may land there — a line through the middle of a body is the exact
    // picture this stopping rule exists to prevent.
    const inHole = (x: number, y: number) => x > -0.25 && x < 0.25 && y > -0.25 && y < 0.25;
    const grid = analyticGrid(65, () => [1, 0], { mask: inHole });
    const curves = integralCurves(grid, 'field', { seeds: [[-0.9, 0]], bothWays: false });
    expect(curves).toHaveLength(1);
    for (const [x, y] of curves[0]!.points) expect(inHole(x, y)).toBe(false);
    // And it stopped *before* the hole rather than jumping it.
    expect(curves[0]!.points.at(-1)![0]).toBeLessThan(0.25);
  });

  it('stops where the field stagnates rather than coasting on the last heading', () => {
    // v = (x, 0): zero on the whole line x = 0, and a curve arriving there has no
    // direction to continue in. Continuing would invent one.
    const grid = analyticGrid(65, (x) => [x, 0]);
    const [curve] = integralCurves(grid, 'field', { seeds: [[-0.8, 0.5]], bothWays: false });
    expect(curve).toBeDefined();
    for (const [x] of curve!.points) expect(x).toBeLessThanOrEqual(1e-6);
  });

  it('integrates on a mesh as well as on a grid', () => {
    const [curve] = integralCurves(MESH, 'u', { seeds: [[0.1, 0.5]], bothWays: false });
    expect(curve).toBeDefined();
    for (const [, y] of curve!.points) expect(y).toBeCloseTo(0.5, 6);
    expect(curve!.points.at(-1)![0]).toBeGreaterThan(0.9);
  });
});

describe('seeding', () => {
  it('lays seeds on a lattice inside the window', () => {
    const seeds = seedLattice([0, 0, 4, 2], 4);
    expect(seeds).toHaveLength(8); // 4 columns, 2 rows at square cells
    for (const [x, y] of seeds) {
      expect(x).toBeGreaterThan(0);
      expect(x).toBeLessThan(4);
      expect(y).toBeGreaterThan(0);
      expect(y).toBeLessThan(2);
    }
  });

  it('caps density so a slider cannot freeze the tab', () => {
    expect(seedLattice([0, 0, 1, 1], 1e6).length).toBeLessThanOrEqual(
      MAX_STREAMLINE_DENSITY * MAX_STREAMLINE_DENSITY,
    );
  });

  it('gives more curves at a higher density, without re-running anything', () => {
    const grid = analyticGrid(65, (x, y) => [1 + 0.2 * y, 0.2 * x]);
    const sparse = integralCurves(grid, 'field', { density: 4 });
    const dense = integralCurves(grid, 'field', { density: 16 });
    expect(sparse.length).toBeGreaterThan(0);
    expect(dense.length).toBeGreaterThan(sparse.length);
  });

  it('honours explicit seeds over the lattice', () => {
    const grid = analyticGrid(33, () => [1, 0]);
    const curves = integralCurves(grid, 'field', {
      seeds: [
        [-0.5, -0.5],
        [-0.5, 0.5],
      ],
      density: 32,
    });
    expect(curves).toHaveLength(2);
    expect(curves.map((c) => c.seed)).toEqual([
      [-0.5, -0.5],
      [-0.5, 0.5],
    ]);
  });

  it('drops a seed outside the solved region rather than starting nowhere', () => {
    const grid = analyticGrid(33, () => [1, 0], { mask: (x) => x < 0 });
    expect(integralCurves(grid, 'field', { seeds: [[-0.5, 0]] })).toEqual([]);
  });

  it('keeps curves apart, so a dense seeding does not draw one line many times', () => {
    const grid = analyticGrid(65, () => [1, 0]);
    const curves = integralCurves(grid, 'field', { density: 24 });
    // Uniform flow: every curve is a horizontal line, so distinct curves must sit at
    // distinct heights, at least the separation apart.
    const heights = curves.map((c) => c.seed[1]).sort((a, b) => a - b);
    for (let i = 1; i < heights.length; i += 1) {
      expect(heights[i]! - heights[i - 1]!).toBeGreaterThan(1e-6);
    }
    expect(curves.length).toBeLessThan(24 * 24);
  });
});

describe('interpolated sampling', () => {
  it('interpolates a grid bilinearly, matching what the server would return', () => {
    // r2 = x^2 + y^2 sampled on a fine grid: half way between nodes the bilinear value is
    // the average of the corners, which for this field is a known number.
    const grid = analyticGrid(3, () => [1, 0]); // nodes at -1, 0, 1
    // Cell corners (0,0), (1,0), (0,1), (1,1) carry 0, 1, 1, 2; the centre is their mean.
    expect(sampleAt(grid, 'r2', [0.5, 0.5])).toBeCloseTo(1, 12);
    expect(sampleAt(grid, 'r2', [0, 0])).toBeCloseTo(0, 12);
    expect(sampleAt(grid, 'r2', [5, 5])).toBeUndefined();
  });

  it('renormalises around masked corners instead of blending in filler', () => {
    const grid = analyticGrid(3, () => [1, 0], { mask: (x, y) => x > 0.5 && y > 0.5 });
    // The (1, 1) corner is masked; the value near it is the average of the three live
    // corners, weighted — never a blend that includes the placeholder.
    const value = sampleAt(grid, 'r2', [0.99, 0.99]);
    expect(value).toBeDefined();
    expect(value!).toBeLessThan(2);
    // Fully inside the masked region there is nothing to report.
    expect(sampleAt(grid, 'r2', [1, 1])).toBeUndefined();
  });

  it('interpolates vectors the same way', () => {
    const grid = analyticGrid(65, (x, y) => [-y, x]);
    const v = sampleVectorAt(grid, 'field', [0.3, 0.4]);
    expect(v![0]).toBeCloseTo(-0.4, 3);
    expect(v![1]).toBeCloseTo(0.3, 3);
    expect(sampleVectorAt(grid, 'r2', [0, 0])).toBeUndefined();
  });

  it('samples a mesh barycentrically', () => {
    expect(sampleAt(MESH, 'psi', [0.5, 0.25])).toBeCloseTo(0.25, 9);
    expect(sampleAt(MESH, 'psi', [5, 5])).toBeUndefined();
  });
});

describe('sections', () => {
  it('samples along a line in the shape the server uses', () => {
    const grid = analyticGrid(33, () => [1, 0]);
    const section = sampleSection(grid, 'r2', [-1, 0], [1, 0], 5)!;
    expect(section.requested).toBe(5);
    expect(section.skipped).toBe(0);
    expect(section.distance[0]).toBeCloseTo(0);
    expect(section.distance.at(-1)).toBeCloseTo(2);
    // r2 = x^2 along y = 0: 1, 0.25, 0, 0.25, 1.
    expect(section.value.map((v) => Number(v.toFixed(6)))).toEqual([1, 0.25, 0, 0.25, 1]);
    expect(section.at).toHaveLength(5);
  });

  it('drops the samples that fall in a hole and counts them', () => {
    const grid = analyticGrid(65, () => [1, 0], {
      mask: (x, y) => x > -0.2 && x < 0.2 && y > -0.2 && y < 0.2,
    });
    const section = sampleSection(grid, 'r2', [-1, 0], [1, 0], 101)!;
    expect(section.skipped).toBeGreaterThan(0);
    expect(section.value).toHaveLength(101 - section.skipped);
    expect(section.distance).toHaveLength(section.value.length);
  });

  it('caps the sample budget and refuses a field it does not have', () => {
    const grid = analyticGrid(9, () => [1, 0]);
    expect(sampleSection(grid, 'r2', [-1, -1], [1, 1], 1e9)!.requested).toBe(4096);
    expect(sampleSection(grid, 'r2', [-1, -1], [1, 1], 0)!.requested).toBe(2);
    expect(sampleSection(grid, 'nope', [-1, -1], [1, 1])).toBeUndefined();
  });
});
