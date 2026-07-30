import { describe, expect, it } from 'vitest';

import type { Bounds2D } from '@fenix-spoon/client';

import {
  MAX_ZOOM_IN,
  MAX_ZOOM_OUT,
  clampViewport,
  fitViewport,
  isValidViewport,
  panViewport,
  sameViewport,
  toDomain,
  toScreen,
  viewportCentre,
  viewportSpan,
  zoomLevel,
  zoomViewport,
} from '../src/viewport.js';

/** A deliberately non-square domain and a non-square plot: the two must not be confused. */
const DOMAIN: Bounds2D = [-2, -1, 4, 2];
const PLOT = { width: 600, height: 200 };

describe('screen <-> domain transforms', () => {
  it('puts the domain corners at the plot corners, with y up', () => {
    expect(toScreen(DOMAIN, PLOT, [-2, 2])).toEqual([0, 0]); // top-left
    expect(toScreen(DOMAIN, PLOT, [4, -1])).toEqual([600, 200]); // bottom-right
    // The flip is the part that is easy to get wrong: +y in the domain is *up* the screen.
    const [, low] = toScreen(DOMAIN, PLOT, [0, -1]);
    const [, high] = toScreen(DOMAIN, PLOT, [0, 2]);
    expect(low).toBeGreaterThan(high);
  });

  it('round-trips through both directions', () => {
    for (const point of [
      [0, 0],
      [-1.5, 1.75],
      [3.9, -0.9],
    ] as [number, number][]) {
      const back = toDomain(DOMAIN, PLOT, toScreen(DOMAIN, PLOT, point));
      expect(back[0]).toBeCloseTo(point[0], 10);
      expect(back[1]).toBeCloseTo(point[1], 10);
    }
  });

  it('keeps the two axes independent, so a stretched plot does not skew a coordinate', () => {
    // Half way across in x, a quarter of the way up in y.
    const screen = toScreen(DOMAIN, PLOT, [1, -0.25]);
    expect(screen[0]).toBeCloseTo(300, 10);
    expect(screen[1]).toBeCloseTo(150, 10);
  });

  it('reports span and centre', () => {
    expect(viewportSpan(DOMAIN)).toEqual([6, 3]);
    expect(viewportCentre(DOMAIN)).toEqual([1, 0.5]);
  });
});

describe('zoom', () => {
  it('keeps the domain point under the pointer exactly where it was', () => {
    // The defining property of a pointer-anchored zoom, and the reason the arithmetic is
    // a pure function: this is an equality, not a screenshot.
    for (const anchor of [
      [0, 0],
      [137, 42],
      [600, 200],
      [301, 99],
    ] as [number, number][]) {
      const before = toDomain(DOMAIN, PLOT, anchor);
      for (const factor of [0.5, 0.9, 1.4, 3]) {
        const zoomed = zoomViewport(DOMAIN, PLOT, anchor, factor);
        const after = toDomain(zoomed, PLOT, anchor);
        expect(after[0]).toBeCloseTo(before[0], 9);
        expect(after[1]).toBeCloseTo(before[1], 9);
      }
    }
  });

  it('scales both axes by the same factor, so the picture never changes shape', () => {
    const zoomed = zoomViewport(DOMAIN, PLOT, [100, 100], 0.25);
    const [width, height] = viewportSpan(zoomed);
    expect(width).toBeCloseTo(6 * 0.25, 12);
    expect(height).toBeCloseTo(3 * 0.25, 12);
    expect(width / height).toBeCloseTo(6 / 3, 12);
  });

  it('reports the zoom level relative to the domain', () => {
    expect(zoomLevel(DOMAIN, DOMAIN)).toBeCloseTo(1);
    expect(zoomLevel(zoomViewport(DOMAIN, PLOT, [300, 100], 0.5), DOMAIN)).toBeCloseTo(2);
  });

  it('ignores a nonsensical factor rather than producing an inverted viewport', () => {
    expect(zoomViewport(DOMAIN, PLOT, [0, 0], 0)).toEqual(DOMAIN);
    expect(zoomViewport(DOMAIN, PLOT, [0, 0], -1)).toEqual(DOMAIN);
    expect(zoomViewport(DOMAIN, PLOT, [0, 0], NaN)).toEqual(DOMAIN);
  });
});

describe('pan', () => {
  it('moves the content with the pointer, not against it', () => {
    // Dragging 60 px to the right — a tenth of the plot — moves the window a tenth of its
    // width to the *left*, which is what makes the picture follow the finger.
    const panned = panViewport(DOMAIN, PLOT, 60, 0);
    expect(panned[0]).toBeCloseTo(-2 - 0.6, 10);
    expect(panned[2]).toBeCloseTo(4 - 0.6, 10);
    expect(panned[1]).toBeCloseTo(-1, 10);
  });

  it('flips y, because the screen counts downwards and the domain does not', () => {
    const panned = panViewport(DOMAIN, PLOT, 0, 20); // ten percent of the height, downwards
    expect(panned[1]).toBeCloseTo(-1 + 0.3, 10);
    expect(panned[3]).toBeCloseTo(2 + 0.3, 10);
  });

  it('preserves the span exactly', () => {
    const panned = panViewport(DOMAIN, PLOT, 123, -45);
    expect(viewportSpan(panned)[0]).toBeCloseTo(6, 10);
    expect(viewportSpan(panned)[1]).toBeCloseTo(3, 10);
  });

  it('is reversible, so a drag and its opposite land back where they started', () => {
    const there = panViewport(DOMAIN, PLOT, 77, 31);
    const back = panViewport(there, PLOT, -77, -31);
    expect(sameViewport(back, DOMAIN, 1e-9)).toBe(true);
  });
});

describe('fit', () => {
  it('frames a target with a proportional margin', () => {
    const fitted = fitViewport([0, 0, 1, 2], 0.1);
    expect(fitted).toEqual([-0.1, -0.2, 1.1, 2.2]);
  });

  it('widens a degenerate target instead of producing something undrawable', () => {
    const point = fitViewport([1, 1, 1, 1]);
    expect(point[2]).toBeGreaterThan(point[0]);
    expect(point[3]).toBeGreaterThan(point[1]);
    // A zero-height sliver — a flat plate — is the realistic case, not a literal point.
    const sliver = fitViewport([0, 0.5, 1, 0.5]);
    expect(sliver[3]).toBeGreaterThan(sliver[1]);
    expect(sliver[2] - sliver[0]).toBeGreaterThan(0);
  });
});

describe('clamping', () => {
  it('stops zooming in past the guard, keeping the aspect ratio', () => {
    // Proportional to the domain, which is what a gesture always produces: zoom scales
    // both spans by one factor, so a view that starts on the domain's aspect keeps it.
    const tiny: Bounds2D = [1 - 1e-6, 0.5 - 0.5e-6, 1 + 1e-6, 0.5 + 0.5e-6];
    const clamped = clampViewport(tiny, DOMAIN);
    const [width, height] = viewportSpan(clamped);
    expect(width).toBeCloseTo(6 / MAX_ZOOM_IN, 12);
    expect(width / height).toBeCloseTo(6 / 3, 6);
  });

  it('lets the tighter axis win when a caller sets an off-aspect viewport by hand', () => {
    // `viewport` is settable, so a viewport whose shape the gestures could never produce
    // is reachable. Correcting only x would leave y past its own guard; correcting for
    // the worse of the two is what keeps both inside, at the cost of a wider frame than
    // asked for — which is the honest outcome, and never a squashed one.
    const skewed: Bounds2D = [1 - 1e-6, 0.5 - 1e-7, 1 + 1e-6, 0.5 + 1e-7];
    const clamped = clampViewport(skewed, DOMAIN);
    const [width, height] = viewportSpan(clamped);
    expect(width).toBeGreaterThanOrEqual(6 / MAX_ZOOM_IN);
    expect(height).toBeCloseTo(3 / MAX_ZOOM_IN, 12);
    expect(width / height).toBeCloseTo(2e-6 / 2e-7, 6); // the caller's shape, preserved
  });

  it('stops zooming out past the guard', () => {
    const huge: Bounds2D = [-1000, -500, 1000, 500];
    const [width] = viewportSpan(clampViewport(huge, DOMAIN));
    expect(width).toBeCloseTo(6 * MAX_ZOOM_OUT, 9);
  });

  it('keeps the centre over the data so the field cannot be flung off screen', () => {
    const wandered: Bounds2D = [900, 900, 906, 903];
    const clamped = clampViewport(wandered, DOMAIN);
    const [cx, cy] = viewportCentre(clamped);
    expect(cx).toBeLessThanOrEqual(DOMAIN[2]);
    expect(cy).toBeLessThanOrEqual(DOMAIN[3]);
    expect(cx).toBeGreaterThanOrEqual(DOMAIN[0]);
    expect(cy).toBeGreaterThanOrEqual(DOMAIN[1]);
  });

  it('leaves a viewport inside the guards untouched', () => {
    const fine: Bounds2D = [0, 0, 3, 1.5];
    expect(sameViewport(clampViewport(fine, DOMAIN), fine, 1e-12)).toBe(true);
  });

  it('falls back to the domain for a viewport that is not one', () => {
    expect(clampViewport([1, 1, 0, 0], DOMAIN)).toEqual(DOMAIN);
  });
});

describe('validation', () => {
  it('rejects inverted, short and non-finite viewports', () => {
    expect(isValidViewport([0, 0, 1, 1])).toBe(true);
    expect(isValidViewport([1, 1, 0, 0])).toBe(false);
    expect(isValidViewport([0, 0, 1])).toBe(false);
    expect(isValidViewport([0, 0, NaN, 1])).toBe(false);
    expect(isValidViewport('nope')).toBe(false);
  });

  it('compares viewports relative to their own size', () => {
    expect(sameViewport([0, 0, 1, 1], [0, 0, 1, 1])).toBe(true);
    expect(sameViewport([0, 0, 1, 1], [0, 0, 1.5, 1])).toBe(false);
  });
});
