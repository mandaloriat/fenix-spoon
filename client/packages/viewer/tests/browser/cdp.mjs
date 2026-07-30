/**
 * A minimal Chrome DevTools Protocol client, and a static file server, in about 150 lines.
 *
 * **Why not Playwright.** The jsdom suite covers everything that is arithmetic — the
 * transforms, the integrator, the caches — and cannot cover the three things a gesture
 * actually depends on: real layout, a real canvas, and real pointer events synthesised by
 * the browser rather than constructed by the test. Something has to drive a browser.
 *
 * Playwright would do it and costs a ~150 MB browser download on every `npm ci`, in a
 * package whose entire point is a small embed footprint. Node 22 ships a WebSocket client,
 * Chromium speaks CDP over one, and `Input.dispatchMouseEvent` produces events the renderer
 * cannot tell from a mouse. That is the whole dependency argument: this file is the cost,
 * and it is paid once.
 *
 * The suite skips itself, loudly, when no Chromium is on the machine.
 */

import { spawn } from 'node:child_process';
import { createReadStream, existsSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { join, normalize, resolve } from 'node:path';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
};

/** Serve `root` read-only on an ephemeral port. */
export async function serve(root) {
  const base = resolve(root);
  const server = createServer((request, response) => {
    const path = normalize(decodeURIComponent(new URL(request.url, 'http://x').pathname));
    const file = join(base, path);
    if (!file.startsWith(base) || !existsSync(file) || statSync(file).isDirectory()) {
      response.writeHead(404).end('not found');
      return;
    }
    const extension = file.slice(file.lastIndexOf('.'));
    response.writeHead(200, { 'content-type': MIME[extension] ?? 'application/octet-stream' });
    createReadStream(file).pipe(response);
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((done) => server.close(done)),
  };
}

/**
 * Where a Chromium might be. `PLAYWRIGHT_BROWSERS_PATH` is checked because CI images that
 * already have a browser usually have it there, and re-downloading one would defeat the
 * point of not depending on Playwright.
 */
export function findChromium() {
  const explicit = process.env.FENIXSPOON_CHROMIUM ?? process.env.CHROME_PATH;
  if (explicit && existsSync(explicit)) return explicit;

  const root = process.env.PLAYWRIGHT_BROWSERS_PATH;
  if (root && existsSync(root)) {
    for (const entry of readdirSync(root)) {
      if (!entry.startsWith('chromium')) continue;
      for (const candidate of [
        join(root, entry, 'chrome-linux', 'chrome'),
        join(root, entry, 'chrome-linux', 'headless_shell'),
        join(root, entry, 'chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'),
      ]) {
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  for (const candidate of [
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

/** Launch headless Chromium and attach to its first page target. */
export async function launch(executable) {
  const child = spawn(
    executable,
    [
      '--headless=new',
      '--remote-debugging-port=0',
      '--no-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      '--hide-scrollbars',
      '--window-size=900,600',
      'about:blank',
    ],
    { stdio: ['ignore', 'ignore', 'pipe'] },
  );

  const endpoint = await new Promise((done, fail) => {
    let buffer = '';
    const timer = setTimeout(() => fail(new Error('chromium did not report a debug port')), 30000);
    child.stderr.on('data', (chunk) => {
      buffer += chunk;
      const match = /ws:\/\/([^\s]+)/.exec(buffer);
      if (match) {
        clearTimeout(timer);
        done(match[0]);
      }
    });
    child.on('exit', (code) => fail(new Error(`chromium exited with ${code}`)));
  });

  const port = new URL(endpoint).port;
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find((target) => target.type === 'page');
  if (!page) throw new Error('chromium started with no page target');

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((done, fail) => {
    socket.addEventListener('open', done, { once: true });
    socket.addEventListener('error', fail, { once: true });
  });

  let nextId = 0;
  const pending = new Map();
  const listeners = new Map();
  socket.addEventListener('message', (message) => {
    const payload = JSON.parse(message.data);
    if (payload.id !== undefined) {
      const entry = pending.get(payload.id);
      pending.delete(payload.id);
      if (payload.error) entry?.fail(new Error(payload.error.message));
      else entry?.done(payload.result);
      return;
    }
    for (const listener of listeners.get(payload.method) ?? []) listener(payload.params);
  });

  const send = (method, params = {}) =>
    new Promise((done, fail) => {
      const id = (nextId += 1);
      pending.set(id, { done, fail });
      socket.send(JSON.stringify({ id, method, params }));
    });

  const once = (method) =>
    new Promise((done) => {
      const handlers = listeners.get(method) ?? [];
      const handler = (params) => {
        listeners.set(
          method,
          (listeners.get(method) ?? []).filter((h) => h !== handler),
        );
        done(params);
      };
      listeners.set(method, [...handlers, handler]);
    });

  await send('Page.enable');
  await send('Runtime.enable');

  return {
    send,
    async goto(url) {
      const loaded = once('Page.loadEventFired');
      await send('Page.navigate', { url });
      await loaded;
    },
    /** Evaluate in the page and return the value, awaiting a promise if one comes back. */
    async evaluate(expression) {
      const result = await send('Runtime.evaluate', {
        expression: `(async () => { ${expression} })()`,
        awaitPromise: true,
        returnByValue: true,
      });
      if (result.exceptionDetails) {
        throw new Error(
          result.exceptionDetails.exception?.description ?? result.exceptionDetails.text,
        );
      }
      return result.result.value;
    },
    async close() {
      socket.close();
      child.kill();
    },
  };
}
