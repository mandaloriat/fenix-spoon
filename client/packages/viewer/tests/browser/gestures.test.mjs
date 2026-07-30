/**
 * Gesture tests in a real browser.
 *
 * These cover exactly what jsdom cannot: layout (the element has a size, so a pixel
 * coordinate means something), a real 2-D canvas (so the raster path is exercised rather
 * than skipped), and pointer events synthesised by the renderer from mouse and touch input
 * rather than constructed by the test. Everything provable by arithmetic is in the vitest
 * suites next door, and is not repeated here.
 *
 *     npm run test:browser --workspace @fenix-spoon/viewer
 *
 * It needs the packages built (`npm run build --prefix client`) and a Chromium on the
 * machine. With neither, it exits 0 with a skip message rather than failing a run that was
 * never asked to do this.
 */

import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { findChromium, launch, serve } from './cdp.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const packages = resolve(here, '../../..');

const cases = [];
const test = (name, body) => cases.push({ name, body });

// --------------------------------------------------------------------------- cases

test('paints the field onto a real canvas', async (page) => {
  // jsdom has no 2-D context at all, so the entire raster path — the offscreen buffer, the
  // blit, the colourbar — is untested until something like this runs it.
  const painted = await page.evaluate(`
    const canvas = viewer.shadowRoot.querySelector('canvas');
    const ctx = canvas.getContext('2d');
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let opaque = 0;
    for (let i = 3; i < data.length; i += 4) if (data[i] > 0) opaque += 1;
    return opaque / (data.length / 4);
  `);
  assert.ok(painted > 0.9, `expected the field to cover the canvas, got ${painted}`);
});

test('wheel zooms about the pointer', async (page) => {
  const result = await page.evaluate(`
    viewer.resetView();
    return { before: viewer.toDomain([200, 100]), zoom: viewer.zoom };
  `);
  await wheel(page, 200, 100, -240);
  const after = await page.evaluate(
    'return { at: viewer.toDomain([200, 100]), zoom: viewer.zoom };',
  );
  assert.ok(after.zoom > result.zoom, 'the wheel should have zoomed in');
  assert.ok(Math.abs(after.at[0] - result.before[0]) < 1e-6, 'x under the pointer moved');
  assert.ok(Math.abs(after.at[1] - result.before[1]) < 1e-6, 'y under the pointer moved');
});

test('drag pans, and the content follows the pointer', async (page) => {
  await page.evaluate('viewer.resetView(); viewer.mode = "pan"; return true;');
  const before = await page.evaluate('return viewer.viewport;');
  await drag(page, 400, 200, 480, 200);
  const after = await page.evaluate('return viewer.viewport;');
  // 80 px right on an 800 px plot is a tenth of the 8-unit domain.
  assert.ok(Math.abs(after[0] - (before[0] - 0.8)) < 1e-6, `viewport did not pan: ${after}`);
});

test('a drag in probe mode does not pan — the modes really are exclusive', async (page) => {
  await page.evaluate('viewer.resetView(); viewer.mode = "probe"; return true;');
  await drag(page, 400, 200, 480, 200);
  const viewport = await page.evaluate('return viewer.viewport;');
  assert.deepEqual(viewport, [0, 0, 8, 8]);
});

test('hovering reads the field out, in the DOM', async (page) => {
  await page.evaluate('viewer.resetView(); viewer.mode = "probe"; events.length = 0; return true;');
  await move(page, 400, 200);
  const readout = await page.evaluate(`
    const el = viewer.shadowRoot.querySelector('.readout');
    return { hidden: el.hidden, text: el.textContent, live: el.getAttribute('aria-live') };
  `);
  assert.equal(readout.hidden, false);
  assert.match(readout.text, /^psi = /);
  assert.equal(readout.live, 'polite');
  const probes = await page.evaluate('return events.filter((e) => e.type === "fs-probe").length;');
  assert.ok(probes > 0, 'a probe event should have been emitted');
});

test('the keyboard pans and resets once the element has focus', async (page) => {
  await page.evaluate('viewer.resetView(); viewer.focus(); return document.activeElement.id;');
  await key(page, 'ArrowRight', 39);
  const panned = await page.evaluate('return viewer.viewport;');
  assert.ok(panned[0] > 0, `ArrowRight should have panned, got ${panned}`);
  await key(page, '0', 48);
  assert.deepEqual(await page.evaluate('return viewer.viewport;'), [0, 0, 8, 8]);
});

test('a two-finger pinch zooms', async (page) => {
  await page.evaluate('viewer.resetView(); viewer.mode = "probe"; return true;');
  await page.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 2 });
  await pinch(page, 400, 200, 40, 160);
  await page.send('Emulation.setTouchEmulationEnabled', { enabled: false });
  const zoom = await page.evaluate('return viewer.zoom;');
  assert.ok(zoom > 1.2, `pinch should have zoomed in, got ${zoom}`);
});

test('the toolbar is operable and switches mode', async (page) => {
  const modes = await page.evaluate(`
    viewer.mode = 'probe';
    events.length = 0;
    viewer.shadowRoot.querySelector('[data-tool=pan]').click();
    return { mode: viewer.mode, events: events.filter((e) => e.type === 'fs-mode-change').length };
  `);
  assert.equal(modes.mode, 'pan');
  assert.equal(modes.events, 1);
});

test('exports the current view as a PNG', async (page) => {
  const url = await page.evaluate('return viewer.toDataURL();');
  assert.match(url, /^data:image\/png;base64,/);
  assert.ok(url.length > 1000, 'the exported image should not be blank');
});

test('integrates curves that close on themselves in a rotation field', async (page) => {
  // The fixture's field is a rigid rotation, so every curve is a circle. Asserted here as
  // well as in the unit suite because this is the path that also *draws* them.
  const summary = await page.evaluate(`
    viewer.resetView();
    viewer.streamlineDensity = 10;
    const curves = viewer.integralCurves;
    return { count: curves.length, closed: curves.filter((c) => c.closed).length };
  `);
  assert.ok(summary.count > 4, `expected several curves, got ${summary.count}`);
  assert.ok(summary.closed > 0, 'a rotation field should produce closed curves');
});

test('fits the geometry it was given', async (page) => {
  const viewport = await page.evaluate('viewer.resetView(); viewer.fitGeometry(0); return viewer.viewport;');
  assert.deepEqual(viewport, [3, 3, 5, 5]);
});

// ------------------------------------------------------------------------- input

async function move(page, x, y) {
  await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, buttons: 0 });
}

async function wheel(page, x, y, deltaY) {
  await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, buttons: 0 });
  await page.send('Input.dispatchMouseEvent', { type: 'mouseWheel', x, y, deltaX: 0, deltaY });
}

async function drag(page, x0, y0, x1, y1) {
  const common = { button: 'left', buttons: 1, clickCount: 1 };
  await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: x0, y: y0, ...common });
  await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x1, y: y1, ...common });
  await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x1, y: y1, ...common });
}

async function key(page, keyName, code) {
  const common = { key: keyName, code: keyName, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code };
  await page.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...common });
  await page.send('Input.dispatchKeyEvent', { type: 'keyUp', ...common });
}

async function pinch(page, cx, cy, from, to) {
  const points = (half) => [
    { x: cx - half, y: cy, id: 1 },
    { x: cx + half, y: cy, id: 2 },
  ];
  await page.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: points(from) });
  for (let half = from; half <= to; half += 20) {
    await page.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: points(half) });
  }
  await page.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}

// ------------------------------------------------------------------------ runner

async function main() {
  const built = ['viewer/dist/index.js', 'client/dist/index.js'].every((file) =>
    existsSync(resolve(packages, file)),
  );
  if (!built) {
    console.log('SKIP browser gestures — run `npm run build --prefix client` first.');
    return;
  }
  const executable = findChromium();
  if (!executable) {
    console.log('SKIP browser gestures — no Chromium found (set FENIXSPOON_CHROMIUM).');
    return;
  }

  const server = await serve(packages);
  const page = await launch(executable);
  let failures = 0;
  try {
    await page.goto(`${server.origin}/viewer/tests/browser/fixture.html`);
    await page.evaluate(`
      for (let i = 0; i < 100 && !globalThis.ready; i += 1) {
        await new Promise((done) => setTimeout(done, 20));
      }
      await new Promise(requestAnimationFrame);
      return globalThis.ready === true;
    `);
    for (const { name, body } of cases) {
      try {
        await body(page);
        // Every gesture ends in a rAF-coalesced paint; let it happen before the next case
        // reads the canvas, so a test never observes a half-applied frame.
        await page.evaluate('await new Promise(requestAnimationFrame); return true;');
        console.log(`  ok  ${name}`);
      } catch (error) {
        failures += 1;
        console.error(`  FAIL ${name}\n       ${error.message}`);
      }
    }
  } finally {
    await page.close();
    await server.close();
  }
  console.log(`${cases.length - failures}/${cases.length} browser gesture tests passed`);
  if (failures) process.exitCode = 1;
}

await main();
