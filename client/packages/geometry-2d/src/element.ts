/**
 * `<fs-geometry-2d>` — a parametric 2D profile editor as a custom element.
 *
 * Renders with **SVG, not canvas**. That is the load-bearing decision here: each control
 * point is a real DOM node, so it is focusable and keyboard-operable for free, hit-testing
 * is the browser's job rather than ours, the widget stays crisp at any zoom, and the whole
 * thing is testable in jsdom without a canvas implementation. Canvas is the right tool for
 * the *field* — see `@fenix-spoon/viewer` — but the wrong one for handles.
 *
 * ```html
 * <fs-geometry-2d bounds="-1,-1,2,1" mode="spline"></fs-geometry-2d>
 * <script type="module">
 *   import '@fenix-spoon/geometry-2d';
 *   const editor = document.querySelector('fs-geometry-2d');
 *   editor.value = { type: 'domain2d', bounds: [...], obstacle: {...} };
 *   editor.addEventListener('change', () => submit(editor.value));
 * </script>
 * ```
 *
 * Events: `input` fires continuously while dragging, `change` when an edit is committed
 * (pointer released, key pressed, undo). Bind expensive work — like submitting a solve —
 * to `change`.
 */

import { type Domain2D, validateGeometry } from '@fenix-spoon/client';

import { type Point, sampleClosedSpline, toPathData } from './spline.js';

export type EditorMode = 'polygon' | 'spline';

const DEFAULT_BOUNDS: [number, number, number, number] = [-1, -1, 2, 1];
const DEFAULT_POINTS: Point[] = [
  [0, 0],
  [0.12, 0.06],
  [0.35, 0.09],
  [0.65, 0.07],
  [1, 0.005],
  [0.65, -0.02],
  [0.35, -0.05],
  [0.12, -0.04],
];

const SVG_NS = 'http://www.w3.org/2000/svg';
const HISTORY_LIMIT = 100;

const STYLE = `
  :host { display: block; position: relative; touch-action: none; }
  svg { display: block; width: 100%; height: 100%; overflow: visible; }
  .outline { fill: var(--fs-outline-fill, rgba(20, 20, 24, 0.55));
             stroke: var(--fs-outline-stroke, #fff); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .hull { fill: none; stroke: var(--fs-hull-stroke, rgba(255,255,255,0.35));
          stroke-dasharray: 4 4; stroke-width: 1; vector-effect: non-scaling-stroke; }
  .handle { fill: var(--fs-handle-fill, #fff); stroke: var(--fs-handle-stroke, #3b82f6);
            stroke-width: 2; cursor: grab; vector-effect: non-scaling-stroke; }
  .handle:focus { outline: none; stroke: var(--fs-handle-focus, #f59e0b); stroke-width: 3; }
  .handle.dragging { cursor: grabbing; }
  .midpoint { fill: var(--fs-midpoint-fill, rgba(255,255,255,0.5)); cursor: copy; }
  .midpoint:hover { fill: var(--fs-handle-fill, #fff); }
`;

/** Serialised editor state, small enough to snapshot on every edit. */
interface State {
  points: Point[];
}

export class GeometryEditorElement extends HTMLElement {
  static observedAttributes = ['bounds', 'mode', 'samples', 'readonly', 'view-box'];

  #root: ShadowRoot;
  #svg!: SVGSVGElement;
  #outline!: SVGPathElement;
  #hull!: SVGPathElement;
  #handleLayer!: SVGGElement;
  #midpointLayer!: SVGGElement;

  #points: Point[] = DEFAULT_POINTS.map((p) => [...p] as Point);
  #bounds: [number, number, number, number] = [...DEFAULT_BOUNDS];
  #viewBox: [number, number, number, number] | null = null;
  #mode: EditorMode = 'polygon';
  #samples = 8;
  #readonly = false;

  #undo: State[] = [];
  #redo: State[] = [];
  #dragging: number | null = null;
  #dragMoved = false;

  constructor() {
    super();
    this.#root = this.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = STYLE;
    this.#root.append(style);
    this.#buildSvg();
  }

  // ------------------------------------------------------------------ public API

  /** The geometry as protocol JSON. Setting it replaces the editor's contents. */
  get value(): Domain2D {
    return {
      type: 'domain2d',
      bounds: [...this.#bounds],
      obstacle: { type: 'polygon2d', points: this.outlinePoints() },
    };
  }

  set value(geometry: Domain2D) {
    const parsed = validateGeometry(geometry);
    if (parsed.type !== 'domain2d') {
      throw new TypeError('<fs-geometry-2d> edits domain2d geometry');
    }
    this.#bounds = [...parsed.bounds];
    this.#points = parsed.obstacle.points.map((p) => [...p] as Point);
    this.#undo = [];
    this.#redo = [];
    this.#render();
  }

  /** Control points, in domain coordinates. In spline mode these are *not* the outline. */
  get controlPoints(): Point[] {
    return this.#points.map((p) => [...p] as Point);
  }

  set controlPoints(points: Point[]) {
    this.#commit(points.map((p) => [...p] as Point));
  }

  /**
   * The outline actually sent to the solver: the control points in polygon mode, the
   * sampled spline in spline mode.
   */
  outlinePoints(): Point[] {
    return this.#mode === 'spline'
      ? sampleClosedSpline(this.#points, this.#samples)
      : this.#points.map((p) => [...p] as Point);
  }

  get mode(): EditorMode {
    return this.#mode;
  }

  set mode(mode: EditorMode) {
    this.setAttribute('mode', mode);
  }

  get bounds(): [number, number, number, number] {
    return [...this.#bounds];
  }

  set bounds(bounds: [number, number, number, number]) {
    this.setAttribute('bounds', bounds.join(','));
  }

  /**
   * The part of the domain currently drawn — **display only**, and `null` to show all of it.
   *
   * This is not a second `bounds`. `bounds` is the protocol's domain rectangle and changing
   * it re-clamps the geometry; this changes nothing but the projection, so the same points
   * are edited at a different magnification. It exists so an editor layered over
   * `<fs-viewer>` can follow that viewer's zoom: listen for `fs-viewport-change` and assign
   * the viewport here, and a control point stays over the feature it was placed on.
   */
  get viewBox(): [number, number, number, number] | null {
    return this.#viewBox ? [...this.#viewBox] : null;
  }

  set viewBox(view: [number, number, number, number] | null) {
    if (view === null) this.removeAttribute('view-box');
    else this.setAttribute('view-box', view.join(','));
  }

  get readOnly(): boolean {
    return this.#readonly;
  }

  set readOnly(value: boolean) {
    this.toggleAttribute('readonly', value);
  }

  canUndo(): boolean {
    return this.#undo.length > 0;
  }

  canRedo(): boolean {
    return this.#redo.length > 0;
  }

  undo(): void {
    const previous = this.#undo.pop();
    if (!previous) return;
    this.#redo.push({ points: this.#points.map((p) => [...p] as Point) });
    this.#points = previous.points;
    this.#render();
    this.#emit('change');
  }

  redo(): void {
    const next = this.#redo.pop();
    if (!next) return;
    this.#undo.push({ points: this.#points.map((p) => [...p] as Point) });
    this.#points = next.points;
    this.#render();
    this.#emit('change');
  }

  /** Insert a point on the edge after `index`, at its midpoint. */
  insertPointAfter(index: number): void {
    const a = this.#points[index];
    const b = this.#points[(index + 1) % this.#points.length];
    if (!a || !b) return;
    const next = this.#points.map((p) => [...p] as Point);
    next.splice(index + 1, 0, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
    this.#commit(next);
  }

  /** Remove a point. Refuses below 3 points, which the protocol rejects anyway. */
  removePoint(index: number): boolean {
    if (this.#points.length <= 3) return false;
    const next = this.#points.map((p) => [...p] as Point);
    next.splice(index, 1);
    this.#commit(next);
    return true;
  }

  // ----------------------------------------------------------------- lifecycle

  connectedCallback(): void {
    if (!this.hasAttribute('tabindex')) this.setAttribute('tabindex', '0');
    this.#render();
  }

  attributeChangedCallback(name: string, _old: string | null, value: string | null): void {
    switch (name) {
      case 'bounds': {
        const parts = (value ?? '').split(',').map(Number);
        if (parts.length === 4 && parts.every(Number.isFinite) && parts[2]! > parts[0]!
            && parts[3]! > parts[1]!) {
          this.#bounds = parts as [number, number, number, number];
          // Points outside the new bounds would make `.value` fail protocol validation,
          // and history snapshots taken under the old bounds could resurrect them — so
          // re-clamp what we have and drop the history rather than keep it invalid.
          this.#points = this.#points.map((p) => this.#clamp(p));
          this.#undo = [];
          this.#redo = [];
        }
        break;
      }
      case 'view-box': {
        const parts = (value ?? '').split(',').map(Number);
        this.#viewBox =
          value !== null &&
          parts.length === 4 &&
          parts.every(Number.isFinite) &&
          parts[2]! > parts[0]! &&
          parts[3]! > parts[1]!
            ? (parts as [number, number, number, number])
            : null;
        break;
      }
      case 'mode':
        this.#mode = value === 'spline' ? 'spline' : 'polygon';
        break;
      case 'samples': {
        const samples = Number(value);
        if (Number.isFinite(samples) && samples >= 1) this.#samples = Math.floor(samples);
        break;
      }
      case 'readonly':
        this.#readonly = value !== null;
        break;
    }
    this.#render();
  }

  // ------------------------------------------------------------------ internals

  #buildSvg(): void {
    this.#svg = document.createElementNS(SVG_NS, 'svg');
    this.#svg.setAttribute('preserveAspectRatio', 'none');
    this.#svg.setAttribute('role', 'application');
    this.#svg.setAttribute('aria-label', 'Parametric 2D profile editor');

    this.#hull = document.createElementNS(SVG_NS, 'path');
    this.#hull.setAttribute('class', 'hull');
    this.#outline = document.createElementNS(SVG_NS, 'path');
    this.#outline.setAttribute('class', 'outline');
    this.#midpointLayer = document.createElementNS(SVG_NS, 'g');
    this.#handleLayer = document.createElementNS(SVG_NS, 'g');

    this.#svg.append(this.#outline, this.#hull, this.#midpointLayer, this.#handleLayer);
    this.#root.append(this.#svg);

    this.#svg.addEventListener('pointermove', this.#onPointerMove);
    this.#svg.addEventListener('pointerup', this.#onPointerUp);
    this.#svg.addEventListener('pointercancel', this.#onPointerUp);
  }

  /** What the SVG viewBox shows: the display view when one is set, else the whole domain. */
  #view(): [number, number, number, number] {
    return this.#viewBox ?? this.#bounds;
  }

  /**
   * Domain units are the SVG user space, flipped so +y is up.
   *
   * The flip is about the *view*, not the domain: with a display view set, mirroring about
   * the domain's centre would put every point outside the box being drawn.
   */
  #toPx = ([x, y]: Point): Point => {
    const [, ymin, , ymax] = this.#view();
    return [x, ymin + ymax - y];
  };

  #fromPx = ([x, y]: Point): Point => {
    const [, ymin, , ymax] = this.#view();
    return [x, ymin + ymax - y];
  };

  #clamp([x, y]: Point): Point {
    const [xmin, ymin, xmax, ymax] = this.#bounds;
    // The protocol requires points *strictly* inside the bounds, so keep a margin.
    const mx = (xmax - xmin) * 0.001;
    const my = (ymax - ymin) * 0.001;
    return [
      Math.min(xmax - mx, Math.max(xmin + mx, x)),
      Math.min(ymax - my, Math.max(ymin + my, y)),
    ];
  }

  #commit(points: Point[], emit: 'change' | 'input' | null = 'change'): void {
    this.#undo.push({ points: this.#points.map((p) => [...p] as Point) });
    if (this.#undo.length > HISTORY_LIMIT) this.#undo.shift();
    this.#redo = [];
    this.#points = points.map((p) => this.#clamp(p));
    this.#render();
    if (emit) this.#emit(emit);
  }

  #emit(type: 'input' | 'change'): void {
    this.dispatchEvent(new CustomEvent(type, { bubbles: true, composed: true }));
  }

  #render(): void {
    if (!this.#svg) return;
    const [xmin, ymin, xmax, ymax] = this.#view();
    this.#svg.setAttribute('viewBox', `${xmin} ${ymin} ${xmax - xmin} ${ymax - ymin}`);

    this.#outline.setAttribute('d', toPathData(this.outlinePoints(), this.#toPx));
    // In spline mode the control polygon is a separate, dashed guide.
    this.#hull.setAttribute(
      'd',
      this.#mode === 'spline' ? toPathData(this.#points, this.#toPx) : '',
    );

    this.#renderHandles();
    this.#renderMidpoints();
  }

  #renderHandles(): void {
    const layer = this.#handleLayer;
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    const radius = this.#handleRadius();

    this.#points.forEach((point, index) => {
      const [px, py] = this.#toPx(point);
      const handle = document.createElementNS(SVG_NS, 'circle');
      handle.setAttribute('class', 'handle');
      handle.setAttribute('cx', String(px));
      handle.setAttribute('cy', String(py));
      handle.setAttribute('r', String(radius));
      handle.dataset.index = String(index);
      if (!this.#readonly) {
        handle.setAttribute('tabindex', '0');
        // Not `slider`: that role promises aria-valuenow/min/max, and it is
        // one-dimensional — a control point moves in two. `button` matches what the
        // element actually is (an activatable thing), and the position lives in the
        // label, which is what a screen reader reads out anyway.
        handle.setAttribute('role', 'button');
        handle.setAttribute(
          'aria-label',
          `Control point ${index + 1} of ${this.#points.length}, ` +
            `x ${point[0].toFixed(3)}, y ${point[1].toFixed(3)}. ` +
            'Arrow keys move, Enter inserts, Delete removes.',
        );
        handle.addEventListener('pointerdown', this.#onPointerDown);
        handle.addEventListener('keydown', this.#onHandleKeyDown);
        handle.addEventListener('dblclick', this.#onHandleDoubleClick);
      }
      layer.append(handle);
    });
  }

  #renderMidpoints(): void {
    const layer = this.#midpointLayer;
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    if (this.#readonly) return;
    const radius = this.#handleRadius() * 0.55;

    this.#points.forEach((point, index) => {
      const next = this.#points[(index + 1) % this.#points.length]!;
      const [px, py] = this.#toPx([(point[0] + next[0]) / 2, (point[1] + next[1]) / 2]);
      const dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('class', 'midpoint');
      dot.setAttribute('cx', String(px));
      dot.setAttribute('cy', String(py));
      dot.setAttribute('r', String(radius));
      dot.dataset.edge = String(index);
      dot.setAttribute('role', 'button');
      dot.setAttribute('aria-label', `Insert a control point on edge ${index + 1}`);
      dot.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        event.stopPropagation();
        this.insertPointAfter(index);
      });
      layer.append(dot);
    });
  }

  /**
   * Handles are sized in domain units, so pick something proportional to what is *shown*.
   * Taking it from the domain instead would shrink every handle to a dot as a caller
   * zoomed the display view in, which is the opposite of what zooming in is for.
   */
  #handleRadius(): number {
    const [xmin, ymin, xmax, ymax] = this.#view();
    return Math.min(xmax - xmin, ymax - ymin) * 0.02;
  }

  #onPointerDown = (event: PointerEvent): void => {
    if (this.#readonly) return;
    const target = event.currentTarget as SVGCircleElement;
    const index = Number(target.dataset.index);
    if (!Number.isInteger(index)) return;
    event.preventDefault();
    this.#dragging = index;
    this.#dragMoved = false;
    // Snapshot before the drag so one drag is one undo step. The redo stack is *not*
    // cleared here: a click that never moves is not an edit, and wiping redo on
    // pointerdown would silently discard it before we know whether an edit happened.
    this.#undo.push({ points: this.#points.map((p) => [...p] as Point) });
    if (this.#undo.length > HISTORY_LIMIT) this.#undo.shift();
    target.classList.add('dragging');
    target.focus?.();
    this.#svg.setPointerCapture?.(event.pointerId);
  };

  #onPointerMove = (event: PointerEvent): void => {
    if (this.#dragging === null) return;
    const position = this.#svgPoint(event);
    if (!position) return;
    if (!this.#dragMoved) this.#redo = []; // the drag is now a real edit
    this.#dragMoved = true;
    this.#points[this.#dragging] = this.#clamp(this.#fromPx(position));
    this.#render();
    // Re-focusing keeps keyboard control on the point being dragged after the re-render.
    this.#focusHandle(this.#dragging);
    this.#emit('input');
  };

  #onPointerUp = (event: PointerEvent): void => {
    if (this.#dragging === null) return;
    this.#svg.releasePointerCapture?.(event.pointerId);
    const moved = this.#dragMoved;
    this.#dragging = null;
    this.#dragMoved = false;
    this.#handleLayer.querySelectorAll('.dragging').forEach((el) => el.classList.remove('dragging'));
    if (moved) this.#emit('change');
    else this.#undo.pop(); // a click that moved nothing is not an edit
  };

  #onHandleDoubleClick = (event: MouseEvent): void => {
    const index = Number((event.currentTarget as SVGCircleElement).dataset.index);
    if (!Number.isInteger(index)) return;
    event.preventDefault();
    this.removePoint(index);
  };

  #onHandleKeyDown = (event: KeyboardEvent): void => {
    if (this.#readonly) return;
    const index = Number((event.currentTarget as SVGCircleElement).dataset.index);
    if (!Number.isInteger(index)) return;

    const [xmin, ymin, xmax, ymax] = this.#bounds;
    // Shift = coarse (1%), plain = fine (0.2%) — arrow keys are the precise instrument.
    const scale = event.shiftKey ? 0.01 : 0.002;
    const dx = (xmax - xmin) * scale;
    const dy = (ymax - ymin) * scale;
    const point = this.#points[index]!;
    let moved: Point | null = null;

    switch (event.key) {
      case 'ArrowLeft': moved = [point[0] - dx, point[1]]; break;
      case 'ArrowRight': moved = [point[0] + dx, point[1]]; break;
      case 'ArrowUp': moved = [point[0], point[1] + dy]; break;
      case 'ArrowDown': moved = [point[0], point[1] - dy]; break;
      case 'Delete':
      case 'Backspace': {
        event.preventDefault();
        const removed = this.removePoint(index);
        if (removed) this.#focusHandle(Math.min(index, this.#points.length - 1));
        return;
      }
      case 'Enter':
      case '+': {
        event.preventDefault();
        this.insertPointAfter(index);
        this.#focusHandle(index + 1);
        return;
      }
      case 'z':
      case 'Z': {
        if (!(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        if (event.shiftKey) this.redo();
        else this.undo();
        this.#focusHandle(Math.min(index, this.#points.length - 1));
        return;
      }
      default:
        return;
    }

    event.preventDefault();
    const next = this.#points.map((p) => [...p] as Point);
    next[index] = moved;
    this.#commit(next);
    this.#focusHandle(index);
  };

  #focusHandle(index: number): void {
    const handle = this.#handleLayer.querySelector<SVGCircleElement>(
      `.handle[data-index="${index}"]`,
    );
    handle?.focus?.();
  }

  /** Pointer position in SVG user space (i.e. domain units, y still flipped). */
  #svgPoint(event: PointerEvent): Point | null {
    const rect = this.#svg.getBoundingClientRect?.();
    if (!rect || rect.width === 0 || rect.height === 0) {
      // jsdom and detached elements report a zero-sized box; fall back to the raw
      // coordinates so programmatic events in tests still move points.
      return [event.clientX, event.clientY];
    }
    const [xmin, ymin, xmax, ymax] = this.#view();
    return [
      xmin + ((event.clientX - rect.left) / rect.width) * (xmax - xmin),
      ymin + ((event.clientY - rect.top) / rect.height) * (ymax - ymin),
    ];
  }
}

export function defineGeometryEditor(tagName = 'fs-geometry-2d'): void {
  if (!customElements.get(tagName)) {
    customElements.define(tagName, GeometryEditorElement);
  }
}
