/**
 * `<fs-viewer>` — renders a Fenix Spoon `grid2d` or `mesh2d` result.
 *
 * Canvas here, deliberately, and note that this is the *opposite* call from
 * `@fenix-spoon/geometry-2d`, which uses SVG. The rule is what the pixels are for: a
 * handful of interactive handles want to be DOM nodes (focusable, hit-tested by the
 * browser); thousands of coloured triangles per frame want a raster surface.
 *
 * All the decisions that don't need a drawing context — colormaps, ranges, contour
 * extraction, probing — live in `colormap.ts` and `field.ts` as pure functions, so they
 * are unit-tested directly and this file stays a thin painter.
 *
 * ```html
 * <fs-viewer field="speed" colormap="viridis" contours="10"></fs-viewer>
 * <script type="module">
 *   import '@fenix-spoon/viewer';
 *   document.querySelector('fs-viewer').result = await job.wait();
 * </script>
 * ```
 */

import { type FieldResult, type JobResult, isFieldResult } from '@fenix-spoon/client';

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
  contourSegments,
  isoLevels,
  probe,
  glyphSamples,
  resultFieldNames,
  resultFieldValues,
  resultVectorFieldNames,
  resultMask,
} from './field.js';

const STYLE = `
  :host { display: block; position: relative; background: var(--fs-viewer-bg, #1e1e22); }
  canvas { display: block; width: 100%; height: 100%; touch-action: none; }
  .readout {
    position: absolute; top: 0.5rem; left: 0.5rem; padding: 0.25rem 0.5rem;
    font: 12px/1.4 ui-monospace, monospace; border-radius: 4px; pointer-events: none;
    background: var(--fs-viewer-readout-bg, rgba(0,0,0,0.6));
    color: var(--fs-viewer-readout-fg, #fff);
  }
  .readout[hidden] { display: none; }
`;

const COLORBAR_WIDTH = 14;
const COLORBAR_MARGIN = 12;
const MASKED: [number, number, number] = [30, 30, 34];

export class FieldViewerElement extends HTMLElement {
  static observedAttributes = [
    'field', 'colormap', 'contours', 'colorbar', 'symmetric', 'units', 'vectors', 'glyphs',
  ];

  #root: ShadowRoot;
  #canvas: HTMLCanvasElement;
  #readout: HTMLDivElement;

  #result: FieldResult | null = null;
  #field: string | null = null;
  #colormap: ColormapName = 'viridis';
  #contours = 0;
  #showColorbar = true;
  #symmetric = false;
  #units = '';
  #vectors: string | null = null;
  #glyphs = 24;
  #frame = 0;

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
    this.#root.append(style, this.#canvas, this.#readout);

    this.#canvas.addEventListener('pointermove', this.#onPointerMove);
    this.#canvas.addEventListener('pointerleave', this.#onPointerLeave);
  }

  // ------------------------------------------------------------------ public API

  /**
   * The result to display. Setting it re-renders.
   *
   * Accepts any `JobResult` but only *stores* a field one. A `series1d` result (protocol 1.4)
   * is curves, and this widget draws a coloured 2-D domain: it has no bounds to fit, no
   * topology to interpolate over and no field to contour. Handing one over clears the view and
   * says so, rather than throwing or drawing a one-pixel picture of a curve — the axes, legend
   * and inverted-y convention a `C_p` plot needs belong to a separate widget.
   */
  get result(): JobResult | null {
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
      this.render();
      return;
    }
    this.#result = result;
    // Keep the current field if the new result still has it; otherwise fall back to
    // the first one, so swapping solvers doesn't blank the view.
    if (result && (!this.#field || !resultFieldNames(result).includes(this.#field))) {
      this.#field = resultFieldNames(result)[0] ?? null;
    }
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

  /** The scalar range currently mapped to the colormap. */
  get range(): Range | null {
    return this.#computeRange();
  }

  /** Sample the displayed field at a domain position. */
  probe(at: Point): number | undefined {
    if (!this.#result || !this.#field) return undefined;
    return probe(this.#result, this.#field, at);
  }

  /** The current view as a PNG data URL, or `''` where canvas export is unavailable. */
  toDataURL(type = 'image/png'): string {
    return this.#canvas.toDataURL?.(type) ?? '';
  }

  // ----------------------------------------------------------------- lifecycle

  connectedCallback(): void {
    this.render();
  }

  attributeChangedCallback(name: string, _old: string | null, value: string | null): void {
    switch (name) {
      case 'field':
        this.#field = value;
        break;
      case 'colormap':
        if (value && isColormapName(value)) this.#colormap = value;
        break;
      case 'vectors':
        this.#vectors = value || null;
        break;
      case 'glyphs': {
        const across = Number(value);
        this.#glyphs = Number.isFinite(across) && across > 0 ? Math.floor(across) : 24;
        break;
      }
      case 'contours': {
        const count = Number(value);
        this.#contours = Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
        break;
      }
      case 'colorbar':
        this.#showColorbar = value !== 'off';
        break;
      case 'symmetric':
        this.#symmetric = value !== null;
        break;
      case 'units':
        this.#units = value ?? '';
        break;
    }
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

    const plotWidth = this.#showColorbar
      ? Math.max(1, width - COLORBAR_WIDTH - COLORBAR_MARGIN * 2)
      : width;

    if (this.#result.kind === 'grid2d') this.#drawGrid(ctx, values, range, plotWidth, height);
    else this.#drawMesh(ctx, values, range, plotWidth, height);

    if (this.#contours > 0) this.#drawContours(ctx, range, plotWidth, height);
    if (this.#vectors) this.#drawGlyphs(ctx, plotWidth, height);
    if (this.#showColorbar) this.#drawColorbar(ctx, range, width, height);
  }

  #resizeCanvas(): { width: number; height: number } {
    const dpr = globalThis.devicePixelRatio ?? 1;
    const rect = this.getBoundingClientRect?.();
    const cssWidth = Math.max(1, Math.round(rect?.width || this.#canvas.width || 300));
    const cssHeight = Math.max(1, Math.round(rect?.height || this.#canvas.height || 150));
    this.#canvas.width = Math.round(cssWidth * dpr);
    this.#canvas.height = Math.round(cssHeight * dpr);
    const ctx = this.#canvas.getContext?.('2d');
    ctx?.setTransform?.(dpr, 0, 0, dpr, 0, 0);
    return { width: cssWidth, height: cssHeight };
  }

  #computeRange(): Range | null {
    if (!this.#result || !this.#field) return null;
    const values = resultFieldValues(this.#result, this.#field);
    if (!values) return null;
    const raw = padRange(fieldRange(values, resultMask(this.#result)));
    return this.#symmetric ? symmetricRange(raw) : raw;
  }

  /** Domain -> CSS pixels, with y flipped so +y points up. */
  #projector(width: number, height: number): (p: Point) => Point {
    const [xmin, ymin, xmax, ymax] = this.#result!.data.bounds;
    return ([x, y]) => [
      ((x - xmin) / (xmax - xmin)) * width,
      height - ((y - ymin) / (ymax - ymin)) * height,
    ];
  }

  #drawGrid(
    ctx: CanvasRenderingContext2D,
    values: readonly number[],
    range: Range,
    width: number,
    height: number,
  ): void {
    const data = this.#result!.data as { shape: [number, number]; mask: number[] };
    const [ny, nx] = data.shape;
    const image = ctx.createImageData?.(nx, ny);
    if (!image) return;
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
    if (!buffer) return;
    buffer.context.putImageData(image, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(buffer.surface as CanvasImageSource, 0, 0, width, height);
  }

  #drawMesh(
    ctx: CanvasRenderingContext2D,
    values: readonly number[],
    range: Range,
    width: number,
    height: number,
  ): void {
    const data = this.#result!.data as { points: Point[]; triangles: [number, number, number][] };
    const project = this.#projector(width, height);
    const pixels = data.points.map(project);
    for (const [i, j, k] of data.triangles) {
      const mean = (values[i]! + values[j]! + values[k]!) / 3;
      const [r, g, b] = sampleColormap(this.#colormap, normalise(mean, range));
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.beginPath();
      ctx.moveTo(pixels[i]![0], pixels[i]![1]);
      ctx.lineTo(pixels[j]![0], pixels[j]![1]);
      ctx.lineTo(pixels[k]![0], pixels[k]![1]);
      ctx.closePath();
      ctx.fill();
    }
  }

  #drawContours(
    ctx: CanvasRenderingContext2D,
    range: Range,
    width: number,
    height: number,
  ): void {
    const project = this.#projector(width, height);
    ctx.strokeStyle = 'rgba(255,255,255,0.55)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const level of isoLevels(range.min, range.max, this.#contours)) {
      for (const [a, b] of contourSegments(this.#result!, this.#field!, level)) {
        const pa = project(a);
        const pb = project(b);
        ctx.moveTo(pa[0], pa[1]);
        ctx.lineTo(pb[0], pb[1]);
      }
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
  #drawGlyphs(ctx: CanvasRenderingContext2D, width: number, height: number): void {
    const glyphs = glyphSamples(this.#result!, this.#vectors!, this.#glyphs);
    if (!glyphs.length) return;
    const project = this.#projector(width, height);
    const peak = Math.max(...glyphs.map((g) => g.magnitude));
    if (!(peak > 0)) return;

    const spacing = width / this.#glyphs;
    const maxLength = spacing * 0.85;
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

  #drawColorbar(
    ctx: CanvasRenderingContext2D,
    range: Range,
    width: number,
    height: number,
  ): void {
    const x = width - COLORBAR_WIDTH - COLORBAR_MARGIN;
    // The units caption sits above the bar, so make room for it rather than letting
    // it collide with the topmost tick label.
    const top = COLORBAR_MARGIN + (this.#units ? 10 : 0);
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
    if (this.#units) {
      // Right-aligned to the bar's right edge so the caption grows leftwards and
      // cannot run off the canvas, however long the unit string is.
      ctx.textAlign = 'right';
      ctx.fillText(this.#units, x + COLORBAR_WIDTH, top - 6);
    }
  }

  #describe(): string {
    if (!this.#result || !this.#field) return 'Field viewer, no result loaded';
    const range = this.#computeRange();
    const kind = this.#result.kind === 'mesh2d' ? 'unstructured mesh' : 'grid';
    return (
      `Field ${this.#field} on a ${kind}` +
      (range ? `, ranging from ${range.min.toPrecision(3)} to ${range.max.toPrecision(3)}` : '') +
      (this.#units ? ` ${this.#units}` : '')
    );
  }

  // ------------------------------------------------------------------- probing

  #onPointerMove = (event: PointerEvent): void => {
    if (!this.#result || !this.#field) return;
    const rect = this.#canvas.getBoundingClientRect?.();
    if (!rect || !rect.width || !rect.height) return;
    const width = this.#showColorbar
      ? Math.max(1, rect.width - COLORBAR_WIDTH - COLORBAR_MARGIN * 2)
      : rect.width;
    const [xmin, ymin, xmax, ymax] = this.#result.data.bounds;
    const x = xmin + ((event.clientX - rect.left) / width) * (xmax - xmin);
    const y = ymin + (1 - (event.clientY - rect.top) / rect.height) * (ymax - ymin);

    const value = this.probe([x, y]);
    if (value === undefined) {
      this.#readout.hidden = true;
      return;
    }
    this.#readout.hidden = false;
    this.#readout.textContent =
      `${this.#field} = ${formatValue(value)}${this.#units ? ` ${this.#units}` : ''}` +
      `  @ (${x.toPrecision(3)}, ${y.toPrecision(3)})`;
  };

  #onPointerLeave = (): void => {
    this.#readout.hidden = true;
  };
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
