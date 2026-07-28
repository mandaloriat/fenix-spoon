import { beforeEach, describe, expect, it, vi } from 'vitest';

import { validatePolygon2D } from '@fenix-spoon/client';

import { GeometryEditorElement, defineGeometryEditor } from '../src/element.js';
import { sampleClosedSpline } from '../src/spline.js';

defineGeometryEditor();

function mount(attributes: Record<string, string> = {}): GeometryEditorElement {
  const element = document.createElement('fs-geometry-2d') as GeometryEditorElement;
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  document.body.append(element);
  return element;
}

function handles(element: GeometryEditorElement): SVGCircleElement[] {
  return [...element.shadowRoot!.querySelectorAll<SVGCircleElement>('.handle')];
}

function midpoints(element: GeometryEditorElement): SVGCircleElement[] {
  return [...element.shadowRoot!.querySelectorAll<SVGCircleElement>('.midpoint')];
}

function key(handle: SVGCircleElement, init: KeyboardEventInit): void {
  handle.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...init }));
}

describe('<fs-geometry-2d>', () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it('renders one focusable handle per control point', () => {
    const element = mount();
    expect(handles(element)).toHaveLength(element.controlPoints.length);
    for (const handle of handles(element)) {
      expect(handle.getAttribute('tabindex')).toBe('0');
      expect(handle.getAttribute('aria-label')).toMatch(/Control point \d+ of \d+/);
    }
  });

  it('exposes protocol geometry through .value', () => {
    const element = mount({ bounds: '-1,-1,2,1' });
    const value = element.value;
    expect(value.type).toBe('domain2d');
    expect(value.bounds).toEqual([-1, -1, 2, 1]);
    expect(value.obstacle.points.length).toBeGreaterThanOrEqual(3);
  });

  it('round-trips a geometry set through .value', () => {
    const element = mount();
    const geometry = {
      type: 'domain2d' as const,
      bounds: [0, 0, 1, 1] as [number, number, number, number],
      obstacle: {
        type: 'polygon2d' as const,
        points: [
          [0.2, 0.2],
          [0.8, 0.2],
          [0.5, 0.8],
        ] as [number, number][],
      },
    };
    element.value = geometry;
    expect(element.value.obstacle.points).toEqual(geometry.obstacle.points);
    expect(handles(element)).toHaveLength(3);
  });

  it('rejects geometry the protocol rejects', () => {
    const element = mount();
    expect(() => {
      element.value = {
        type: 'domain2d',
        bounds: [0, 0, 1, 1],
        obstacle: { type: 'polygon2d', points: [[0.5, 0.5], [0.6, 0.6]] as [number, number][] },
      };
    }).toThrow();
  });

  it('moves a point with the arrow keys and emits change', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const onChange = vi.fn();
    element.addEventListener('change', onChange);

    const before = element.controlPoints[0]!;
    key(handles(element)[0]!, { key: 'ArrowRight' });
    const after = element.controlPoints[0]!;

    expect(after[0]).toBeGreaterThan(before[0]);
    expect(after[1]).toBe(before[1]);
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('uses a coarser step with shift held', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const start = element.controlPoints[0]![0];
    key(handles(element)[0]!, { key: 'ArrowRight' });
    const fine = element.controlPoints[0]![0] - start;

    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    key(handles(element)[0]!, { key: 'ArrowRight', shiftKey: true });
    const coarse = element.controlPoints[0]![0] - start;

    expect(coarse).toBeGreaterThan(fine);
  });

  it('inserts a point with Enter and via the midpoint handles', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    key(handles(element)[0]!, { key: 'Enter' });
    expect(element.controlPoints).toHaveLength(4);
    // The new point sits at the midpoint of the edge it was inserted on.
    expect(element.controlPoints[1]).toEqual([0.5, 0.2]);

    midpoints(element)[0]!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    expect(element.controlPoints).toHaveLength(5);
  });

  it('removes a point with Delete but never below three', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
      [0.3, 0.6],
    ];
    key(handles(element)[0]!, { key: 'Delete' });
    expect(element.controlPoints).toHaveLength(3);

    // A triangle is the protocol's minimum, so further deletion must be refused.
    key(handles(element)[0]!, { key: 'Delete' });
    expect(element.controlPoints).toHaveLength(3);
    expect(element.removePoint(0)).toBe(false);
  });

  it('clamps points strictly inside the bounds', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [-5, -5],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const [x, y] = element.controlPoints[0]!;
    expect(x).toBeGreaterThan(0);
    expect(y).toBeGreaterThan(0);
    // Strictly inside means the protocol validator accepts the result.
    expect(() => element.value).not.toThrow();
  });

  it('undoes and redoes edits', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const original = element.controlPoints;
    key(handles(element)[0]!, { key: 'ArrowRight' });
    const moved = element.controlPoints;
    expect(moved).not.toEqual(original);

    expect(element.canUndo()).toBe(true);
    element.undo();
    expect(element.controlPoints).toEqual(original);

    expect(element.canRedo()).toBe(true);
    element.redo();
    expect(element.controlPoints).toEqual(moved);
  });

  it('undoes via Ctrl+Z on a focused handle', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const original = element.controlPoints;
    key(handles(element)[0]!, { key: 'ArrowRight' });
    key(handles(element)[0]!, { key: 'z', ctrlKey: true });
    expect(element.controlPoints).toEqual(original);
  });

  it('treats one drag as one undo step', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const original = element.controlPoints;
    const handle = handles(element)[0]!;

    handle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1 }));
    const svg = element.shadowRoot!.querySelector('svg')!;
    for (const clientX of [10, 20, 30]) {
      svg.dispatchEvent(
        new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX, clientY: 5 }),
      );
    }
    svg.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }));

    expect(element.controlPoints).not.toEqual(original);
    element.undo();
    expect(element.controlPoints).toEqual(original); // not three separate steps
  });

  it('emits input while dragging and change once on release', () => {
    const element = mount({ bounds: '0,0,1,1' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    const onInput = vi.fn();
    const onChange = vi.fn();
    element.addEventListener('input', onInput);
    element.addEventListener('change', onChange);

    const handle = handles(element)[0]!;
    const svg = element.shadowRoot!.querySelector('svg')!;
    handle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1 }));
    svg.dispatchEvent(
      new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: 10, clientY: 5 }),
    );
    svg.dispatchEvent(
      new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: 20, clientY: 5 }),
    );
    svg.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }));

    expect(onInput).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('does not record an undo step for a click that moved nothing', () => {
    const element = mount({ bounds: '0,0,1,1' });
    // `.value` loads a document and clears history; the `controlPoints` setter is an
    // edit and records one, so start from `.value` to get a clean slate.
    element.value = {
      type: 'domain2d',
      bounds: [0, 0, 1, 1],
      obstacle: {
        type: 'polygon2d',
        points: [
          [0.2, 0.2],
          [0.8, 0.2],
          [0.5, 0.8],
        ],
      },
    };
    expect(element.canUndo()).toBe(false);

    const handle = handles(element)[0]!;
    const svg = element.shadowRoot!.querySelector('svg')!;
    handle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1 }));
    svg.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }));
    expect(element.canUndo()).toBe(false);
  });

  it('ignores edits when readonly', () => {
    const element = mount({ bounds: '0,0,1,1', readonly: '' });
    element.setAttribute('readonly', '');
    const before = element.controlPoints;
    expect(midpoints(element)).toHaveLength(0);
    for (const handle of handles(element)) {
      expect(handle.hasAttribute('tabindex')).toBe(false);
      key(handle, { key: 'ArrowRight' });
    }
    expect(element.controlPoints).toEqual(before);
  });

  it('spline mode outlines more points than it has handles', () => {
    const element = mount({ bounds: '0,0,1,1', mode: 'spline', samples: '8' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    expect(handles(element)).toHaveLength(3);
    expect(element.outlinePoints()).toHaveLength(24);
    expect(element.value.obstacle.points).toHaveLength(24);
    // The dashed control hull is only drawn in spline mode.
    expect(element.shadowRoot!.querySelector('.hull')!.getAttribute('d')).not.toBe('');
  });

  it('polygon mode outlines exactly the control points', () => {
    const element = mount({ bounds: '0,0,1,1', mode: 'polygon' });
    element.controlPoints = [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.5, 0.8],
    ];
    expect(element.outlinePoints()).toEqual(element.controlPoints);
    expect(element.shadowRoot!.querySelector('.hull')!.getAttribute('d')).toBe('');
  });
});

describe('sampleClosedSpline', () => {
  it('passes through its control points', () => {
    const points: [number, number][] = [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ];
    const sampled = sampleClosedSpline(points, 4);
    for (const point of points) {
      expect(sampled.some((s) => Math.hypot(s[0] - point[0], s[1] - point[1]) < 1e-9)).toBe(true);
    }
  });

  it('produces perSegment samples per span', () => {
    const points: [number, number][] = [
      [0, 0],
      [1, 0],
      [0.5, 1],
    ];
    expect(sampleClosedSpline(points, 6)).toHaveLength(18);
  });

  it('survives coincident control points instead of dividing by zero', () => {
    const points: [number, number][] = [
      [0, 0],
      [0, 0],
      [1, 0],
      [0.5, 1],
    ];
    const sampled = sampleClosedSpline(points, 4);
    expect(sampled.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y))).toBe(true);
  });

  // Catmull-Rom interpolates through its control points with tangents, so it is *not*
  // hull-preserving — a square's corners bulge outside the hull by design. The property
  // that actually matters is that the sampled outline stays a simple polygon, because
  // the protocol rejects self-intersecting ones and the mesher can hang on them. So
  // assert exactly that, using the protocol's own validator.
  it.each([
    ['square', [[0, 0], [1, 0], [1, 1], [0, 1]]],
    ['cambered airfoil', [
      [0, 0], [0.12, 0.06], [0.35, 0.09], [0.65, 0.07],
      [1, 0.005], [0.65, -0.02], [0.35, -0.05], [0.12, -0.04],
    ]],
    ['uneven spacing', [[0, 0], [0.02, 0.01], [0.05, 0.2], [1, 0.4], [0.5, -0.3]]],
  ] as [string, [number, number][]][])(
    'samples %s into a simple (non-self-intersecting) polygon',
    (_name, points) => {
      const sampled = sampleClosedSpline(points, 10);
      expect(() =>
        validatePolygon2D({ type: 'polygon2d', points: sampled }),
      ).not.toThrow();
    },
  );

  it('overshoots only modestly on a convex outline', () => {
    // Bounded overshoot is what centripetal parameterisation buys over uniform; this
    // pins it so a change of alpha can't silently balloon the outline.
    const square: [number, number][] = [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ];
    for (const [x, y] of sampleClosedSpline(square, 10)) {
      expect(x).toBeGreaterThanOrEqual(-0.2);
      expect(x).toBeLessThanOrEqual(1.2);
      expect(y).toBeGreaterThanOrEqual(-0.2);
      expect(y).toBeLessThanOrEqual(1.2);
    }
  });
});
