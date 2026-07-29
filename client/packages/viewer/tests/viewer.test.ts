import { describe, expect, it } from 'vitest';

import type { Grid2DResult, Mesh2DResult } from '@fenix-spoon/client';

import {
  COLORMAP_NAMES,
  colorbarTicks,
  fieldRange,
  isColormapName,
  normalise,
  padRange,
  sampleColormap,
  symmetricRange,
} from '../src/colormap.js';
import { FieldViewerElement, defineFieldViewer } from '../src/element.js';
import {
  barycentric,
  contourSegments,
  isoLevels,
  probe,
  resultFieldNames,
  resultFieldValues,
} from '../src/field.js';

defineFieldViewer();

/** 3x3 grid on the unit square, psi = y so contours are horizontal lines. */
const GRID: Grid2DResult = {
  job_id: 'j-grid',
  kind: 'grid2d',
  data: {
    bounds: [0, 0, 1, 1],
    shape: [3, 3],
    fields: {
      psi: [0, 0, 0, 0.5, 0.5, 0.5, 1, 1, 1],
      speed: [0, 1, 2, 3, 4, 5, 6, 7, 8],
    },
    mask: [0, 0, 0, 0, 1, 0, 0, 0, 0],
  },
  stats: {},
  artifacts: [],
};

/** Same grid without the mask, for contouring: psi = y on a 3x3 unit square. */
const OPEN_GRID: Grid2DResult = {
  ...GRID,
  data: { ...GRID.data, mask: [0, 0, 0, 0, 0, 0, 0, 0, 0] },
};

/** Two triangles covering the unit square, values equal to y. */
const MESH: Mesh2DResult = {
  job_id: 'j-mesh',
  kind: 'mesh2d',
  data: {
    bounds: [0, 0, 1, 1],
    points: [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ],
    triangles: [
      [0, 1, 2],
      [0, 2, 3],
    ],
    point_fields: { psi: [0, 0, 1, 1] },
  },
  stats: {},
  artifacts: [],
};

describe('colormaps', () => {
  it('ships the documented set', () => {
    expect(COLORMAP_NAMES).toContain('viridis');
    expect(COLORMAP_NAMES).toContain('coolwarm');
    expect(isColormapName('viridis')).toBe(true);
    expect(isColormapName('nope')).toBe(false);
  });

  it('interpolates between stops and clamps outside [0, 1]', () => {
    const low = sampleColormap('viridis', 0);
    const high = sampleColormap('viridis', 1);
    expect(sampleColormap('viridis', -5)).toEqual(low);
    expect(sampleColormap('viridis', 5)).toEqual(high);

    const middle = sampleColormap('greyscale', 0.5);
    expect(middle[0]).toBeGreaterThan(100);
    expect(middle[0]).toBeLessThan(160);
    expect(middle[0]).toBe(middle[1]);
    expect(middle[1]).toBe(middle[2]);
  });

  it('returns a defined colour for a non-finite input', () => {
    expect(sampleColormap('viridis', NaN).every(Number.isFinite)).toBe(true);
  });

  it('does not accept inherited Object properties as colormap names', () => {
    // `'toString' in STOPS` is true, and sampling it would read past the end of a
    // function and throw — so the check must be own-property only.
    for (const inherited of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(isColormapName(inherited)).toBe(false);
      expect(() => sampleColormap(inherited, 0.5)).not.toThrow();
      expect(sampleColormap(inherited, 0.5)).toEqual(sampleColormap('viridis', 0.5));
    }
  });
});

describe('ranges', () => {
  it('ignores masked and non-finite entries', () => {
    expect(fieldRange([1, 99, 3], [0, 1, 0])).toEqual({ min: 1, max: 3 });
    expect(fieldRange([1, NaN, 3])).toEqual({ min: 1, max: 3 });
    expect(fieldRange([Infinity, 2])).toEqual({ min: 2, max: 2 });
  });

  it('falls back rather than returning an inverted range when all is masked', () => {
    const range = fieldRange([1, 2], [1, 1]);
    expect(range.max).toBeGreaterThan(range.min);
  });

  it('pads a constant field so normalisation never divides by zero', () => {
    const padded = padRange(fieldRange([5, 5, 5]));
    expect(padded.max).toBeGreaterThan(padded.min);
    expect(Number.isFinite(normalise(5, padded))).toBe(true);
  });

  it('makes a range symmetric about zero for diverging maps', () => {
    expect(symmetricRange({ min: -1, max: 4 })).toEqual({ min: -4, max: 4 });
    expect(normalise(0, symmetricRange({ min: -1, max: 4 }))).toBeCloseTo(0.5);
  });

  it('labels colorbar ticks readably, switching to exponential at the extremes', () => {
    expect(colorbarTicks({ min: 0, max: 1 }, 3).map((t) => t.label)).toEqual([
      '0.00',
      '0.50',
      '1.00',
    ]);
    expect(colorbarTicks({ min: 0, max: 5e6 }, 2)[1]!.label).toContain('e+');
    // Negative zero would otherwise print as "-0.00".
    expect(colorbarTicks({ min: -1, max: 1 }, 3)[1]!.label).toBe('0.00');
  });
});

describe('field access', () => {
  it('reads fields from either result kind through one interface', () => {
    expect(resultFieldNames(GRID).sort()).toEqual(['psi', 'speed']);
    expect(resultFieldNames(MESH)).toEqual(['psi']);
    expect(resultFieldValues(GRID, 'psi')).toHaveLength(9);
    expect(resultFieldValues(MESH, 'psi')).toHaveLength(4);
    expect(resultFieldValues(GRID, 'nope')).toBeUndefined();
  });
});

describe('probing', () => {
  it('samples a grid by nearest node', () => {
    expect(probe(GRID, 'psi', [0, 0])).toBe(0);
    expect(probe(GRID, 'psi', [1, 1])).toBe(1);
  });

  it('returns undefined inside a masked cell and outside the domain', () => {
    expect(probe(GRID, 'psi', [0.5, 0.5])).toBeUndefined(); // masked centre node
    expect(probe(GRID, 'psi', [5, 5])).toBeUndefined();
  });

  it('interpolates within a mesh triangle rather than snapping to a vertex', () => {
    // psi = y on this mesh, so an interior point must return its own y, not 0 or 1.
    const value = probe(MESH, 'psi', [0.5, 0.25]);
    expect(value).toBeCloseTo(0.25, 6);
    expect(value).not.toBe(0);
  });

  it('computes barycentric weights and rejects points outside the triangle', () => {
    const a: [number, number] = [0, 0];
    const b: [number, number] = [1, 0];
    const c: [number, number] = [0, 1];
    const inside = barycentric([0.25, 0.25], a, b, c);
    expect(inside).not.toBeNull();
    expect(inside!.reduce((s, w) => s + w, 0)).toBeCloseTo(1);
    expect(barycentric([2, 2], a, b, c)).toBeNull();
    // A degenerate (zero-area) triangle must not divide by zero.
    expect(barycentric([0, 0], a, b, [2, 0])).toBeNull();
  });
});

describe('contours', () => {
  it('places grid iso-lines at the right height', () => {
    // psi = y, so the 0.25 contour is the horizontal line y = 0.25.
    const segments = contourSegments(OPEN_GRID, 'psi', 0.25);
    expect(segments.length).toBeGreaterThan(0);
    for (const [a, b] of segments) {
      expect(a[1]).toBeCloseTo(0.25, 6);
      expect(b[1]).toBeCloseTo(0.25, 6);
    }
  });

  it('skips cells touching a masked node', () => {
    // Every cell of this 3x3 grid touches the masked centre node, so contouring it
    // yields nothing at all — the alternative would be drawing lines through a hole.
    expect(contourSegments(GRID, 'psi', 0.25)).toEqual([]);
  });

  it('still traces a level that lands exactly on node values', () => {
    // psi = y with the middle row exactly 0.5. The cells *below* it register no
    // crossing (both endpoints compare equal against the level), but the cells above
    // do, with the interpolation parameter landing at zero — so the contour still
    // comes out on the line y = 0.5 rather than disappearing.
    const segments = contourSegments(OPEN_GRID, 'psi', 0.5);
    expect(segments.length).toBeGreaterThan(0);
    for (const [a, b] of segments) {
      expect(a[1]).toBeCloseTo(0.5, 6);
      expect(b[1]).toBeCloseTo(0.5, 6);
    }
  });

  it('places mesh iso-lines at the right height', () => {
    const segments = contourSegments(MESH, 'psi', 0.25);
    expect(segments.length).toBeGreaterThan(0);
    for (const [a, b] of segments) {
      expect(a[1]).toBeCloseTo(0.25, 6);
      expect(b[1]).toBeCloseTo(0.25, 6);
    }
  });

  it('returns nothing for a level outside the field range', () => {
    expect(contourSegments(MESH, 'psi', 99)).toEqual([]);
  });

  it('spaces iso levels strictly inside the range', () => {
    const levels = isoLevels(0, 1, 3);
    expect(levels).toHaveLength(3);
    expect(Math.min(...levels)).toBeGreaterThan(0);
    expect(Math.max(...levels)).toBeLessThan(1);
  });
});

describe('<fs-viewer>', () => {
  function mount(attributes: Record<string, string> = {}): FieldViewerElement {
    const element = document.createElement('fs-viewer') as FieldViewerElement;
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    document.body.append(element);
    return element;
  }

  it('picks a default field when a result is set', () => {
    const element = mount();
    element.result = GRID;
    expect(element.field).toBe('psi');
    expect(element.fields.sort()).toEqual(['psi', 'speed']);
  });

  it('keeps the chosen field when a new result still has it', () => {
    const element = mount({ field: 'speed' });
    element.result = GRID;
    expect(element.field).toBe('speed');
  });

  it('falls back to the first field when the new result lacks the current one', () => {
    const element = mount({ field: 'speed' });
    element.result = MESH; // mesh only carries psi
    expect(element.field).toBe('psi');
  });

  it('exposes the mapped range, honouring the symmetric attribute', () => {
    const element = mount();
    element.result = GRID;
    expect(element.range).toEqual({ min: 0, max: 1 });

    element.setAttribute('symmetric', '');
    expect(element.range).toEqual({ min: -1, max: 1 });
  });

  it('probes through the element', () => {
    const element = mount();
    element.result = MESH;
    expect(element.probe([0.5, 0.25])).toBeCloseTo(0.25, 6);
  });

  it('describes itself for assistive technology', () => {
    const element = mount();
    element.result = MESH;
    element.draw(); // jsdom has no 2d context, but the label is set before that matters
    const label = element.shadowRoot!.querySelector('canvas')!.getAttribute('aria-label');
    expect(label).toContain('psi');
    expect(label).toContain('unstructured mesh');
  });

  it('reports no result rather than throwing when empty', () => {
    const element = mount();
    expect(element.range).toBeNull();
    expect(element.probe([0, 0])).toBeUndefined();
    expect(() => element.draw()).not.toThrow();
  });

  it('ignores an unknown colormap instead of rendering a blank field', () => {
    const element = mount({ colormap: 'not-a-map' });
    expect(element.colormap).toBe('viridis');
    element.setAttribute('colormap', 'plasma');
    expect(element.colormap).toBe('plasma');
  });
});
