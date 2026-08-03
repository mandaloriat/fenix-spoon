/**
 * `<fs-plot>` — draws the curves a Fenix Spoon result carries.
 *
 * Protocol 1.5 added `series1d` and the `series` key beside a field, and the wire-protocol
 * document has listed the consequence as unfinished ever since: *"`series1d` has no browser
 * renderer yet; the protocol carries the shape and every consumer still writes its own plot."*
 * This is that renderer. **No protocol change** — every number it draws has been on the wire
 * since 1.5, which is the same relationship `<fs-viewer>`'s explorable tools have to 1.1.
 *
 * ```html
 * <fs-plot series="surface_cp" invert-y legend></fs-plot>
 * <script type="module">
 *   import '@fenix-spoon/plot';
 *   document.querySelector('fs-plot').result = await job.result({ level: 'series' });
 * </script>
 * ```
 *
 * ## Why a fourth package rather than a second element in the viewer
 *
 * They share a canvas and almost nothing else. The viewer needs colormaps, a viewport, contour
 * extraction and glyph lattices; a plot needs scales, round ticks and a legend, and none of
 * those helps draw a field. Folding this in would make every page that shows a temperature map
 * carry axis code it never calls — against the one property `@fenix-spoon/viewer` was built
 * around, which is that the embed stays small enough to be worth embedding.
 *
 * ## `invert-y` is an attribute, never an inference
 *
 * A pressure coefficient is plotted with `C_p` increasing *downwards*, and it would be easy to
 * notice a trace called `cp_upper` and flip the axis for the caller. That is exactly the class
 * of guess [ADR 0001](../../../docs/adr/0001-explorable-viewer.md) records the viewer refusing:
 * it will not infer a vector field from a scalar one, a geometry outline from a mask, or a
 * physical name for an integrated curve. A name is not a quantity. The convention is real, so
 * the attribute exists and the airfoil demo sets it — but the page says so, not the widget.
 */

import {
  type JobResult,
  type Series1DData,
  resultSeries,
} from '@fenix-spoon/client';

import {
  type NearestPoint,
  type ResolvedTrace,
  axisLabel,
  extentX,
  extentY,
  nearestPoint,
  resolveTraces,
  sharedUnit,
} from './curves.js';
import {
  type Scale,
  type ScaleKind,
  type Tick,
  LOG_FLOOR,
  padRange,
  scaleFor,
  ticksFor,
} from './scale.js';

/** Distinguishable at a glance, and readable on both light and dark backgrounds. */
const TRACE_COLORS = [
  '#2b6cb0',
  '#c05621',
  '#2f855a',
  '#b83280',
  '#6b46c1',
  '#b7791f',
  '#2c7a7b',
  '#9b2c2c',
] as const;

/** Room for tick labels and axis captions, in CSS pixels. */
const MARGIN = { top: 14, right: 16, bottom: 40, left: 56 } as const;

/** How near the pointer must be, in pixels, before a point is reported. */
const HOVER_RADIUS = 24;

const SCALE_KINDS: readonly ScaleKind[] = ['linear', 'log'];

function isScaleKind(value: string | null): value is ScaleKind {
  return value !== null && (SCALE_KINDS as readonly string[]).includes(value);
}

/** Why a tool is unavailable, in the shape `<fs-viewer>` reports its own capabilities. */
export interface PlotCapabilities {
  /** Whether there is anything to draw at all. */
  draw: { available: boolean; reason?: string };
  /** Whether a log y axis is usable on the current data. */
  logY: { available: boolean; reason?: string };
}

export class PlotElement extends HTMLElement {
  static observedAttributes = [
    'series',
    'invert-y',
    'legend',
    'x-scale',
    'y-scale',
    'x-label',
    'y-label',
    'interactive',
  ];

  #result: JobResult | null = null;
  #curves: Series1DData | null = null;
  #canvas: HTMLCanvasElement | null = null;
  #readout: HTMLElement | null = null;
  #hover: NearestPoint | null = null;
  #size = { width: 0, height: 0 };
  #observer: ResizeObserver | null = null;

  // ------------------------------------------------------------------ data in

  /**
   * The result to draw. Accepts either kind: a `series1d` payload *is* the curve set, and a
   * field result carries its curves beside `data`. `resultSeries` is the SDK accessor that
   * knows which, so this element never branches on `kind` itself.
   */
  get result(): JobResult | null {
    return this.#result;
  }

  set result(result: JobResult | null) {
    this.#result = result;
    this.#curves = null;
    this.#hover = null;
    this.#render();
  }

  /**
   * A curve set directly, for a page that already has one — a sweep it assembled itself, or a
   * comparison of two solves. Setting this wins over `result`.
   */
  get curves(): Series1DData | null {
    return this.#curves ?? this.#selected();
  }

  set curves(curves: Series1DData | null) {
    this.#curves = curves;
    this.#hover = null;
    this.#render();
  }

  /** Every curve set the current result carries, by name — what a picker offers. */
  get available(): string[] {
    return this.#result ? resultSeries(this.#result).map((entry) => entry.name) : [];
  }

  /** Which curve set to draw. Null draws the first, which is the whole answer for one. */
  get series(): string | null {
    return this.getAttribute('series');
  }

  set series(name: string | null) {
    if (name === null) this.removeAttribute('series');
    else this.setAttribute('series', name);
  }

  // ------------------------------------------------------------------ display

  get invertY(): boolean {
    return this.hasAttribute('invert-y');
  }

  set invertY(value: boolean) {
    this.toggleAttribute('invert-y', value);
  }

  get legend(): boolean {
    return this.hasAttribute('legend');
  }

  set legend(value: boolean) {
    this.toggleAttribute('legend', value);
  }

  get xScale(): ScaleKind {
    const stated = this.getAttribute('x-scale');
    return isScaleKind(stated) ? stated : 'linear';
  }

  set xScale(kind: ScaleKind) {
    this.setAttribute('x-scale', kind);
  }

  get yScale(): ScaleKind {
    const stated = this.getAttribute('y-scale');
    return isScaleKind(stated) ? stated : 'linear';
  }

  set yScale(kind: ScaleKind) {
    this.setAttribute('y-scale', kind);
  }

  /** Override the axis caption the payload's units would produce. */
  get xLabel(): string | null {
    return this.getAttribute('x-label');
  }

  set xLabel(label: string | null) {
    if (label === null) this.removeAttribute('x-label');
    else this.setAttribute('x-label', label);
  }

  get yLabel(): string | null {
    return this.getAttribute('y-label');
  }

  set yLabel(label: string | null) {
    if (label === null) this.removeAttribute('y-label');
    else this.setAttribute('y-label', label);
  }

  /** Whether pointer movement reports the nearest point. Off by default, like the viewer. */
  get interactive(): boolean {
    return this.hasAttribute('interactive');
  }

  set interactive(value: boolean) {
    this.toggleAttribute('interactive', value);
  }

  /** The last point a hover resolved to, or null. */
  get hovered(): NearestPoint | null {
    return this.#hover;
  }

  /**
   * What this element can currently do, and why not where it cannot.
   *
   * The same shape `<fs-viewer>` reports, for the same reason: a page that offers a log-scale
   * toggle needs to know whether the data admits one *before* drawing a blank frame, and a
   * refusal with a sentence beats an empty canvas.
   */
  get capabilities(): PlotCapabilities {
    const traces = this.#traces();
    if (traces.length === 0) {
      const reason = this.#curves ?? this.#selected() ? 'no trace has a usable abscissa' : 'no curves set';
      return { draw: { available: false, reason }, logY: { available: false, reason } };
    }
    const y = extentY(traces);
    const positive = y !== null && y.min > 0;
    return {
      draw: { available: true },
      logY: positive
        ? { available: true }
        : {
            available: false,
            reason:
              'a log axis needs strictly positive values, and this curve reaches ' +
              `${y ? y.min : 0}; values at or below zero are clipped to ${LOG_FLOOR}`,
          },
    };
  }

  // ------------------------------------------------------------------ lifecycle

  connectedCallback(): void {
    if (!this.#canvas) this.#build();
    this.#observe();
    this.#render();
  }

  disconnectedCallback(): void {
    this.#observer?.disconnect();
    this.#observer = null;
  }

  attributeChangedCallback(): void {
    this.#hover = null;
    this.#render();
  }

  #build(): void {
    if (!this.style.display) this.style.display = 'block';
    if (!this.style.position) this.style.position = 'relative';

    const canvas = document.createElement('canvas');
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.display = 'block';
    this.appendChild(canvas);
    this.#canvas = canvas;

    // The readout is a live region rather than a tooltip: a hover that only paints pixels is
    // invisible to a screen reader, and the number under the pointer is the whole answer.
    const readout = document.createElement('p');
    readout.setAttribute('aria-live', 'polite');
    readout.style.cssText =
      'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);margin:-1px;';
    this.appendChild(readout);
    this.#readout = readout;

    canvas.addEventListener('pointermove', this.#onPointerMove);
    canvas.addEventListener('pointerleave', this.#onPointerLeave);
  }

  #observe(): void {
    if (typeof ResizeObserver === 'undefined' || this.#observer) return;
    this.#observer = new ResizeObserver(() => this.#render());
    this.#observer.observe(this);
  }

  // ------------------------------------------------------------------ resolution

  #selected(): Series1DData | null {
    if (!this.#result) return null;
    const all = resultSeries(this.#result);
    if (all.length === 0) return null;
    const wanted = this.series;
    if (wanted === null) return all[0]!;
    return all.find((entry) => entry.name === wanted) ?? null;
  }

  #traces(): ResolvedTrace[] {
    const curves = this.#curves ?? this.#selected();
    return curves ? resolveTraces(curves) : [];
  }

  /** The two scales for the current data and box, or null when there is nothing to draw. */
  #scales(width: number, height: number, traces: readonly ResolvedTrace[]) {
    const x = extentX(traces);
    const y = extentY(traces);
    if (!x || !y) return null;
    const left = MARGIN.left;
    const right = Math.max(left + 1, width - MARGIN.right);
    const top = MARGIN.top;
    const bottom = Math.max(top + 1, height - MARGIN.bottom);
    return {
      x: scaleFor(this.xScale === 'log' ? x : padRange(x, 0.02), left, right, this.xScale),
      // Inverted by swapping the pixel endpoints, so nothing downstream has to know: a
      // projection is a projection, and `invert-y` becomes one argument rather than a flag
      // every drawing call has to remember.
      y: this.invertY
        ? scaleFor(padRange(y), top, bottom, this.yScale)
        : scaleFor(padRange(y), bottom, top, this.yScale),
    };
  }

  // ------------------------------------------------------------------ input

  #onPointerMove = (event: PointerEvent): void => {
    if (!this.interactive || !this.#canvas) return;
    const rect = this.#canvas.getBoundingClientRect();
    const at = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    const traces = this.#traces();
    const scales = this.#scales(this.#size.width, this.#size.height, traces);
    if (!scales) return;

    const found = nearestPoint(
      traces,
      at,
      (point) => ({ x: scales.x.project(point.x), y: scales.y.project(point.y) }),
      HOVER_RADIUS,
    );
    if (found?.trace === this.#hover?.trace && found?.at === this.#hover?.at) return;
    this.#hover = found;
    this.#announce(found);
    this.dispatchEvent(
      new CustomEvent('fs-plot-hover', { detail: found, bubbles: true, composed: true }),
    );
    this.#render();
  };

  #onPointerLeave = (): void => {
    if (this.#hover === null) return;
    this.#hover = null;
    this.#announce(null);
    this.dispatchEvent(
      new CustomEvent('fs-plot-hover', { detail: null, bubbles: true, composed: true }),
    );
    this.#render();
  };

  #announce(found: NearestPoint | null): void {
    if (!this.#readout) return;
    this.#readout.textContent = found
      ? `${found.trace.name}: ${format(found.x)}, ${format(found.y)} ${found.trace.unit}`
      : '';
  }

  // ------------------------------------------------------------------ painting

  #render(): void {
    const canvas = this.#canvas;
    if (!canvas) return;

    const ratio = typeof devicePixelRatio === 'number' ? devicePixelRatio : 1;
    const width = this.clientWidth || Number(this.getAttribute('width')) || 640;
    const height = this.clientHeight || Number(this.getAttribute('height')) || 320;
    this.#size = { width, height };
    // Only reallocate the backing store when the element actually changed size — the same
    // rule the viewer follows, because reassigning `width` clears the canvas.
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }

    // Describe before painting, and before the context is even asked for. The accessible
    // name is a function of the *data*, not of whether this environment can rasterise — so a
    // browser with canvas disabled, or any headless renderer, still gets an element that
    // announces what it holds instead of an unlabelled box. Writing the tests found this:
    // the obvious order bails on a null context and skips the description with it.
    const traces = this.#traces();
    this.#describe(traces);

    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const scales = this.#scales(width, height, traces);
    if (!scales) return;

    const xTicks = ticksFor(scales.x);
    const yTicks = ticksFor(scales.y);
    this.#paintFrame(ctx, scales.x, scales.y, xTicks, yTicks, width, height);
    this.#paintTraces(ctx, scales.x, scales.y, traces);
    if (this.#hover) this.#paintHover(ctx, scales.x, scales.y);
    if (this.legend && traces.length > 0) this.#paintLegend(ctx, traces);
  }

  #paintFrame(
    ctx: CanvasRenderingContext2D,
    x: Scale,
    y: Scale,
    xTicks: Tick[],
    yTicks: Tick[],
    width: number,
    height: number,
  ): void {
    const left = MARGIN.left;
    const right = width - MARGIN.right;
    const top = MARGIN.top;
    const bottom = height - MARGIN.bottom;

    ctx.save();
    ctx.font = '11px system-ui, sans-serif';
    ctx.strokeStyle = '#d0d7de';
    ctx.fillStyle = '#57606a';
    ctx.lineWidth = 1;

    for (const tick of yTicks) {
      const at = Math.round(y.project(tick.value)) + 0.5;
      if (at < top - 0.5 || at > bottom + 0.5) continue;
      ctx.beginPath();
      ctx.moveTo(left, at);
      ctx.lineTo(right, at);
      ctx.stroke();
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(tick.label, left - 6, at);
    }
    for (const tick of xTicks) {
      const at = Math.round(x.project(tick.value)) + 0.5;
      if (at < left - 0.5 || at > right + 0.5) continue;
      ctx.beginPath();
      ctx.moveTo(at, top);
      ctx.lineTo(at, bottom);
      ctx.stroke();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(tick.label, at, bottom + 6);
    }

    ctx.strokeStyle = '#8c959f';
    ctx.strokeRect(left + 0.5, top + 0.5, right - left, bottom - top);

    ctx.fillStyle = '#24292f';
    ctx.font = '12px system-ui, sans-serif';
    const captions = this.#captions();
    if (captions.x) {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.fillText(captions.x, (left + right) / 2, height - 4);
    }
    if (captions.y) {
      ctx.save();
      ctx.translate(12, (top + bottom) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(captions.y, 0, 0);
      ctx.restore();
    }
    ctx.restore();
  }

  #paintTraces(
    ctx: CanvasRenderingContext2D,
    x: Scale,
    y: Scale,
    traces: readonly ResolvedTrace[],
  ): void {
    ctx.save();
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    traces.forEach((trace, index) => {
      ctx.strokeStyle = TRACE_COLORS[index % TRACE_COLORS.length]!;
      ctx.beginPath();
      let started = false;
      for (const point of trace.points) {
        if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) {
          // A gap, not a straight line across it. Joining over a NaN would draw a chord
          // through a region the solve said nothing about.
          started = false;
          continue;
        }
        const at = { x: x.project(point.x), y: y.project(point.y) };
        if (started) ctx.lineTo(at.x, at.y);
        else ctx.moveTo(at.x, at.y);
        started = true;
      }
      ctx.stroke();
    });
    ctx.restore();
  }

  #paintHover(ctx: CanvasRenderingContext2D, x: Scale, y: Scale): void {
    const found = this.#hover;
    if (!found) return;
    const at = { x: x.project(found.x), y: y.project(found.y) };
    ctx.save();
    ctx.fillStyle = TRACE_COLORS[found.trace.index % TRACE_COLORS.length]!;
    ctx.beginPath();
    ctx.arc(at.x, at.y, 4, 0, Math.PI * 2);
    ctx.fill();

    const label = `${format(found.x)}, ${format(found.y)}`;
    ctx.font = '11px system-ui, sans-serif';
    const width = ctx.measureText(label).width + 8;
    const boxX = Math.min(at.x + 8, MARGIN.left + Math.max(0, this.#size.width - MARGIN.left - MARGIN.right - width));
    ctx.fillStyle = 'rgba(255,255,255,0.92)';
    ctx.fillRect(boxX, at.y - 20, width, 16);
    ctx.fillStyle = '#24292f';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, boxX + 4, at.y - 12);
    ctx.restore();
  }

  #paintLegend(ctx: CanvasRenderingContext2D, traces: readonly ResolvedTrace[]): void {
    ctx.save();
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    // Each entry carries its own unit when the traces disagree, which is the case
    // `sharedUnit` returns null for — the axis cannot caption them all, so the legend does.
    const mixed = sharedUnit(traces) === null;
    const entries = traces.map((trace) =>
      mixed && trace.unit && trace.unit !== '1' ? `${trace.name} [${trace.unit}]` : trace.name,
    );
    const widths = entries.map((entry) => ctx.measureText(entry).width + 24);
    const boxWidth = Math.max(...widths);
    let top = MARGIN.top + 6;
    const left = this.#size.width - MARGIN.right - boxWidth - 6;

    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.fillRect(left - 4, top - 4, boxWidth + 8, entries.length * 16 + 8);
    entries.forEach((entry, index) => {
      ctx.strokeStyle = TRACE_COLORS[index % TRACE_COLORS.length]!;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(left, top + 8);
      ctx.lineTo(left + 16, top + 8);
      ctx.stroke();
      ctx.fillStyle = '#24292f';
      ctx.fillText(entry, left + 20, top + 8);
      top += 16;
    });
    ctx.restore();
  }

  #captions(): { x: string; y: string } {
    const curves = this.#curves ?? this.#selected();
    const traces = this.#traces();
    const axis = curves?.x ?? curves?.traces.find((trace) => trace.x)?.x ?? null;
    return {
      x: this.xLabel ?? (axis ? axisLabel(axis.name, axis.unit) : ''),
      y: this.yLabel ?? this.#defaultYCaption(traces),
    };
  }

  #defaultYCaption(traces: readonly ResolvedTrace[]): string {
    const unit = sharedUnit(traces);
    // One trace names itself; several sharing a unit are captioned by the unit alone; several
    // disagreeing get nothing here and their units in the legend instead.
    if (traces.length === 1) return axisLabel(traces[0]!.name, traces[0]!.unit);
    if (unit === null) return '';
    return unit === '1' ? '' : `[${unit}]`;
  }

  /**
   * A text description of the canvas, kept current.
   *
   * A canvas is opaque to assistive technology, so without this the element is an empty box
   * with a live region that only speaks on hover. `<fs-viewer>` states its scale, zoom and
   * mode for the same reason.
   */
  #describe(traces: readonly ResolvedTrace[]): void {
    const canvas = this.#canvas;
    if (!canvas) return;
    canvas.setAttribute('role', 'img');
    if (traces.length === 0) {
      canvas.setAttribute('aria-label', 'Plot with no curves.');
      return;
    }
    const captions = this.#captions();
    const names = traces.map((trace) => trace.name).join(', ');
    const points = traces.reduce((total, trace) => total + trace.points.length, 0);
    canvas.setAttribute(
      'aria-label',
      `Plot of ${traces.length} ${traces.length === 1 ? 'curve' : 'curves'} (${names}), ` +
        `${points} points, x axis ${captions.x || 'unlabelled'}, ` +
        `y axis ${captions.y || 'unlabelled'}${this.invertY ? ', inverted' : ''}.`,
    );
  }
}

function format(value: number): string {
  if (value === 0) return '0';
  const magnitude = Math.abs(value);
  if (magnitude >= 1e5 || magnitude < 1e-3) return value.toExponential(2);
  return value.toFixed(magnitude >= 100 ? 1 : magnitude >= 1 ? 3 : 4);
}

export function definePlot(tagName = 'fs-plot'): void {
  if (typeof customElements !== 'undefined' && !customElements.get(tagName)) {
    customElements.define(tagName, PlotElement);
  }
}
