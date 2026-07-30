/**
 * Viewport arithmetic — the mapping between a *domain window* and the plot rectangle.
 *
 * A viewport is a `Bounds2D`, exactly like `result.data.bounds`: the part of the domain
 * currently visible, in domain units. That choice is deliberate over a
 * `{centre, zoom}` pair, because every other coordinate in this protocol is already
 * expressed that way — a caller that has a geometry's bounds can hand them straight to
 * `viewer.viewport` and get "frame this", with no unit conversion in between.
 *
 * **The mapping stays anisotropic.** `<fs-viewer>` has always stretched the domain to
 * fill its box, and preserving aspect ratio here would silently re-frame every existing
 * consumer's page. Zoom multiplies both spans by the *same* factor, so whatever aspect
 * distortion a page starts with, it keeps — panning and zooming never change the shape of
 * what is on screen, which is the property that actually matters for reading a field.
 *
 * Everything here is a pure function of (viewport, plot size, gesture). No canvas, no DOM,
 * no element state — so the interesting claims ("zoom keeps the point under the pointer
 * fixed") are ordinary arithmetic assertions rather than screenshots.
 */

import type { Bounds2D } from '@fenix-spoon/client';

// Local, not re-exported: `field.ts` already publishes `Point` for this package and two
// identical aliases in one barrel is an ambiguity a consumer has to resolve by hand.
import type { Point } from './field.js';

/** The drawing area a viewport is projected onto, in CSS pixels. */
export interface PlotSize {
  width: number;
  height: number;
}

/**
 * How far the interactive gestures may go, as a multiple of the domain span.
 *
 * Both ends are guards rather than preferences. Zooming in past ~1e4 puts the viewport
 * span into the same order as float rounding on the domain coordinates; zooming out past
 * a few domain widths leaves the data a speck with nothing to explore. A caller that
 * genuinely wants a wilder frame can still set `viewport` directly — the clamp is on the
 * *gestures*, not on the property.
 */
export const MAX_ZOOM_IN = 1e4;
export const MAX_ZOOM_OUT = 8;

export function viewportSpan(view: Bounds2D): [number, number] {
  return [view[2] - view[0], view[3] - view[1]];
}

export function viewportCentre(view: Bounds2D): Point {
  return [(view[0] + view[2]) / 2, (view[1] + view[3]) / 2];
}

/** A viewport is usable if it is finite and has a strictly positive span on both axes. */
export function isValidViewport(view: unknown): view is Bounds2D {
  return (
    Array.isArray(view) &&
    view.length === 4 &&
    view.every((v) => typeof v === 'number' && Number.isFinite(v)) &&
    (view[2] as number) > (view[0] as number) &&
    (view[3] as number) > (view[1] as number)
  );
}

export function sameViewport(a: Bounds2D, b: Bounds2D, epsilon = 1e-12): boolean {
  const scale = Math.max(1e-30, Math.abs(a[2] - a[0]), Math.abs(a[3] - a[1]));
  return a.every((v, i) => Math.abs(v - b[i]!) <= epsilon * scale);
}

/** Domain -> plot-local CSS pixels, with y flipped so +y points up. */
export function toScreen(view: Bounds2D, plot: PlotSize, [x, y]: Point): Point {
  const [width, height] = viewportSpan(view);
  return [
    ((x - view[0]) / width) * plot.width,
    plot.height - ((y - view[1]) / height) * plot.height,
  ];
}

/** Plot-local CSS pixels -> domain. The exact inverse of {@link toScreen}. */
export function toDomain(view: Bounds2D, plot: PlotSize, [px, py]: Point): Point {
  const [width, height] = viewportSpan(view);
  return [
    view[0] + (px / plot.width) * width,
    view[1] + (1 - py / plot.height) * height,
  ];
}

/**
 * Zoom about a point on screen: `factor < 1` moves in, `> 1` moves out.
 *
 * The defining property, and the one the tests assert: the domain point under `anchorPx`
 * is at `anchorPx` afterwards too. Anything else makes a wheel feel like it is fighting
 * the pointer.
 */
export function zoomViewport(
  view: Bounds2D,
  plot: PlotSize,
  anchorPx: Point,
  factor: number,
): Bounds2D {
  if (!(factor > 0) || !Number.isFinite(factor)) return view;
  const [width, height] = viewportSpan(view);
  const u = plot.width > 0 ? anchorPx[0] / plot.width : 0.5;
  const v = plot.height > 0 ? 1 - anchorPx[1] / plot.height : 0.5;
  const anchorX = view[0] + u * width;
  const anchorY = view[1] + v * height;
  const nextWidth = width * factor;
  const nextHeight = height * factor;
  return [
    anchorX - u * nextWidth,
    anchorY - v * nextHeight,
    anchorX + (1 - u) * nextWidth,
    anchorY + (1 - v) * nextHeight,
  ];
}

/**
 * Drag the content by a screen-space delta.
 *
 * Dragging right moves the *content* right, which moves the window left — the sign that
 * makes a drag feel like it is holding the picture rather than scrolling a scrollbar.
 */
export function panViewport(
  view: Bounds2D,
  plot: PlotSize,
  dxPx: number,
  dyPx: number,
): Bounds2D {
  const [width, height] = viewportSpan(view);
  const dx = plot.width > 0 ? -(dxPx / plot.width) * width : 0;
  const dy = plot.height > 0 ? (dyPx / plot.height) * height : 0;
  return [view[0] + dx, view[1] + dy, view[2] + dx, view[3] + dy];
}

/**
 * Frame `target`, with a margin expressed as a fraction of its own span.
 *
 * A zero-span target — a degenerate geometry, a single point — would otherwise produce a
 * viewport nothing can be drawn in, so it is widened to something with an extent. The
 * fallback span is relative to the coordinate, not absolute: a domain in millimetres and
 * one in kilometres both want a margin proportional to what they measure.
 */
export function fitViewport(target: Bounds2D, padding = 0.04): Bounds2D {
  let [xmin, ymin, xmax, ymax] = target;
  let width = xmax - xmin;
  let height = ymax - ymin;
  if (!(width > 0)) {
    const pad = Math.abs(xmin) > 0 ? Math.abs(xmin) * 0.5 : 0.5;
    xmin -= pad;
    xmax += pad;
    width = xmax - xmin;
  }
  if (!(height > 0)) {
    const pad = Math.abs(ymin) > 0 ? Math.abs(ymin) * 0.5 : 0.5;
    ymin -= pad;
    ymax += pad;
    height = ymax - ymin;
  }
  const mx = width * padding;
  const my = height * padding;
  return [xmin - mx, ymin - my, xmax + mx, ymax + my];
}

/**
 * Keep a gesture inside the guard rails: bounded zoom, and the centre still over the data.
 *
 * Centre-inside-the-domain rather than "the view must contain the domain" is what lets a
 * caller push a feature to the edge of the box to look at it against a neighbour, while
 * still making it impossible to fling the field off screen and be left with an empty
 * rectangle and no way back except the reset button.
 */
export function clampViewport(view: Bounds2D, domain: Bounds2D): Bounds2D {
  if (!isValidViewport(view)) return domain;
  const [domainWidth, domainHeight] = viewportSpan(domain);
  let [width, height] = viewportSpan(view);
  const [cx, cy] = viewportCentre(view);

  // One correction applied to both axes, so clamping cannot change the view's aspect —
  // a zoom that silently squashed the picture at the limit would be worse than stopping.
  let correction = 1;
  if (domainWidth > 0) {
    const minWidth = domainWidth / MAX_ZOOM_IN;
    const maxWidth = domainWidth * MAX_ZOOM_OUT;
    if (width < minWidth) correction = minWidth / width;
    else if (width > maxWidth) correction = maxWidth / width;
  }
  if (domainHeight > 0) {
    const minHeight = domainHeight / MAX_ZOOM_IN;
    const maxHeight = domainHeight * MAX_ZOOM_OUT;
    const scaled = height * correction;
    if (scaled < minHeight) correction *= minHeight / scaled;
    else if (scaled > maxHeight) correction *= maxHeight / scaled;
  }
  width *= correction;
  height *= correction;

  const clampedX = Math.min(domain[2], Math.max(domain[0], cx));
  const clampedY = Math.min(domain[3], Math.max(domain[1], cy));
  return [
    clampedX - width / 2,
    clampedY - height / 2,
    clampedX + width / 2,
    clampedY + height / 2,
  ];
}

/** Current zoom as a multiple of the domain span; 1 means the whole domain is framed. */
export function zoomLevel(view: Bounds2D, domain: Bounds2D): number {
  const [width] = viewportSpan(view);
  const [domainWidth] = viewportSpan(domain);
  return width > 0 ? domainWidth / width : 1;
}
