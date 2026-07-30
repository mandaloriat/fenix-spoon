/**
 * `<fs-viewer>` — renders and *explores* a Fenix Spoon `grid2d` or `mesh2d` result.
 *
 * Canvas here, deliberately, and note that this is the *opposite* call from
 * `@fenix-spoon/geometry-2d`, which uses SVG. The rule is what the pixels are for: a
 * handful of interactive handles want to be DOM nodes (focusable, hit-tested by the
 * browser); thousands of coloured triangles per frame want a raster surface.
 *
 * All the decisions that don't need a drawing context — colormaps, ranges, contour
 * extraction, probing, viewport arithmetic, curve integration — live in `colormap.ts`,
 * `field.ts`, `viewport.ts` and `streamlines.ts` as pure functions, so they are unit-tested
 * directly and this file stays a thin painter with an input layer.
 *
 * ```html
 * <fs-viewer field="speed" colormap="viridis" contours="10" interactive mode="pan"></fs-viewer>
 * <script type="module">
 *   import '@fenix-spoon/viewer';
 *   document.querySelector('fs-viewer').result = await job.wait();
 * </script>
 * ```
 *
 * **Navigation is opt-in.** Without the `interactive` attribute this element behaves
 * exactly as it always has: it draws, and it reads out a value on hover. That is not
 * timidity about the new features — it is that a page which layers a geometry editor over
 * the viewer, as `examples/airfoil-2d/` does, must be the one deciding which of the two
 * owns a drag. A viewer that grabbed the gesture on upgrade would break that page silently.
 * The programmatic API (`fitDomain`, `setViewport`, `probe`, …) is always available.
 */

import {
  type Bounds2D,
  type FieldResult,
  type Geometry,
  type JobResult,
  isFieldResult,
} from '@fenix-spoon/client';

import {
  type ColormapName,
  type Range,
  colorbarTicks,
  fieldRange,
  isColormapName,
  normalise,
  padRange,
  sampleColormap,
  symmetricRange,
} from './colormap.js';
import {
  type Point,
  type Section,
  MAX_GLYPHS_ACROSS,
  contourSegments,
  isoLevels,
  probe,
  glyphSamples,
  resultFieldNames,
  resultFieldValues,
  resultVectorFieldNames,
  resultMask,
  sampleAt,
  sampleSection,
} from './field.js';
import {
  type IntegralCurve,
  MAX_STREAMLINE_DENSITY,
  integralCurves,
  streamlineAvailability,
} from './streamlines.js';
import {
  type PlotSize,
  clampViewport,
  fitViewport,
  isValidViewport,
  panViewport,
  sameViewport,
  toDomain,
  toScreen,
  zoomLevel,
  zoomViewport,
} from './viewport.js';

/**
 * What a pointer gesture does. Explicit, because a viewer with a geometry editor on top of
 * it has two things that both want a drag, and "whichever one is on top" is not a contract
 * an application can build on.
 */
export type ViewerMode = 'pan' | 'probe' | 'section' | 'seed' | 'none';

const MODES: readonly ViewerMode[] = ['pan', 'probe', 'section', 'seed', 'none'];

/** Whether one tool can be offered, and if not, a sentence saying why. */
export interface ToolAvailability {
  available: boolean;
  reason?: string;
}

/**
 * What this viewer can currently do with the result it holds.
 *
 * Built for driving a toolbar: every entry is either usable or carries the sentence to put
 * in a tooltip. A page that hides a disabled button leaves the user wondering; one that
 * shows it with "this result carries no vector field" has answered the question.
 */
export interface ViewerCapabilities {
  pan: ToolAvailability;
  probe: ToolAvailability;
  section: ToolAvailability;
  vectors: ToolAvailability;
  streamlines: ToolAvailability;
  fitGeometry: ToolAvailability;
  export: ToolAvailability;
}

/** What an application overlay is handed. Everything it needs, nothing it can break. */
export interface OverlayContext {
  ctx: CanvasRenderingContext2D;
  /** Domain -> CSS pixels within the plot area. */
  toScreen(point: Point): Point;
  /** CSS pixels within the plot area -> domain. */
  toDomain(point: Point): Point;
  viewport: Bounds2D;
  plot: PlotSize;
  result: FieldResult | null;
}

const STYLE = `
  :host { display: block; position: relative; background: var(--fs-viewer-bg, #1e1e22); }
  :host([interactive]) { touch-action: none; }
  :host(:focus-visible) { outline: 2px solid var(--fs-viewer-focus, #f59e0b); outline-offset: 2px; }
  canvas { display: block; width: 100%; height: 100%; touch-action: none; }
  .readout {
    position: absolute; top: 0.5rem; left: 0.5rem; padding: 0.25rem 0.5rem;
    font: 12px/1.4 ui-monospace, monospace; border-radius: 4px; pointer-events: none;
    background: var(--fs-viewer-readout-bg, rgba(0,0,0,0.6));
    color: var(--fs-viewer-readout-fg, #fff);
  }
  .readout[hidden] { display: none; }
  .toolbar {
    position: absolute; top: 0.5rem; right: 0.5rem; display: flex; gap: 0.25rem;
    flex-wrap: wrap; justify-content: flex-end; max-width: calc(100% - 1rem);
  }
  .toolbar[hidden] { display: none; }
  .toolbar button {
    font: 11px/1 ui-monospace, monospace; padding: 0.35rem 0.5rem; cursor: pointer;
    border-radius: 4px; border: 1px solid var(--fs-viewer-tool-border, rgba(255,255,255,0.25));
    background: var(--fs-viewer-tool-bg, rgba(0,0,0,0.55));
    color: var(--fs-viewer-tool-fg, #fff);
    transition: background-color 120ms ease;
  }
  .toolbar button[aria-pressed="true"] {
    background: var(--fs-viewer-tool-active-bg, rgba(59,130,246,0.85));
  }
  .toolbar button[disabled] { opacity: 0.4; cursor: not-allowed; }
  .toolbar button:focus-visible { outline: 2px solid var(--fs-viewer-focus, #f59e0b); }
  /* The view itself never animates — a zoom or a fit is applied on the frame it is asked
     for, so there is no motion to reduce there. This covers the only thing that moves. */
  @media (prefers-reduced-motion: reduce) {
    .toolbar button { transition: none; }
  }
`;

const COLORBAR_WIDTH = 14;
const COLORBAR_MARGIN = 12;
const MASKED: [number, number, number] = [30, 30, 34];

/** Wheel notches are not a distance; this is the zoom per notch, tuned by feel. */
const WHEEL_ZOOM = 1.15;
/** Keyboard zoom step, coarser than the wheel because it is one keypress per step. */
const KEY_ZOOM = 1.25;
/** Keyboard pan, as a fraction of the visible span. */
const KEY_PAN = 0.12;

/** Tools the optional built-in toolbar knows how to render, in display order. */
const TOOLBAR_TOOLS = [
  'pan',
  'probe',
  'section',
  'seed',
  'zoom-in',
  'zoom-out',
  'fit-domain',
  'fit-geometry',
  'reset',
  'export',
] as const;
export type ToolbarTool = (typeof TOOLBAR_TOOLS)[number];

const TOOLBAR_DEFAULT: ToolbarTool[] = ['pan', 'probe', 'fit-domain', 'reset'];

const TOOL_LABELS: Record<ToolbarTool, { label: string; title: string }> = {
  pan: { label: '✥', title: 'Pan mode — drag to move the view' },
  probe: { label: '⌖', title: 'Probe mode — read the field under the pointer' },
  section: { label: '⁄', title: 'Section mode — drag a line to sample along it' },
  seed: { label: '✱', title: 'Seed mode — click to start an integral curve' },
  'zoom-in': { label: '+', title: 'Zoom in' },
  'zoom-out': { label: '−', title: 'Zoom out' },
  'fit-domain': { label: '⤢', title: 'Fit the whole domain' },
  'fit-geometry': { label: '◧', title: 'Fit the geometry' },
  reset: { label: '⟲', title: 'Reset the view' },
  export: { label: '⤓', title: 'Export the current view as a PNG' },
};

/** The coloured field, rasterised once per (field, colormap, range) and blitted thereafter. */
interface Raster {
  key: string;
  surface: unknown;
  width: number;
  height: number;
}

const MODE_TOOLS: Partial<Record<ToolbarTool, ViewerMode>> = {
  pan: 'pan',
  probe: 'probe',
  section: 'section',
  seed: 'seed',
};

export class FieldViewerElement extends HTMLElement {
  static observedAttributes = [
    'field', 'colormap', 'contours', 'colorbar', 'symmetric', 'units', 'vectors', 'glyphs',
    'glyph-scale', 'mode', 'interactive', 'range-min', 'range-max', 'streamlines',
    'streamline-density', 'toolbar',
  ];

  #root: ShadowRoot;
  #canvas: HTMLCanvasElement;
  #readout: HTMLDivElement;
  #toolbarEl: HTMLDivElement;

  #result: FieldResult | null = null;
  #field: string | null = null;
  #colormap: ColormapName = 'viridis';
  #contours = 0;
  #showColorbar = true;
  #symmetric = false;
  #units = '';
  #fieldUnits: Record<string, string> | null = null;
  #fieldLabels: Record<string, string> | null = null;
  #vectors: string | null = null;
  #glyphs = 24;
  #glyphScale = 1;
  #frame = 0;

  #viewport: Bounds2D | null = null;
  #mode: ViewerMode = 'probe';
  #interactive = false;
  #geometry: Geometry | null = null;
  #showGeometry = true;
  #manualRange: Range | null = null;
  #streamlines: string | null = null;
  #streamlineDensity = 18;
  #streamlineSeeds: Point[] | null = null;
  #overlay: ((context: OverlayContext) => void) | null = null;
  #tools: ToolbarTool[] = [];
  #section: { start: Point; end: Point } | null = null;

  // Gesture bookkeeping. `#pointers` carries every active pointer so a pinch can be told
  // from a drag without a second event pathway.
  #pointers = new Map<number, Point>();
  #dragFrom: Point | null = null;
  #pinchDistance = 0;

  // Derived-data caches. Every one of them is keyed by the inputs it was computed from, so
  // a pan re-projects instead of recomputing, which is the difference between a viewer that
  // stays at 60 fps on a 500k-node mesh and one that does not.
  #raster: Raster | null = null;
  #contourCache: { key: string; segments: [Point, Point][] } | null = null;
  #glyphCache: { key: string; glyphs: ReturnType<typeof glyphSamples> } | null = null;
  #curveCache: { key: string; curves: IntegralCurve[] } | null = null;
  #canvasSize = { width: 0, height: 0, dpr: 0 };

  constructor() {
    super();
    this.#root = this.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = STYLE;
    this.#canvas = document.createElement('canvas');
    this.#canvas.setAttribute('role', 'img');
    this.#readout = document.createElement('div');
    this.#readout.className = 'readout';
    this.#readout.hidden = true;
    // A probe readout is information, not decoration: announce it politely so a value read
    // by pointer is also available to someone who is not using one.
    this.#readout.setAttribute('aria-live', 'polite');
    this.#toolbarEl = document.createElement('div');
    this.#toolbarEl.className = 'toolbar';
    this.#toolbarEl.hidden = true;
    this.#toolbarEl.setAttribute('role', 'toolbar');
    this.#toolbarEl.setAttribute('aria-label', 'Field viewer tools');
    this.#root.append(style, this.#canvas, this.#readout, this.#toolbarEl);

    this.#canvas.addEventListener('pointermove', this.#onPointerMove);
    this.#canvas.addEventListener('pointerleave', this.#onPointerLeave);
    this.#canvas.addEventListener('pointerdown', this.#onPointerDown);
    this.#canvas.addEventListener('pointerup', this.#onPointerUp);
    this.#canvas.addEventListener('pointercancel', this.#onPointerUp);
    this.#canvas.addEventListener('wheel', this.#onWheel, { passive: false });
    this.addEventListener('keydown', this.#onKeyDown);
  }

  // ------------------------------------------------------------------ public API

  /**
   * The result to display. Setting it re-renders.
   *
   * Accepts any `JobResult` but only *stores* a field one. A `series1d` result (protocol 1.5)
   * is curves, and this widget draws a coloured 2-D domain: it has no bounds to fit, no
   * topology to interpolate over and no field to contour. Handing one over clears the view and
   * says so, rather than throwing or drawing a one-pixel picture of a curve — the axes, legend
   * and inverted-y convention a `C_p` plot needs belong to a separate widget.
   */
  get result(): FieldResult | null {
    // Deliberately narrower than the setter accepts. The setter takes any `JobResult`,
    // because a custom-element property is reachable from untyped JS and plain HTML; by the
    // time a value is stored it is known to be a field result, and saying so here saves every
    // reader re-narrowing a union the setter has already made impossible.
    return this.#result;
  }

  set result(result: JobResult | null) {
    if (result && !isFieldResult(result)) {
      console.warn(
        `<fs-viewer> draws 2-D fields; a "${result.kind}" result carries curves. ` +
          'Read them with `resultSeries(result)` and plot them separately.',
      );
      this.#result = null;
      this.#field = null;
      this.#invalidate();
      this.render();
      return;
    }
    const previous = this.#result;
    this.#result = result;
    // Keep the current field if the new result still has it; otherwise fall back to
    // the first one, so swapping solvers doesn't blank the view.
    if (result && (!this.#field || !resultFieldNames(result).includes(this.#field))) {
      this.#field = resultFieldNames(result)[0] ?? null;
    }
    // Keep the frame across a re-solve of the same domain — the whole point of an
    // auto-run loop is watching one region change — but do not carry a viewport onto a
    // result whose bounds it has nothing to do with.
    if (
      this.#viewport &&
      (!result || !previous || !sameViewport(previous.data.bounds, result.data.bounds))
    ) {
      this.#viewport = null;
    }
    this.#invalidate();
    this.#syncToolbar();
    this.render();
  }

  /** Which scalar field to draw. */
  get field(): string | null {
    return this.#field;
  }

  set field(field: string | null) {
    if (field === null) this.removeAttribute('field');
    else this.setAttribute('field', field);
  }

  /** Field names available in the current result. */
  get fields(): string[] {
    return this.#result ? resultFieldNames(this.#result) : [];
  }

  /** Vector fields in the current result — empty against a pre-1.1 server. */
  get vectorFields(): string[] {
    return this.#result ? resultVectorFieldNames(this.#result) : [];
  }

  get vectors(): string | null {
    return this.#vectors;
  }

  set vectors(name: string | null) {
    // Via the attribute, like `field`: one path into `attributeChangedCallback`, so the
    // property and the attribute can never disagree about what is being drawn.
    if (name === null || name === '') this.removeAttribute('vectors');
    else this.setAttribute('vectors', name);
  }

  get colormap(): ColormapName {
    return this.#colormap;
  }

  set colormap(name: ColormapName) {
    this.setAttribute('colormap', name);
  }

  /**
   * The scalar range currently mapped to the colormap.
   *
   * Setting it **pins** the scale, which is what comparing two solves side by side needs:
   * an auto-scaled pair of pictures uses two different colour meanings and invites exactly
   * the wrong conclusion. Set `null` to go back to auto.
   */
  get range(): Range | null {
    return this.#computeRange();
  }

  set range(range: Range | null) {
    if (range === null) {
      this.removeAttribute('range-min');
      this.removeAttribute('range-max');
      return;
    }
    this.setAttribute('range-min', String(range.min));
    this.setAttribute('range-max', String(range.max));
  }

  /** Whether the colour scale follows the data or has been pinned. */
  get scale(): 'auto' | 'manual' {
    return this.#manualRange ? 'manual' : 'auto';
  }

  /** The range the data would produce, ignoring any manual pin. */
  get autoRange(): Range | null {
    return this.#autoRange();
  }

  /**
   * Units per field name, for the colourbar caption and the probe readout.
   *
   * The wire carries units for curves and not for fields — a field is coloured, and this
   * element has always taken its caption from an attribute the page sets. That is fine with
   * one field and wrong the moment a picker switches between `T` in °C and `flux` in W/m²,
   * which is why this map exists beside the scalar `units` attribute. It is the
   * application's to fill: it can read the units straight out of
   * `GET /capabilities/{name}?sections=metrics`.
   */
  get fieldUnits(): Record<string, string> | null {
    return this.#fieldUnits;
  }

  set fieldUnits(units: Record<string, string> | null) {
    this.#fieldUnits = units;
    this.render();
  }

  /** Display names per field, when the wire name is not what a reader should see. */
  get fieldLabels(): Record<string, string> | null {
    return this.#fieldLabels;
  }

  set fieldLabels(labels: Record<string, string> | null) {
    this.#fieldLabels = labels;
    this.render();
  }

  /**
   * The geometry this result was solved for, when the application has it.
   *
   * **Not read from the result**, because it is not in one: a result carries arrays and
   * bounds, and reconstructing an outline from a `grid2d` mask would be a guess that is
   * right for a `domain2d` obstacle and wrong for a `regions2d` heat sink, where the mask
   * marks the fluid rather than the solid. The page that submitted the job has the exact
   * geometry, so it supplies it — and that is what "fit geometry" fits and what the
   * outline overlay draws.
   */
  get geometry(): Geometry | null {
    return this.#geometry;
  }

  set geometry(geometry: Geometry | null) {
    this.#geometry = geometry;
    this.#syncToolbar();
    this.render();
  }

  /** Whether to outline the supplied geometry over the field. Default true. */
  get showGeometry(): boolean {
    return this.#showGeometry;
  }

  set showGeometry(show: boolean) {
    this.#showGeometry = show;
    this.render();
  }

  /** The visible part of the domain, in domain units. Null before a result arrives. */
  get viewport(): Bounds2D | null {
    return this.#result ? [...this.#view()] : null;
  }

  set viewport(view: Bounds2D | null) {
    this.setViewport(view);
  }

  /** How far in the view is zoomed, as a multiple of the domain span. */
  get zoom(): number {
    return this.#result ? zoomLevel(this.#view(), this.#result.data.bounds) : 1;
  }

  /**
   * What a pointer gesture does. `probe` is the default because it is what this element
   * did before there were modes, and a mode that changes on upgrade is not a default.
   */
  get mode(): ViewerMode {
    return this.#mode;
  }

  set mode(mode: ViewerMode) {
    this.setAttribute('mode', mode);
  }

  /** Whether wheel, drag, pinch and keyboard navigation are wired up. */
  get interactive(): boolean {
    return this.#interactive;
  }

  set interactive(value: boolean) {
    this.toggleAttribute('interactive', value);
  }

  /** Which vector field the integral-curve overlay integrates, or null for none. */
  get streamlines(): string | null {
    return this.#streamlines;
  }

  set streamlines(name: string | null) {
    if (name === null || name === '') this.removeAttribute('streamlines');
    else this.setAttribute('streamlines', name);
  }

  /** Seed lattice columns for the curve overlay. Clamped to a safe ceiling. */
  get streamlineDensity(): number {
    return this.#streamlineDensity;
  }

  set streamlineDensity(density: number) {
    this.setAttribute('streamline-density', String(density));
  }

  /**
   * Explicit seed points for the curve overlay, replacing the lattice.
   *
   * Changing these re-integrates from data already in the page. No job is submitted, and
   * none needs to be: an integral curve is a property of the array the solver already sent.
   */
  get streamlineSeeds(): Point[] | null {
    return this.#streamlineSeeds ? this.#streamlineSeeds.map((p) => [...p] as Point) : null;
  }

  set streamlineSeeds(seeds: readonly Point[] | null) {
    this.#streamlineSeeds = seeds ? seeds.map((p) => [...p] as Point) : null;
    this.#curveCache = null;
    this.render();
  }

  /** Add one seed to the curve overlay — what `mode="seed"` does on a click. */
  addStreamlineSeed(at: Point): void {
    this.#streamlineSeeds = [...(this.#streamlineSeeds ?? []), [at[0], at[1]]];
    this.#curveCache = null;
    this.render();
  }

  clearStreamlineSeeds(): void {
    this.#streamlineSeeds = null;
    this.#curveCache = null;
    this.render();
  }

  /** The curves currently drawn, in domain coordinates. Empty when the overlay is off. */
  get integralCurves(): IntegralCurve[] {
    return this.#curves();
  }

  /** Arrow length multiplier, on top of the magnitude scaling. */
  get glyphScale(): number {
    return this.#glyphScale;
  }

  set glyphScale(scale: number) {
    this.setAttribute('glyph-scale', String(scale));
  }

  /** Arrow lattice columns. Clamped to a safe ceiling. */
  get glyphs(): number {
    return this.#glyphs;
  }

  set glyphs(across: number) {
    this.setAttribute('glyphs', String(across));
  }

  /**
   * An application-supplied painter, run last so it draws over the field.
   *
   * This is how a page adds what only it knows about — a resultant arrow, a probe pin, a
   * label on a feature — without this package growing a vocabulary for each. It gets the
   * context and both transforms, so an overlay is written in domain coordinates and
   * survives every zoom and pan for free.
   */
  get overlay(): ((context: OverlayContext) => void) | null {
    return this.#overlay;
  }

  set overlay(painter: ((context: OverlayContext) => void) | null) {
    this.#overlay = painter;
    this.render();
  }

  /** Which built-in toolbar buttons to show. Empty — the default — shows no toolbar. */
  get tools(): ToolbarTool[] {
    return [...this.#tools];
  }

  set tools(tools: readonly ToolbarTool[]) {
    this.setAttribute('toolbar', tools.join(','));
  }

  /** What is possible with the current result, and why anything else is not. */
  get capabilities(): ViewerCapabilities {
    const noResult = { available: false, reason: 'No result is loaded.' };
    if (!this.#result) {
      return {
        pan: noResult,
        probe: noResult,
        section: noResult,
        vectors: noResult,
        streamlines: noResult,
        fitGeometry: noResult,
        export: { available: this.#canExport(), reason: this.#canExport() ? undefined : 'This environment has no canvas export.' },
      };
    }
    const hasField = this.#field !== null && resultFieldValues(this.#result, this.#field) != null;
    const fieldReason = hasField ? undefined : 'This result carries no scalar field to sample.';
    const vectorFields = this.vectorFields;
    const curves = streamlineAvailability(this.#result, this.#streamlines ?? undefined);
    return {
      pan: { available: true },
      probe: { available: hasField, reason: fieldReason },
      section: { available: hasField, reason: fieldReason },
      vectors: vectorFields.length
        ? { available: true }
        : {
            available: false,
            reason: 'This result carries no vector field; the solver did not send one.',
          },
      streamlines: { available: curves.available, reason: curves.reason },
      fitGeometry: this.#geometryBounds()
        ? { available: true }
        : {
            available: false,
            reason:
              'No geometry has been supplied. A result carries arrays and bounds, not an ' +
              'outline — set `viewer.geometry` to the geometry the job was submitted with.',
          },
      export: {
        available: this.#canExport(),
        reason: this.#canExport() ? undefined : 'This environment has no canvas export.',
      },
    };
  }

  // ------------------------------------------------------------- view operations

  /** Frame the whole domain. The reset target, and what a fresh result starts at. */
  fitDomain(): void {
    if (!this.#result) return;
    this.#applyViewport([...this.#result.data.bounds], 'fit');
  }

  /**
   * Frame the supplied geometry. Does nothing — and says why through `capabilities` —
   * when there is no geometry to frame.
   */
  fitGeometry(padding = 0.15): boolean {
    const bounds = this.#geometryBounds();
    if (!bounds) return false;
    this.#applyViewport(fitViewport(bounds, padding), 'fit');
    return true;
  }

  /** Back to the whole domain. */
  resetView(): void {
    if (!this.#result) return;
    this.#viewport = null;
    this.#invalidateView();
    this.#emit('fs-viewport-change', { viewport: [...this.#view()], reason: 'reset' });
    this.render();
  }

  /** Set the visible domain window. `null` restores the domain fit. */
  setViewport(view: Bounds2D | null, reason = 'set'): void {
    if (view === null) {
      this.resetView();
      return;
    }
    if (!isValidViewport(view)) return;
    this.#applyViewport([...view] as Bounds2D, reason);
  }

  /**
   * Zoom about a point. `factor < 1` moves in. `at` is in **domain** coordinates and
   * defaults to the centre of the view, so a caller need not think in pixels.
   */
  zoomBy(factor: number, at?: Point): void {
    if (!this.#result) return;
    const plot = this.#plot();
    const anchor = at ? toScreen(this.#view(), plot, at) : [plot.width / 2, plot.height / 2];
    this.#applyViewport(
      clampViewport(
        zoomViewport(this.#view(), plot, anchor as Point, factor),
        this.#result.data.bounds,
      ),
      'zoom',
    );
  }

  /** Pan by a screen-space delta in CSS pixels. */
  panBy(dxPx: number, dyPx: number): void {
    if (!this.#result) return;
    this.#applyViewport(
      clampViewport(
        panViewport(this.#view(), this.#plot(), dxPx, dyPx),
        this.#result.data.bounds,
      ),
      'pan',
    );
  }

  /** Domain -> CSS pixels within the plot area. */
  toScreen(point: Point): Point {
    return toScreen(this.#view(), this.#plot(), point);
  }

  /** CSS pixels within the plot area -> domain. */
  toDomain(point: Point): Point {
    return toDomain(this.#view(), this.#plot(), point);
  }

  // ------------------------------------------------------------------- sampling

  /**
   * Sample the displayed field at a domain position, nearest-node on a grid.
   *
   * This is the probe readout's value, unchanged since the first release. {@link sample}
   * is the interpolating one, which matches what the server's `at_point` query returns.
   */
  probe(at: Point): number | undefined {
    if (!this.#result || !this.#field) return undefined;
    return probe(this.#result, this.#field, at);
  }

  /** Interpolated sample of the displayed field — bilinear on a grid, barycentric on a mesh. */
  sample(at: Point, field: string | null = this.#field): number | undefined {
    if (!this.#result || !field) return undefined;
    return sampleAt(this.#result, field, at);
  }

  /**
   * Sample a field along a line already in the page. Costs no job and no round trip.
   *
   * The shape is the server's `section` query result, key for key, so a page can move
   * between the two without rewriting its plotting code.
   */
  sampleSection(
    start: Point,
    end: Point,
    options: { samples?: number; field?: string } = {},
  ): Section | undefined {
    const field = options.field ?? this.#field;
    if (!this.#result || !field) return undefined;
    return sampleSection(this.#result, field, start, end, options.samples);
  }

  /** The section line currently drawn, if any. */
  get section(): { start: Point; end: Point } | null {
    return this.#section
      ? { start: [...this.#section.start] as Point, end: [...this.#section.end] as Point }
      : null;
  }

  set section(line: { start: Point; end: Point } | null) {
    this.#section = line
      ? { start: [...line.start] as Point, end: [...line.end] as Point }
      : null;
    this.render();
  }

  clearSection(): void {
    this.section = null;
  }

  /** The current view as a PNG data URL, or `''` where canvas export is unavailable. */
  toDataURL(type = 'image/png'): string {
    return this.#canvas.toDataURL?.(type) ?? '';
  }

  // ----------------------------------------------------------------- lifecycle

  connectedCallback(): void {
    this.#syncFocusability();
    this.#syncToolbar();
    this.render();
  }

  attributeChangedCallback(name: string, _old: string | null, value: string | null): void {
    switch (name) {
      case 'field':
        this.#field = value;
        this.#raster = null;
        this.#contourCache = null;
        break;
      case 'colormap':
        if (value && isColormapName(value)) this.#colormap = value;
        this.#raster = null;
        break;
      case 'vectors':
        this.#vectors = value || null;
        this.#glyphCache = null;
        break;
      case 'glyphs': {
        const across = Number(value);
        this.#glyphs =
          Number.isFinite(across) && across > 0
            ? Math.min(MAX_GLYPHS_ACROSS, Math.floor(across))
            : 24;
        this.#glyphCache = null;
        break;
      }
      case 'glyph-scale': {
        const scale = Number(value);
        // Clamped rather than trusted: an arrow twenty times its lattice cell is a
        // scribble, and the cap is what keeps a slider bound to this attribute honest.
        this.#glyphScale = Number.isFinite(scale) && scale > 0 ? Math.min(8, scale) : 1;
        break;
      }
      case 'contours': {
        const count = Number(value);
        this.#contours = Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
        this.#contourCache = null;
        break;
      }
      case 'colorbar':
        this.#showColorbar = value !== 'off';
        break;
      case 'symmetric':
        this.#symmetric = value !== null;
        this.#raster = null;
        break;
      case 'units':
        this.#units = value ?? '';
        break;
      case 'mode': {
        const mode = (value ?? 'probe') as ViewerMode;
        const next = MODES.includes(mode) ? mode : 'probe';
        if (next !== this.#mode) {
          this.#mode = next;
          this.#emit('fs-mode-change', { mode: next });
        }
        break;
      }
      case 'interactive':
        this.#interactive = value !== null;
        this.#syncFocusability();
        break;
      case 'range-min':
      case 'range-max': {
        const min = Number(this.getAttribute('range-min'));
        const max = Number(this.getAttribute('range-max'));
        this.#manualRange =
          this.hasAttribute('range-min') &&
          this.hasAttribute('range-max') &&
          Number.isFinite(min) &&
          Number.isFinite(max) &&
          max > min
            ? { min, max }
            : null;
        this.#raster = null;
        break;
      }
      case 'streamlines':
        this.#streamlines = value || null;
        this.#curveCache = null;
        break;
      case 'streamline-density': {
        const density = Number(value);
        this.#streamlineDensity =
          Number.isFinite(density) && density > 0
            ? Math.min(MAX_STREAMLINE_DENSITY, Math.floor(density))
            : 18;
        this.#curveCache = null;
        break;
      }
      case 'toolbar': {
        this.#tools =
          value === null
            ? []
            : value.trim() === ''
              ? [...TOOLBAR_DEFAULT]
              : (value
                  .split(',')
                  .map((tool) => tool.trim())
                  .filter((tool): tool is ToolbarTool =>
                    (TOOLBAR_TOOLS as readonly string[]).includes(tool),
                  ) as ToolbarTool[]);
        this.#buildToolbar();
        break;
      }
    }
    // Once, rather than in the four cases that happen to affect a tool: `field` changes
    // what can be probed, `mode` changes what is pressed, and getting the list wrong is
    // a button that lies. It returns immediately when there is no toolbar.
    this.#syncToolbar();
    this.render();
  }

  // ------------------------------------------------------------------ rendering

  /** Coalesce bursts of property writes into one paint. */
  render(): void {
    if (this.#frame) return;
    const schedule =
      typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame
        : (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0) as unknown as number;
    this.#frame = schedule(() => {
      this.#frame = 0;
      this.draw();
    });
  }

  /** Paint immediately. `render()` is the debounced entry point; this is the work. */
  draw(): void {
    // The accessible description states what the data *is*, so it must not depend on
    // having a rendering context — a screen-reader user gets it either way.
    this.#canvas.setAttribute('aria-label', this.#describe());

    const ctx = this.#canvas.getContext?.('2d');
    if (!ctx) return; // no canvas implementation (jsdom) — the logic is tested directly

    const { width, height } = this.#resizeCanvas();
    ctx.clearRect(0, 0, width, height);
    if (!this.#result || !this.#field) return;

    const values = resultFieldValues(this.#result, this.#field);
    if (!values) return;
    const range = this.#computeRange();
    if (!range) return;

    const plot = this.#plot(width, height);
    const view = this.#view();

    ctx.save();
    // Everything below is clipped to the plot area, so a zoomed-in field cannot paint
    // over the colourbar it is supposed to be read against.
    ctx.beginPath();
    ctx.rect(0, 0, plot.width, plot.height);
    ctx.clip();

    if (this.#result.kind === 'grid2d') this.#drawGrid(ctx, values, range, view, plot);
    else this.#drawMesh(ctx, values, range, view, plot);

    if (this.#contours > 0) this.#drawContours(ctx, range, view, plot);
    if (this.#streamlines) this.#drawCurves(ctx, view, plot);
    if (this.#vectors) this.#drawGlyphs(ctx, view, plot);
    if (this.#geometry && this.#showGeometry) this.#drawGeometry(ctx, view, plot);
    if (this.#section) this.#drawSection(ctx, view, plot);
    if (this.#overlay) {
      this.#overlay({
        ctx,
        toScreen: (point) => toScreen(view, plot, point),
        toDomain: (point) => toDomain(view, plot, point),
        viewport: [...view],
        plot,
        result: this.#result,
      });
    }
    ctx.restore();

    if (this.#showColorbar) this.#drawColorbar(ctx, range, width, height);
  }

  #resizeCanvas(): { width: number; height: number } {
    const dpr = globalThis.devicePixelRatio ?? 1;
    const rect = this.getBoundingClientRect?.();
    const cssWidth = Math.max(1, Math.round(rect?.width || this.#canvas.width || 300));
    const cssHeight = Math.max(1, Math.round(rect?.height || this.#canvas.height || 150));
    const ctx = this.#canvas.getContext?.('2d');
    // Assigning `canvas.width` reallocates the backing store and resets the transform, so
    // doing it unconditionally made every frame of a pan throw away a buffer it was about
    // to refill. Only touch it when the size actually changed.
    if (
      this.#canvasSize.width !== cssWidth ||
      this.#canvasSize.height !== cssHeight ||
      this.#canvasSize.dpr !== dpr
    ) {
      this.#canvas.width = Math.round(cssWidth * dpr);
      this.#canvas.height = Math.round(cssHeight * dpr);
      this.#canvasSize = { width: cssWidth, height: cssHeight, dpr };
    }
    ctx?.setTransform?.(dpr, 0, 0, dpr, 0, 0);
    return { width: cssWidth, height: cssHeight };
  }

  /** The drawing area the field occupies, i.e. everything but the colourbar gutter. */
  #plot(width?: number, height?: number): PlotSize {
    let cssWidth = width;
    let cssHeight = height;
    if (cssWidth === undefined || cssHeight === undefined) {
      const rect = this.getBoundingClientRect?.();
      cssWidth = Math.max(1, Math.round(rect?.width || this.#canvasSize.width || 300));
      cssHeight = Math.max(1, Math.round(rect?.height || this.#canvasSize.height || 150));
    }
    return {
      width: this.#showColorbar
        ? Math.max(1, cssWidth - COLORBAR_WIDTH - COLORBAR_MARGIN * 2)
        : cssWidth,
      height: cssHeight,
    };
  }

  /** The current viewport, defaulting to the whole domain. */
  #view(): Bounds2D {
    if (this.#viewport) return this.#viewport;
    return this.#result ? this.#result.data.bounds : [0, 0, 1, 1];
  }

  #applyViewport(view: Bounds2D, reason: string): void {
    const previous = this.#view();
    this.#viewport = view;
    if (sameViewport(previous, view)) return;
    this.#invalidateView();
    this.#emit('fs-viewport-change', { viewport: [...view], reason });
    this.render();
  }

  /** Caches that survive a pan but not a change of data. */
  #invalidate(): void {
    this.#raster = null;
    this.#contourCache = null;
    this.#glyphCache = null;
    this.#curveCache = null;
  }

  /** Caches that depend on the visible window. The raster and the curves do not. */
  #invalidateView(): void {
    this.#glyphCache = null;
  }

  #autoRange(): Range | null {
    if (!this.#result || !this.#field) return null;
    const values = resultFieldValues(this.#result, this.#field);
    if (!values) return null;
    const raw = padRange(fieldRange(values, resultMask(this.#result)));
    return this.#symmetric ? symmetricRange(raw) : raw;
  }

  #computeRange(): Range | null {
    if (!this.#result || !this.#field) return null;
    if (this.#manualRange) return this.#manualRange;
    return this.#autoRange();
  }

  #unitsFor(field: string | null): string {
    if (field && this.#fieldUnits && Object.hasOwn(this.#fieldUnits, field)) {
      return this.#fieldUnits[field] ?? '';
    }
    return this.#units;
  }

  #labelFor(field: string | null): string {
    if (field && this.#fieldLabels && Object.hasOwn(this.#fieldLabels, field)) {
      return this.#fieldLabels[field] ?? field;
    }
    return field ?? '';
  }

  #canExport(): boolean {
    return typeof this.#canvas.toDataURL === 'function';
  }

  // ---------------------------------------------------------------- field paint

  /**
   * The coloured field as an offscreen raster, one texel per grid sample.
   *
   * Cached, because this is the expensive one: filling it is a pass over every sample with
   * a colormap lookup each. Panning and zooming only change where it is *drawn*, so a
   * viewport change re-blits an image it already has rather than recolouring 170k points.
   */
  #gridRaster(values: readonly number[], range: Range): Raster | null {
    const data = this.#result!.data as { shape: [number, number]; mask: number[] };
    const [ny, nx] = data.shape;
    const key = [
      this.#result!.job_id,
      this.#field,
      this.#colormap,
      range.min,
      range.max,
      nx,
      ny,
    ].join('|');
    if (this.#raster?.key === key) return this.#raster;

    const ctx = this.#canvas.getContext?.('2d');
    const image = ctx?.createImageData?.(nx, ny);
    if (!image) return null;
    for (let iy = 0; iy < ny; iy += 1) {
      for (let ix = 0; ix < nx; ix += 1) {
        const src = iy * nx + ix;
        // Row 0 of the payload is the *bottom* of the domain; canvas rows go down.
        const dst = ((ny - 1 - iy) * nx + ix) * 4;
        const [r, g, b] = data.mask?.[src]
          ? MASKED
          : sampleColormap(this.#colormap, normalise(values[src]!, range));
        image.data[dst] = r;
        image.data[dst + 1] = g;
        image.data[dst + 2] = b;
        image.data[dst + 3] = 255;
      }
    }
    const buffer = createBuffer(nx, ny);
    if (!buffer) return null;
    buffer.context.putImageData(image, 0, 0);
    this.#raster = { key, surface: buffer.surface, width: nx, height: ny };
    return this.#raster;
  }

  #drawGrid(
    ctx: CanvasRenderingContext2D,
    values: readonly number[],
    range: Range,
    view: Bounds2D,
    plot: PlotSize,
  ): void {
    const raster = this.#gridRaster(values, range);
    if (!raster) return;
    const bounds = this.#result!.data.bounds;
    // The raster covers the whole domain, so its destination rectangle is simply the
    // domain projected through the current viewport. One blit, whatever the zoom.
    const [x0, y0] = toScreen(view, plot, [bounds[0], bounds[3]]);
    const [x1, y1] = toScreen(view, plot, [bounds[2], bounds[1]]);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(raster.surface as CanvasImageSource, x0, y0, x1 - x0, y1 - y0);
  }

  #drawMesh(
    ctx: CanvasRenderingContext2D,
    values: readonly number[],
    range: Range,
    view: Bounds2D,
    plot: PlotSize,
  ): void {
    const data = this.#result!.data as { points: Point[]; triangles: [number, number, number][] };
    const project = (p: Point): Point => toScreen(view, plot, p);
    const pixels = data.points.map(project);
    // Cull against the plot rectangle rather than trusting the clip: a zoomed-in mesh has
    // most of its triangles off screen, and the cheapest triangle is the one never pathed.
    const margin = 2;
    for (const [i, j, k] of data.triangles) {
      const a = pixels[i]!;
      const b = pixels[j]!;
      const c = pixels[k]!;
      if (
        Math.max(a[0], b[0], c[0]) < -margin ||
        Math.min(a[0], b[0], c[0]) > plot.width + margin ||
        Math.max(a[1], b[1], c[1]) < -margin ||
        Math.min(a[1], b[1], c[1]) > plot.height + margin
      ) {
        continue;
      }
      const mean = (values[i]! + values[j]! + values[k]!) / 3;
      const [r, g, b2] = sampleColormap(this.#colormap, normalise(mean, range));
      ctx.fillStyle = `rgb(${r},${g},${b2})`;
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.lineTo(c[0], c[1]);
      ctx.closePath();
      ctx.fill();
    }
  }

  /** Contour segments in domain coordinates, cached: a pan re-projects, it re-extracts nothing. */
  #segments(range: Range): [Point, Point][] {
    const key = [this.#result!.job_id, this.#field, this.#contours, range.min, range.max].join('|');
    if (this.#contourCache?.key === key) return this.#contourCache.segments;
    const segments: [Point, Point][] = [];
    for (const level of isoLevels(range.min, range.max, this.#contours)) {
      segments.push(...contourSegments(this.#result!, this.#field!, level));
    }
    this.#contourCache = { key, segments };
    return segments;
  }

  #drawContours(
    ctx: CanvasRenderingContext2D,
    range: Range,
    view: Bounds2D,
    plot: PlotSize,
  ): void {
    ctx.strokeStyle = 'rgba(255,255,255,0.55)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const [a, b] of this.#segments(range)) {
      const pa = toScreen(view, plot, a);
      const pb = toScreen(view, plot, b);
      ctx.moveTo(pa[0], pa[1]);
      ctx.lineTo(pb[0], pb[1]);
    }
    ctx.stroke();
  }

  /**
   * Arrow glyphs for a vector field, on a lattice independent of the data's resolution.
   *
   * Arrows are scaled by magnitude but capped at the lattice spacing, so a fast region
   * cannot draw arrows that overlap their neighbours into an unreadable smear — the cap
   * is what keeps the *pattern* legible when the dynamic range is wide. Everything is
   * drawn in one path: hundreds of individually stroked arrows is the slow way.
   */
  #drawGlyphs(ctx: CanvasRenderingContext2D, view: Bounds2D, plot: PlotSize): void {
    const key = [this.#result!.job_id, this.#vectors, this.#glyphs, ...view].join('|');
    if (this.#glyphCache?.key !== key) {
      this.#glyphCache = {
        key,
        glyphs: glyphSamples(this.#result!, this.#vectors!, this.#glyphs, view),
      };
    }
    const glyphs = this.#glyphCache.glyphs;
    if (!glyphs.length) return;
    const project = (p: Point): Point => toScreen(view, plot, p);
    const peak = Math.max(...glyphs.map((g) => g.magnitude));
    if (!(peak > 0)) return;

    const spacing = plot.width / this.#glyphs;
    const maxLength = spacing * 0.85 * this.#glyphScale;
    ctx.strokeStyle = 'rgba(255,255,255,0.8)';
    ctx.fillStyle = 'rgba(255,255,255,0.8)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const glyph of glyphs) {
      const [px, py] = project([glyph.x, glyph.y]);
      // Direction in screen space: y is flipped, because the domain has y up and the
      // canvas has y down. Projecting a second point rather than negating by hand keeps
      // this correct if the projection ever changes.
      const [tx, ty] = project([glyph.x + glyph.vx, glyph.y + glyph.vy]);
      const dx = tx - px;
      const dy = ty - py;
      const screenLength = Math.hypot(dx, dy);
      if (!(screenLength > 0)) continue;
      const length = (glyph.magnitude / peak) * maxLength;
      const ux = (dx / screenLength) * length;
      const uy = (dy / screenLength) * length;
      // Centre the arrow on its lattice point instead of starting there: a tail-anchored
      // arrow visually biases the field downstream by half its own length.
      const x0 = px - ux / 2;
      const y0 = py - uy / 2;
      const x1 = px + ux / 2;
      const y1 = py + uy / 2;
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      const head = Math.min(length * 0.35, spacing * 0.3);
      const angle = Math.atan2(uy, ux);
      for (const sweep of [2.6, -2.6]) {
        ctx.moveTo(x1, y1);
        ctx.lineTo(x1 + head * Math.cos(angle + sweep), y1 + head * Math.sin(angle + sweep));
      }
    }
    ctx.stroke();
  }

  /**
   * Integral curves, computed in domain space and therefore cached across pan and zoom.
   *
   * Seeding is over the whole domain rather than the visible window on purpose: an
   * integral curve is a property of the field, not of where you are looking, and a set of
   * curves that reshuffled itself on every zoom would be a different picture of the same
   * data each time you moved.
   */
  #curves(): IntegralCurve[] {
    if (!this.#result || !this.#streamlines) return [];
    const key = [
      this.#result.job_id,
      this.#streamlines,
      this.#streamlineDensity,
      this.#streamlineSeeds ? JSON.stringify(this.#streamlineSeeds) : 'lattice',
    ].join('|');
    if (this.#curveCache?.key === key) return this.#curveCache.curves;
    const curves = integralCurves(this.#result, this.#streamlines, {
      density: this.#streamlineDensity,
      seeds: this.#streamlineSeeds ?? undefined,
    });
    this.#curveCache = { key, curves };
    return curves;
  }

  #drawCurves(ctx: CanvasRenderingContext2D, view: Bounds2D, plot: PlotSize): void {
    const curves = this.#curves();
    if (!curves.length) return;
    ctx.strokeStyle = 'rgba(255,255,255,0.75)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (const curve of curves) {
      curve.points.forEach((point, index) => {
        const [px, py] = toScreen(view, plot, point);
        if (index === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
    }
    ctx.stroke();
  }

  /** The application's geometry, outlined over the field. Never inferred from the result. */
  #drawGeometry(ctx: CanvasRenderingContext2D, view: Bounds2D, plot: PlotSize): void {
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.lineWidth = 1.5;
    for (const polygon of geometryPolygons(this.#geometry!)) {
      if (polygon.length < 2) continue;
      ctx.beginPath();
      polygon.forEach((point, index) => {
        const [px, py] = toScreen(view, plot, point);
        if (index === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.closePath();
      ctx.stroke();
    }
  }

  #drawSection(ctx: CanvasRenderingContext2D, view: Bounds2D, plot: PlotSize): void {
    const { start, end } = this.#section!;
    const a = toScreen(view, plot, start);
    const b = toScreen(view, plot, end);
    ctx.save();
    ctx.strokeStyle = 'rgba(245,158,11,0.95)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash?.([6, 4]);
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
    ctx.setLineDash?.([]);
    for (const point of [a, b]) {
      ctx.beginPath();
      ctx.arc(point[0], point[1], 3, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(245,158,11,0.95)';
      ctx.fill();
    }
    ctx.restore();
  }

  #drawColorbar(
    ctx: CanvasRenderingContext2D,
    range: Range,
    width: number,
    height: number,
  ): void {
    const x = width - COLORBAR_WIDTH - COLORBAR_MARGIN;
    const caption = this.#colorbarCaption();
    // The caption sits above the bar, so make room for it rather than letting it collide
    // with the topmost tick label.
    const top = COLORBAR_MARGIN + (caption ? 10 : 0);
    const barHeight = Math.max(1, height - top - COLORBAR_MARGIN);

    for (let i = 0; i < barHeight; i += 1) {
      const [r, g, b] = sampleColormap(this.#colormap, 1 - i / barHeight);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(x, top + i, COLORBAR_WIDTH, 1);
    }

    ctx.font = '10px ui-monospace, monospace';
    ctx.fillStyle = 'rgba(255,255,255,0.9)';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const tick of colorbarTicks(range, 5)) {
      const y = top + barHeight * (1 - normalise(tick.value, range));
      ctx.fillText(tick.label, x - 4, y);
    }
    if (caption) {
      // Right-aligned to the bar's right edge so the caption grows leftwards and
      // cannot run off the canvas, however long the string is.
      ctx.textAlign = 'right';
      ctx.fillText(caption, x + COLORBAR_WIDTH, top - 6);
    }
    if (this.#manualRange) {
      // A pinned scale must announce itself. Two pictures with identical colourbars mean
      // different things depending on whether the range was chosen or measured.
      ctx.textAlign = 'right';
      ctx.fillStyle = 'rgba(255,255,255,0.65)';
      ctx.fillText('fixed', x + COLORBAR_WIDTH, top + barHeight + 8);
    }
  }

  /** `name [unit]` — the two things a colourbar has to say beyond its numbers. */
  #colorbarCaption(): string {
    const label = this.#labelFor(this.#field);
    const units = this.#unitsFor(this.#field);
    if (label && units) return `${label} [${units}]`;
    return label || units;
  }

  #describe(): string {
    if (!this.#result || !this.#field) return 'Field viewer, no result loaded';
    const range = this.#computeRange();
    const kind = this.#result.kind === 'mesh2d' ? 'unstructured mesh' : 'grid';
    const units = this.#unitsFor(this.#field);
    const zoom = this.zoom;
    return (
      `Field ${this.#field} on a ${kind}` +
      (range ? `, ranging from ${range.min.toPrecision(3)} to ${range.max.toPrecision(3)}` : '') +
      (units ? ` ${units}` : '') +
      (this.#manualRange ? ', on a fixed colour scale' : '') +
      (zoom > 1.01 ? `, zoomed ${zoom.toPrecision(2)}x` : '') +
      (this.#interactive
        ? `. Interactive, ${this.#mode} mode; arrow keys pan, plus and minus zoom, 0 resets`
        : '')
    );
  }

  // ------------------------------------------------------------------- pointers

  /** Pointer position in plot-local CSS pixels, or null when the box has no size. */
  #plotPoint(event: PointerEvent | WheelEvent): Point | null {
    const rect = this.#canvas.getBoundingClientRect?.();
    if (!rect || !rect.width || !rect.height) return null;
    return [event.clientX - rect.left, event.clientY - rect.top];
  }

  #domainPoint(event: PointerEvent | WheelEvent): Point | null {
    const local = this.#plotPoint(event);
    if (!local) return null;
    const rect = this.#canvas.getBoundingClientRect!();
    // The plot rect used for the transform must be the one `draw` used, which is derived
    // from the element's own box rather than from the canvas' backing store.
    return toDomain(this.#view(), this.#plot(rect.width, rect.height), local);
  }

  #onPointerDown = (event: PointerEvent): void => {
    // `none` means exactly that: no pointer gesture is claimed, including a pinch. It is
    // what an application sets while an overlaid editor owns the surface, and a viewer
    // that kept one gesture for itself would make that promise conditional.
    if (!this.#interactive || !this.#result || this.#mode === 'none') return;
    const local = this.#plotPoint(event);
    if (!local) return;
    this.#pointers.set(event.pointerId, local);

    if (this.#pointers.size === 2) {
      // A second finger converts the gesture into a pinch, and a pinch is a zoom in every
      // other mode — no application wants two fingers to mean "probe twice".
      this.#dragFrom = null;
      this.#pinchDistance = this.#pinchSpan();
      return;
    }

    this.#canvas.setPointerCapture?.(event.pointerId);
    const domain = this.#domainPoint(event);
    switch (this.#mode) {
      case 'pan':
        this.#dragFrom = local;
        break;
      case 'section':
        if (domain) {
          this.#section = { start: domain, end: domain };
          this.render();
        }
        break;
      case 'seed':
        if (domain) {
          this.addStreamlineSeed(domain);
          this.#emit('fs-seed', { at: domain });
        }
        break;
      default:
        break;
    }
  };

  #onPointerMove = (event: PointerEvent): void => {
    if (this.#pointers.has(event.pointerId)) {
      this.#pointers.set(event.pointerId, this.#plotPoint(event) ?? [0, 0]);
    }
    if (this.#pointers.size === 2 && this.#result) {
      const span = this.#pinchSpan();
      if (this.#pinchDistance > 0 && span > 0) {
        const centre = this.#pinchCentre();
        const plot = this.#plot();
        this.#applyViewport(
          clampViewport(
            zoomViewport(this.#view(), plot, centre, this.#pinchDistance / span),
            this.#result.data.bounds,
          ),
          'zoom',
        );
      }
      this.#pinchDistance = span;
      return;
    }

    if (this.#dragFrom && this.#result) {
      const local = this.#plotPoint(event);
      if (local) {
        this.panBy(local[0] - this.#dragFrom[0], local[1] - this.#dragFrom[1]);
        this.#dragFrom = local;
      }
      return;
    }

    if (this.#section && this.#interactive && this.#mode === 'section' && this.#pointers.size) {
      const domain = this.#domainPoint(event);
      if (domain) {
        this.#section = { start: this.#section.start, end: domain };
        this.#emitSection('preview');
        this.render();
      }
      return;
    }

    this.#updateReadout(event);
  };

  #onPointerUp = (event: PointerEvent): void => {
    this.#pointers.delete(event.pointerId);
    this.#canvas.releasePointerCapture?.(event.pointerId);
    if (this.#pointers.size < 2) this.#pinchDistance = 0;
    this.#dragFrom = null;
    if (this.#section && this.#mode === 'section') this.#emitSection('commit');
  };

  #onPointerLeave = (): void => {
    this.#readout.hidden = true;
    this.#pointers.clear();
    this.#dragFrom = null;
    this.#emit('fs-probe', { at: null, value: null, field: this.#field });
  };

  #onWheel = (event: WheelEvent): void => {
    if (!this.#interactive || !this.#result || this.#mode === 'none') return;
    // Only when the gesture is ours: an unconditional preventDefault on a page-embedded
    // viewer hijacks the page's scroll, which is the complaint every map widget gets.
    event.preventDefault();
    const local = this.#plotPoint(event);
    if (!local) return;
    const factor = event.deltaY > 0 ? WHEEL_ZOOM : 1 / WHEEL_ZOOM;
    const plot = this.#plot();
    this.#applyViewport(
      clampViewport(zoomViewport(this.#view(), plot, local, factor), this.#result.data.bounds),
      'zoom',
    );
  };

  #onKeyDown = (event: KeyboardEvent): void => {
    if (!this.#interactive || !this.#result) return;
    const plot = this.#plot();
    const stepX = plot.width * KEY_PAN;
    const stepY = plot.height * KEY_PAN;
    switch (event.key) {
      case 'ArrowLeft':
        this.panBy(stepX, 0);
        break;
      case 'ArrowRight':
        this.panBy(-stepX, 0);
        break;
      case 'ArrowUp':
        this.panBy(0, stepY);
        break;
      case 'ArrowDown':
        this.panBy(0, -stepY);
        break;
      case '+':
      case '=':
        this.zoomBy(1 / KEY_ZOOM);
        break;
      case '-':
      case '_':
        this.zoomBy(KEY_ZOOM);
        break;
      case '0':
      case 'Home':
        this.resetView();
        break;
      case 'g':
        if (!this.fitGeometry()) return;
        break;
      case 'Escape':
        if (!this.#section) return;
        this.clearSection();
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  #pinchSpan(): number {
    const [a, b] = [...this.#pointers.values()];
    return a && b ? Math.hypot(a[0] - b[0], a[1] - b[1]) : 0;
  }

  #pinchCentre(): Point {
    const [a, b] = [...this.#pointers.values()];
    return a && b ? [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] : [0, 0];
  }

  #updateReadout(event: PointerEvent): void {
    if (!this.#result || !this.#field) return;
    const at = this.#domainPoint(event);
    if (!at) return;
    const value = this.probe(at);
    if (value === undefined) {
      this.#readout.hidden = true;
      this.#emit('fs-probe', { at, value: null, field: this.#field });
      return;
    }
    const units = this.#unitsFor(this.#field);
    this.#readout.hidden = false;
    this.#readout.textContent =
      `${this.#labelFor(this.#field)} = ${formatValue(value)}${units ? ` ${units}` : ''}` +
      `  @ (${at[0].toPrecision(3)}, ${at[1].toPrecision(3)})`;
    this.#emit('fs-probe', { at, value, field: this.#field, units });
  }

  #emitSection(phase: 'preview' | 'commit'): void {
    if (!this.#section) return;
    this.#emit('fs-section', {
      start: [...this.#section.start],
      end: [...this.#section.end],
      phase,
      field: this.#field,
      // Sampled here rather than left to the listener: the point of the mode is to hand
      // an application a curve, and every listener would otherwise write the same call.
      section: phase === 'commit' ? this.sampleSection(this.#section.start, this.#section.end) : null,
    });
  }

  #emit(type: string, detail: unknown): void {
    this.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
  }

  // -------------------------------------------------------------------- toolbar

  #syncFocusability(): void {
    if (this.#interactive) {
      if (!this.hasAttribute('tabindex')) this.setAttribute('tabindex', '0');
      this.setAttribute('role', 'application');
      this.setAttribute(
        'aria-label',
        'Field viewer. Arrow keys pan, plus and minus zoom, 0 resets the view.',
      );
    } else if (this.getAttribute('tabindex') === '0') {
      this.removeAttribute('tabindex');
      this.removeAttribute('role');
      this.removeAttribute('aria-label');
    }
  }

  #buildToolbar(): void {
    this.#toolbarEl.replaceChildren();
    this.#toolbarEl.hidden = this.#tools.length === 0;
    for (const tool of this.#tools) {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.tool = tool;
      button.textContent = TOOL_LABELS[tool].label;
      button.title = TOOL_LABELS[tool].title;
      button.setAttribute('aria-label', TOOL_LABELS[tool].title);
      button.addEventListener('click', () => this.#runTool(tool));
      this.#toolbarEl.append(button);
    }
    this.#syncToolbar();
  }

  /** Reflect availability and pressed state onto the buttons, with the reason as a tooltip. */
  #syncToolbar(): void {
    if (!this.#tools.length) return;
    const capabilities = this.capabilities;
    for (const button of this.#toolbarEl.querySelectorAll('button')) {
      const tool = button.dataset.tool as ToolbarTool;
      const mode = MODE_TOOLS[tool];
      const capability =
        tool === 'fit-geometry'
          ? capabilities.fitGeometry
          : tool === 'export'
            ? capabilities.export
            : mode === 'probe'
              ? capabilities.probe
              : mode === 'section'
                ? capabilities.section
                : mode === 'seed'
                  ? capabilities.streamlines
                  : { available: this.#result !== null };
      button.disabled = !capability.available;
      button.title = capability.available
        ? TOOL_LABELS[tool].title
        : `${TOOL_LABELS[tool].title} — unavailable: ${capability.reason ?? 'not supported here'}`;
      if (mode) button.setAttribute('aria-pressed', String(this.#mode === mode));
      else button.removeAttribute('aria-pressed');
    }
  }

  #runTool(tool: ToolbarTool): void {
    const mode = MODE_TOOLS[tool];
    if (mode) {
      this.mode = mode;
      return;
    }
    switch (tool) {
      case 'zoom-in':
        this.zoomBy(1 / KEY_ZOOM);
        break;
      case 'zoom-out':
        this.zoomBy(KEY_ZOOM);
        break;
      case 'fit-domain':
        this.fitDomain();
        break;
      case 'fit-geometry':
        this.fitGeometry();
        break;
      case 'reset':
        this.resetView();
        break;
      case 'export':
        this.#emit('fs-export', { dataUrl: this.toDataURL() });
        break;
    }
  }

  #geometryBounds(): Bounds2D | null {
    if (!this.#geometry) return null;
    let xmin = Infinity;
    let ymin = Infinity;
    let xmax = -Infinity;
    let ymax = -Infinity;
    for (const polygon of geometryPolygons(this.#geometry)) {
      for (const [x, y] of polygon) {
        if (x < xmin) xmin = x;
        if (y < ymin) ymin = y;
        if (x > xmax) xmax = x;
        if (y > ymax) ymax = y;
      }
    }
    return Number.isFinite(xmin) && Number.isFinite(ymin) ? [xmin, ymin, xmax, ymax] : null;
  }
}

/**
 * The outlines a geometry carries: the obstacle of a `domain2d`, every region of a
 * `regions2d`. The domain rectangle itself is not one of them — it is the bounds, which is
 * what "fit domain" already frames.
 */
export function geometryPolygons(geometry: Geometry): Point[][] {
  if (geometry.type === 'domain2d') return [geometry.obstacle.points.map((p) => [...p] as Point)];
  return geometry.regions.map((region) => region.shape.points.map((p) => [...p] as Point));
}

/**
 * An offscreen pixel buffer, falling back to a detached `<canvas>`.
 *
 * `OffscreenCanvas` is missing in browsers that otherwise support canvas fine (Safari
 * before 16.4), where constructing it throws and the field never renders at all.
 */
function createBuffer(
  width: number,
  height: number,
): { surface: unknown; context: CanvasRenderingContext2D } | null {
  if (typeof OffscreenCanvas === 'function') {
    const surface = new OffscreenCanvas(width, height);
    const context = surface.getContext('2d') as CanvasRenderingContext2D | null;
    if (context) return { surface, context };
  }
  if (typeof document === 'undefined') return null;
  const surface = document.createElement('canvas');
  surface.width = width;
  surface.height = height;
  const context = surface.getContext('2d');
  return context ? { surface, context } : null;
}

function formatValue(value: number): string {
  const magnitude = Math.abs(value);
  return magnitude !== 0 && (magnitude >= 1e4 || magnitude < 1e-3)
    ? value.toExponential(3)
    : value.toPrecision(4);
}

export function defineFieldViewer(tagName = 'fs-viewer'): void {
  if (!customElements.get(tagName)) {
    customElements.define(tagName, FieldViewerElement);
  }
}
