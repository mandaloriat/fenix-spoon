import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Domain2D, Grid2DResult, Mesh2DResult } from '@fenix-spoon/client';

import { type FieldViewerElement, defineFieldViewer } from '../src/element.js';

defineFieldViewer();

/** A 9x9 grid on [0, 8]^2 with a rightward vector field and a linear scalar. */
function grid(options: { vectors?: boolean; bounds?: [number, number, number, number] } = {}) {
  const n = 9;
  const fields: number[] = [];
  const vectors: [number, number][] = [];
  for (let iy = 0; iy < n; iy += 1) {
    for (let ix = 0; ix < n; ix += 1) {
      fields.push(ix + iy);
      vectors.push([1, 0]);
    }
  }
  const result: Grid2DResult = {
    job_id: 'j-grid',
    kind: 'grid2d',
    data: {
      bounds: options.bounds ?? [0, 0, 8, 8],
      shape: [n, n],
      fields: { psi: fields },
      mask: Array(n * n).fill(0),
    },
    stats: {},
    artifacts: [],
  };
  if (options.vectors !== false) result.data.vector_fields = { velocity: vectors };
  return result;
}

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

const GEOMETRY: Domain2D = {
  type: 'domain2d',
  bounds: [0, 0, 8, 8],
  obstacle: {
    type: 'polygon2d',
    points: [
      [3, 3.5],
      [5, 4],
      [3, 4.5],
    ],
  },
};

const SIZE = { width: 640, height: 320 };

/** jsdom lays nothing out, so the element is told how big it is. */
function stubRect(node: Element, size: { width: number; height: number }): void {
  node.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: size.width,
      bottom: size.height,
      width: size.width,
      height: size.height,
      toJSON: () => ({}),
    }) as DOMRect;
}

function mount(attributes: Record<string, string> = {}): FieldViewerElement {
  const element = document.createElement('fs-viewer') as FieldViewerElement;
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  document.body.append(element);
  stubRect(element, SIZE);
  stubRect(element.shadowRoot!.querySelector('canvas')!, SIZE);
  return element;
}

/** jsdom has no `PointerEvent`; the listeners only need the type and the coordinates. */
function pointer(type: string, x: number, y: number, pointerId = 1): MouseEvent {
  const event = new MouseEvent(type, { clientX: x, clientY: y, bubbles: true });
  Object.defineProperty(event, 'pointerId', { value: pointerId });
  return event;
}

function canvasOf(element: FieldViewerElement): HTMLCanvasElement {
  return element.shadowRoot!.querySelector('canvas')!;
}

beforeEach(() => {
  document.body.replaceChildren();
});

describe('viewport lifecycle', () => {
  it('starts framing the whole domain and reports it', () => {
    const element = mount();
    expect(element.viewport).toBeNull(); // nothing to frame yet
    element.result = grid();
    expect(element.viewport).toEqual([0, 0, 8, 8]);
    expect(element.zoom).toBeCloseTo(1);
  });

  it('emits on every view change, naming what caused it', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();
    const seen: { viewport: number[]; reason: string }[] = [];
    element.addEventListener('fs-viewport-change', (event) => {
      seen.push((event as CustomEvent).detail);
    });
    element.zoomBy(0.5);
    element.panBy(10, 0);
    element.resetView();
    expect(seen.map((e) => e.reason)).toEqual(['zoom', 'pan', 'reset']);
    expect(seen.at(-1)!.viewport).toEqual([0, 0, 8, 8]);
  });

  it('sets and restores a viewport, ignoring one that is not a rectangle', () => {
    const element = mount();
    element.result = grid();
    element.viewport = [2, 2, 4, 4];
    expect(element.viewport).toEqual([2, 2, 4, 4]);
    element.viewport = [4, 4, 2, 2] as [number, number, number, number];
    expect(element.viewport).toEqual([2, 2, 4, 4]); // unchanged
    element.viewport = null;
    expect(element.viewport).toEqual([0, 0, 8, 8]);
  });

  it('keeps the frame across a re-solve of the same domain', () => {
    // The point of an auto-run loop is watching one region change while you edit. A
    // viewer that snapped back to the whole domain on every solve would make that useless.
    const element = mount();
    element.result = grid();
    element.viewport = [2, 2, 4, 4];
    element.result = grid();
    expect(element.viewport).toEqual([2, 2, 4, 4]);
  });

  it('drops the frame when the new result is of a different domain', () => {
    const element = mount();
    element.result = grid();
    element.viewport = [2, 2, 4, 4];
    element.result = grid({ bounds: [-10, -10, 10, 10] });
    expect(element.viewport).toEqual([-10, -10, 10, 10]);
  });
});

describe('zoom and pan gestures', () => {
  it('zooms about the pointer, leaving the domain point under it fixed', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();
    const at: [number, number] = [500, 60];
    const before = element.toDomain(at);
    canvasOf(element).dispatchEvent(
      new WheelEvent('wheel', { deltaY: -120, clientX: at[0], clientY: at[1], bubbles: true, cancelable: true }),
    );
    expect(element.zoom).toBeGreaterThan(1);
    const after = element.toDomain(at);
    expect(after[0]).toBeCloseTo(before[0], 9);
    expect(after[1]).toBeCloseTo(before[1], 9);
  });

  it('zooms out on the other wheel direction', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();
    element.viewport = [3, 3, 5, 5];
    const before = element.zoom;
    canvasOf(element).dispatchEvent(
      new WheelEvent('wheel', { deltaY: 120, clientX: 320, clientY: 160, bubbles: true, cancelable: true }),
    );
    expect(element.zoom).toBeLessThan(before);
  });

  it('leaves the wheel alone when the element is not interactive', () => {
    // The back-compatibility guarantee: a page that embedded this before navigation
    // existed must still scroll when the pointer happens to be over the field.
    const element = mount({ colorbar: 'off' });
    element.result = grid();
    const event = new WheelEvent('wheel', {
      deltaY: -120,
      clientX: 100,
      clientY: 100,
      bubbles: true,
      cancelable: true,
    });
    canvasOf(element).dispatchEvent(event);
    expect(element.zoom).toBeCloseTo(1);
    expect(event.defaultPrevented).toBe(false);
  });

  it('pans with a drag in pan mode', () => {
    const element = mount({ interactive: '', mode: 'pan', colorbar: 'off' });
    element.result = grid();
    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 320, 160));
    canvas.dispatchEvent(pointer('pointermove', 384, 160)); // a tenth of the width, right
    canvas.dispatchEvent(pointer('pointerup', 384, 160));
    // The window moved left by a tenth of its width, so the content followed the finger.
    expect(element.viewport![0]).toBeCloseTo(-0.8, 6);
    expect(element.viewport![2]).toBeCloseTo(7.2, 6);
  });

  it('does not pan in probe mode — the mode is what settles who owns the drag', () => {
    // This is the contract that lets a page layer `<fs-geometry-2d>` over the viewer: two
    // widgets both want a drag, and "whichever is on top" is not something to build on.
    const element = mount({ interactive: '', mode: 'probe', colorbar: 'off' });
    element.result = grid();
    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 320, 160));
    canvas.dispatchEvent(pointer('pointermove', 384, 160));
    canvas.dispatchEvent(pointer('pointerup', 384, 160));
    expect(element.viewport).toEqual([0, 0, 8, 8]);
  });

  it('claims no pointer gesture at all in `none` mode', () => {
    // What an application sets while an overlaid geometry editor owns the surface. A
    // viewer that kept the wheel, or the pinch, would make that promise conditional.
    const element = mount({ interactive: '', mode: 'none', colorbar: 'off' });
    element.result = grid();
    const canvas = canvasOf(element);
    const wheelEvent = new WheelEvent('wheel', {
      deltaY: -120,
      clientX: 320,
      clientY: 160,
      bubbles: true,
      cancelable: true,
    });
    canvas.dispatchEvent(wheelEvent);
    canvas.dispatchEvent(pointer('pointerdown', 300, 160, 1));
    canvas.dispatchEvent(pointer('pointerdown', 340, 160, 2));
    canvas.dispatchEvent(pointer('pointermove', 260, 160, 1));
    canvas.dispatchEvent(pointer('pointermove', 380, 160, 2));
    expect(element.viewport).toEqual([0, 0, 8, 8]);
    expect(wheelEvent.defaultPrevented).toBe(false);
    // The keyboard still works, because focus is an unambiguous statement of intent.
    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(element.viewport![0]).toBeGreaterThan(0);
  });

  it('zooms on a two-finger pinch in every mode that claims gestures', () => {
    const element = mount({ interactive: '', mode: 'probe', colorbar: 'off' });
    element.result = grid();
    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 300, 160, 1));
    canvas.dispatchEvent(pointer('pointerdown', 340, 160, 2));
    canvas.dispatchEvent(pointer('pointermove', 260, 160, 1));
    canvas.dispatchEvent(pointer('pointermove', 380, 160, 2));
    expect(element.zoom).toBeGreaterThan(1);
  });

  it('cannot be zoomed or flung past the guard rails', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();
    for (let i = 0; i < 400; i += 1) element.zoomBy(0.5);
    expect(element.zoom).toBeLessThanOrEqual(1e4 + 1);
    for (let i = 0; i < 400; i += 1) element.panBy(500, 0);
    const [xmin, , xmax] = element.viewport!;
    expect((xmin + xmax) / 2).toBeLessThanOrEqual(8);
  });
});

describe('keyboard operation', () => {
  it('pans, zooms and resets from the keyboard', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();

    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(element.viewport![0]).toBeGreaterThan(0);

    element.dispatchEvent(new KeyboardEvent('keydown', { key: '+', bubbles: true }));
    expect(element.zoom).toBeGreaterThan(1);

    element.dispatchEvent(new KeyboardEvent('keydown', { key: '0', bubbles: true }));
    expect(element.viewport).toEqual([0, 0, 8, 8]);
  });

  it('pans up and down the right way round', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();
    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }));
    // ArrowUp looks further up the domain, so the window's y increases.
    expect(element.viewport![1]).toBeGreaterThan(0);
  });

  it('claims only the keys it uses', () => {
    const element = mount({ interactive: '' });
    element.result = grid();
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    element.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(false);
    const arrow = new KeyboardEvent('keydown', {
      key: 'ArrowRight',
      bubbles: true,
      cancelable: true,
    });
    element.dispatchEvent(arrow);
    expect(arrow.defaultPrevented).toBe(true);
  });

  it('ignores the keyboard entirely when not interactive', () => {
    const element = mount();
    element.result = grid();
    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(element.viewport).toEqual([0, 0, 8, 8]);
  });

  it('fits the geometry on `g`, and does nothing when there is none', () => {
    const element = mount({ interactive: '', colorbar: 'off' });
    element.result = grid();
    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true }));
    expect(element.viewport).toEqual([0, 0, 8, 8]);
    element.geometry = GEOMETRY;
    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true }));
    expect(element.viewport![0]).toBeGreaterThan(2);
    expect(element.viewport![2]).toBeLessThan(6);
  });
});

describe('accessibility', () => {
  it('becomes focusable and self-describing only when it is interactive', () => {
    const element = mount();
    expect(element.hasAttribute('tabindex')).toBe(false);
    element.setAttribute('interactive', '');
    expect(element.getAttribute('tabindex')).toBe('0');
    expect(element.getAttribute('role')).toBe('application');
    expect(element.getAttribute('aria-label')).toMatch(/Arrow keys/);
    element.removeAttribute('interactive');
    expect(element.hasAttribute('tabindex')).toBe(false);
  });

  it('describes the data, the scale and the zoom on the canvas', () => {
    const element = mount({ interactive: '', 'range-min': '0', 'range-max': '10' });
    element.result = MESH;
    element.viewport = [0, 0, 0.5, 0.5];
    element.draw();
    const label = canvasOf(element).getAttribute('aria-label')!;
    expect(label).toContain('psi');
    expect(label).toContain('unstructured mesh');
    expect(label).toContain('fixed colour scale');
    expect(label).toMatch(/zoomed/);
    expect(label).toMatch(/probe mode/);
  });

  it('announces the probe readout politely', () => {
    const element = mount();
    element.result = grid();
    const readout = element.shadowRoot!.querySelector('.readout')!;
    expect(readout.getAttribute('aria-live')).toBe('polite');
    canvasOf(element).dispatchEvent(pointer('pointermove', 100, 100));
    expect((readout as HTMLElement).hidden).toBe(false);
    expect(readout.textContent).toContain('psi =');
  });
});

describe('probing and sections', () => {
  it('emits a probe with coordinates and a value, and a null one on leave', () => {
    const element = mount({ colorbar: 'off' });
    element.result = grid();
    const seen: { at: number[] | null; value: number | null }[] = [];
    element.addEventListener('fs-probe', (event) => seen.push((event as CustomEvent).detail));

    canvasOf(element).dispatchEvent(pointer('pointermove', 0, 320)); // bottom-left corner
    expect(seen[0]!.at![0]).toBeCloseTo(0, 6);
    expect(seen[0]!.at![1]).toBeCloseTo(0, 6);
    expect(seen[0]!.value).toBe(0);

    canvasOf(element).dispatchEvent(new MouseEvent('pointerleave', { bubbles: true }));
    expect(seen.at(-1)!.value).toBeNull();
    expect(seen.at(-1)!.at).toBeNull();
  });

  it('samples a section from data already in the page', () => {
    const element = mount();
    element.result = grid();
    const section = element.sampleSection([0, 0], [8, 0], { samples: 5 })!;
    expect(section.value).toEqual([0, 2, 4, 6, 8]);
    expect(section.distance.at(-1)).toBeCloseTo(8);
    expect(section.skipped).toBe(0);
  });

  it('drags a section and reports it on commit', () => {
    const element = mount({ interactive: '', mode: 'section', colorbar: 'off' });
    element.result = grid();
    const seen: { phase: string; section: { value: number[] } | null }[] = [];
    element.addEventListener('fs-section', (event) => seen.push((event as CustomEvent).detail));

    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 0, 320));
    canvas.dispatchEvent(pointer('pointermove', 640, 320));
    canvas.dispatchEvent(pointer('pointerup', 640, 320));

    expect(seen.map((e) => e.phase)).toEqual(['preview', 'commit']);
    expect(seen[0]!.section).toBeNull(); // a preview costs nothing
    expect(seen[1]!.section!.value.length).toBeGreaterThan(2);
    expect(element.section!.start[0]).toBeCloseTo(0, 6);
    expect(element.section!.end[0]).toBeCloseTo(8, 6);

    element.clearSection();
    expect(element.section).toBeNull();
  });

  it('abandons a section when a second finger turns the gesture into a pinch', () => {
    // Found in review of #77. A pinch begun in section mode used to leave the degenerate
    // line from the first touch in place, so the pointerup at the end of the zoom
    // committed it — and the second finger coming up committed it again. Two `fs-section`
    // events for a gesture that was a zoom, each carrying a zero-length line.
    const element = mount({ interactive: '', mode: 'section', colorbar: 'off' });
    element.result = grid();
    const seen: { phase: string }[] = [];
    element.addEventListener('fs-section', (event) => seen.push((event as CustomEvent).detail));

    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 300, 160, 1));
    canvas.dispatchEvent(pointer('pointerdown', 340, 160, 2));
    canvas.dispatchEvent(pointer('pointermove', 260, 160, 1));
    canvas.dispatchEvent(pointer('pointermove', 380, 160, 2));
    canvas.dispatchEvent(pointer('pointerup', 260, 160, 1));
    canvas.dispatchEvent(pointer('pointerup', 380, 160, 2));

    expect(seen).toEqual([]);
    expect(element.section).toBeNull();
    expect(element.zoom).toBeGreaterThan(1); // it really was a zoom
  });

  it('puts back a section the pinch interrupted, rather than destroying it', () => {
    const element = mount({ interactive: '', mode: 'section', colorbar: 'off' });
    element.result = grid();
    element.section = { start: [1, 1], end: [7, 7] };

    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 300, 160, 1));
    canvas.dispatchEvent(pointer('pointerdown', 340, 160, 2));
    canvas.dispatchEvent(pointer('pointerup', 300, 160, 1));
    canvas.dispatchEvent(pointer('pointerup', 340, 160, 2));

    expect(element.section).toEqual({ start: [1, 1], end: [7, 7] });
  });

  it('does not commit a section the system cancelled', () => {
    const element = mount({ interactive: '', mode: 'section', colorbar: 'off' });
    element.result = grid();
    const seen: unknown[] = [];
    element.addEventListener('fs-section', (event) => {
      if ((event as CustomEvent).detail.phase === 'commit') seen.push(event);
    });

    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 0, 320));
    canvas.dispatchEvent(pointer('pointermove', 320, 320));
    canvas.dispatchEvent(pointer('pointercancel', 320, 320));

    expect(seen).toEqual([]);
    expect(element.section).toBeNull();
  });

  it('commits once per drag, and a later hover reads out instead of redrawing the line', () => {
    const element = mount({ interactive: '', mode: 'section', colorbar: 'off' });
    element.result = grid();
    const commits: { end: number[] }[] = [];
    element.addEventListener('fs-section', (event) => {
      const detail = (event as CustomEvent).detail;
      if (detail.phase === 'commit') commits.push(detail);
    });

    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 0, 320));
    canvas.dispatchEvent(pointer('pointermove', 320, 320));
    canvas.dispatchEvent(pointer('pointerup', 320, 320));
    expect(commits).toHaveLength(1);

    // Hovering afterwards must not drag the committed line's end around with the pointer.
    const before = element.section!.end;
    canvas.dispatchEvent(pointer('pointermove', 600, 100));
    expect(element.section!.end).toEqual(before);
    expect(commits).toHaveLength(1);
    expect((element.shadowRoot!.querySelector('.readout') as HTMLElement).hidden).toBe(false);
  });

  it('has no section tool when the result carries no scalar field', () => {
    const element = mount();
    expect(element.capabilities.section.available).toBe(false);
    expect(element.capabilities.section.reason).toMatch(/No result/);
  });
});

describe('streamline controls', () => {
  it('seeds a curve on a click in seed mode, without submitting anything', () => {
    const element = mount({ interactive: '', mode: 'seed', streamlines: 'velocity', colorbar: 'off' });
    element.result = grid();
    const seen: { at: number[] }[] = [];
    element.addEventListener('fs-seed', (event) => seen.push((event as CustomEvent).detail));

    canvasOf(element).dispatchEvent(pointer('pointerdown', 320, 160));
    expect(seen).toHaveLength(1);
    expect(element.streamlineSeeds).toHaveLength(1);
    expect(element.integralCurves).toHaveLength(1);

    element.clearStreamlineSeeds();
    expect(element.streamlineSeeds).toBeNull();
  });

  it('re-integrates when the density changes, and clamps a silly one', () => {
    const element = mount({ streamlines: 'velocity', 'streamline-density': '6' });
    element.result = grid();
    const sparse = element.integralCurves.length;
    expect(sparse).toBeGreaterThan(0);
    element.streamlineDensity = 24;
    expect(element.integralCurves.length).toBeGreaterThan(sparse);
    element.streamlineDensity = 1e9;
    expect(element.streamlineDensity).toBe(64);
  });

  it('explains itself when the result carries no vector field', () => {
    const element = mount({ streamlines: 'velocity' });
    element.result = grid({ vectors: false });
    const capability = element.capabilities.streamlines;
    expect(capability.available).toBe(false);
    expect(capability.reason).toMatch(/no vector field/i);
    expect(element.integralCurves).toEqual([]);
  });

  it('refuses a scalar field by name', () => {
    const element = mount({ streamlines: 'psi' });
    element.result = grid();
    expect(element.capabilities.streamlines.available).toBe(false);
    expect(element.capabilities.streamlines.reason).toMatch(/scalar field/);
  });
});

describe('colour scale', () => {
  it('pins the range when both ends are set, and reports which mode it is in', () => {
    const element = mount();
    element.result = grid();
    expect(element.scale).toBe('auto');
    expect(element.range).toEqual({ min: 0, max: 16 });

    element.range = { min: -5, max: 5 };
    expect(element.scale).toBe('manual');
    expect(element.range).toEqual({ min: -5, max: 5 });
    // The data's own range stays available, which is what a "reset scale" button needs.
    expect(element.autoRange).toEqual({ min: 0, max: 16 });

    element.range = null;
    expect(element.scale).toBe('auto');
    expect(element.range).toEqual({ min: 0, max: 16 });
  });

  it('ignores half a range, and an inverted one', () => {
    const element = mount({ 'range-min': '2' });
    element.result = grid();
    expect(element.scale).toBe('auto');
    element.setAttribute('range-max', '1');
    expect(element.scale).toBe('auto');
    element.setAttribute('range-max', '3');
    expect(element.scale).toBe('manual');
  });

  it('lets two viewers share one scale, which is the point', () => {
    const a = mount();
    const b = mount();
    a.result = grid();
    b.result = grid({ bounds: [0, 0, 4, 4] });
    const shared = { min: 0, max: 20 };
    a.range = shared;
    b.range = shared;
    expect(a.range).toEqual(b.range);
  });
});

describe('geometry, labels and overlays', () => {
  it('cannot fit a geometry it was not given, and says so', () => {
    const element = mount();
    element.result = grid();
    expect(element.fitGeometry()).toBe(false);
    const capability = element.capabilities.fitGeometry;
    expect(capability.available).toBe(false);
    expect(capability.reason).toMatch(/No geometry has been supplied/);
  });

  it('frames the supplied geometry with a margin', () => {
    const element = mount();
    element.result = grid();
    element.geometry = GEOMETRY;
    expect(element.capabilities.fitGeometry.available).toBe(true);
    expect(element.fitGeometry(0)).toBe(true);
    expect(element.viewport).toEqual([3, 3.5, 5, 4.5]);
  });

  it('captions the colourbar with the field name and its unit', () => {
    const element = mount();
    element.result = grid();
    element.fieldUnits = { psi: 'Wb/m' };
    element.fieldLabels = { psi: 'A_z' };
    element.draw();
    expect(canvasOf(element).getAttribute('aria-label')).toContain('Wb/m');
    // The readout follows the same per-field units rather than one global attribute.
    canvasOf(element).dispatchEvent(pointer('pointermove', 100, 100));
    const readout = element.shadowRoot!.querySelector('.readout')!;
    expect(readout.textContent).toContain('A_z');
    expect(readout.textContent).toContain('Wb/m');
  });

  it('runs an application overlay with both transforms', () => {
    const element = mount({ colorbar: 'off' });
    element.result = grid();
    const painter = vi.fn();
    element.overlay = painter;
    element.draw();
    // jsdom has no 2-D context, so `draw` stops before overlays. The contract that can be
    // checked here is that the property round-trips and the transforms agree.
    expect(element.overlay).toBe(painter);
    const screen = element.toScreen([4, 4]);
    expect(screen[0]).toBeCloseTo(320, 6);
    expect(screen[1]).toBeCloseTo(160, 6);
    const back = element.toDomain(screen);
    expect(back[0]).toBeCloseTo(4, 6);
    expect(back[1]).toBeCloseTo(4, 6);
  });
});

describe('the optional toolbar', () => {
  it('shows nothing unless asked', () => {
    const element = mount();
    expect(element.tools).toEqual([]);
    expect((element.shadowRoot!.querySelector('.toolbar') as HTMLElement).hidden).toBe(true);
  });

  it('renders the requested tools, in a labelled toolbar', () => {
    const element = mount({ toolbar: 'pan,probe,fit-geometry' });
    element.result = grid();
    const toolbar = element.shadowRoot!.querySelector('.toolbar')!;
    expect(toolbar.getAttribute('role')).toBe('toolbar');
    expect([...toolbar.querySelectorAll('button')].map((b) => b.dataset.tool)).toEqual([
      'pan',
      'probe',
      'fit-geometry',
    ]);
  });

  it('ignores a tool it does not know rather than rendering a dead button', () => {
    const element = mount({ toolbar: 'pan,teleport' });
    expect(element.tools).toEqual(['pan']);
  });

  it('disables a tool the data cannot support, with the reason in the tooltip', () => {
    const element = mount({ toolbar: 'fit-geometry' });
    element.result = grid();
    const button = element.shadowRoot!.querySelector<HTMLButtonElement>('[data-tool=fit-geometry]')!;
    expect(button.disabled).toBe(true);
    expect(button.title).toMatch(/No geometry has been supplied/);
    element.geometry = GEOMETRY;
    expect(button.disabled).toBe(false);
  });

  it('marks the active mode as pressed and switches it on a click', () => {
    const element = mount({ toolbar: 'pan,probe', interactive: '' });
    element.result = grid();
    const pan = element.shadowRoot!.querySelector<HTMLButtonElement>('[data-tool=pan]')!;
    const probe = element.shadowRoot!.querySelector<HTMLButtonElement>('[data-tool=probe]')!;
    expect(probe.getAttribute('aria-pressed')).toBe('true');
    const modes: string[] = [];
    element.addEventListener('fs-mode-change', (event) =>
      modes.push((event as CustomEvent).detail.mode),
    );
    pan.click();
    expect(element.mode).toBe('pan');
    expect(pan.getAttribute('aria-pressed')).toBe('true');
    expect(probe.getAttribute('aria-pressed')).toBe('false');
    expect(modes).toEqual(['pan']);
  });

  it('falls back to a sensible default set for a bare attribute', () => {
    const element = mount({ toolbar: '' });
    expect(element.tools).toEqual(['pan', 'probe', 'fit-domain', 'reset']);
  });

  it('emits the exported image rather than downloading it itself', () => {
    const element = mount({ toolbar: 'export' });
    element.result = grid();
    const seen: unknown[] = [];
    element.addEventListener('fs-export', (event) => seen.push((event as CustomEvent).detail));
    element.shadowRoot!.querySelector<HTMLButtonElement>('[data-tool=export]')!.click();
    expect(seen).toHaveLength(1);
  });
});

describe('backwards compatibility', () => {
  it('is inert by default: no modes wired, no gestures claimed, same probe readout', () => {
    const element = mount();
    element.result = grid();
    expect(element.interactive).toBe(false);
    expect(element.mode).toBe('probe');
    expect(element.hasAttribute('tabindex')).toBe(false);
    expect(element.scale).toBe('auto');
    expect(element.tools).toEqual([]);
    expect(element.geometry).toBeNull();
    expect(element.streamlines).toBeNull();

    const canvas = canvasOf(element);
    canvas.dispatchEvent(pointer('pointerdown', 100, 100));
    canvas.dispatchEvent(pointer('pointermove', 300, 100));
    canvas.dispatchEvent(pointer('pointerup', 300, 100));
    expect(element.viewport).toEqual([0, 0, 8, 8]);
    expect((element.shadowRoot!.querySelector('.readout') as HTMLElement).hidden).toBe(false);
  });

  it('still draws a result from a pre-1.1 server, with the new tools declared unavailable', () => {
    const element = mount();
    const old = grid({ vectors: false });
    element.result = old;
    expect(element.fields).toEqual(['psi']);
    expect(element.vectorFields).toEqual([]);
    expect(element.capabilities.vectors.available).toBe(false);
    expect(element.capabilities.probe.available).toBe(true);
    expect(element.probe([0, 0])).toBe(0);
  });
});
