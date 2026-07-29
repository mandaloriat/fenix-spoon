/**
 * Client behaviour against a fake transport. The integration test in
 * `integration.test.ts` covers the real server; these cover the awkward paths a live
 * server won't reliably reproduce — notably a dropped event stream mid-job.
 */

import { describe, expect, it, vi } from 'vitest';

import { FenixSpoonClient, FenixSpoonError, JobFailedError } from '../src/client.js';
import type { JobEvent } from '../src/types.js';

/** Minimal WebSocket stand-in: scripted events, optional mid-stream close. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
    queueMicrotask(() => this.script());
  }

  /** Overridden per test. */
  script(): void {}

  emit(event: JobEvent): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.();
  }
}

function makeClient(
  handlers: Record<string, (init?: RequestInit) => unknown>,
  SocketClass: typeof FakeSocket,
) {
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const path = url.replace('http://server', '');
    const handler = handlers[`${init?.method ?? 'GET'} ${path}`] ?? handlers[path];
    if (!handler) {
      return new Response(JSON.stringify({ detail: 'not found' }), { status: 404 });
    }
    const body = handler(init);
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });

  return new FenixSpoonClient('http://server', {
    fetch: fetchImpl as unknown as typeof globalThis.fetch,
    WebSocket: SocketClass as unknown as typeof globalThis.WebSocket,
    reconnectDelayMs: 1,
  });
}

const GRID_RESULT = {
  job_id: 'j-1',
  kind: 'grid2d',
  data: {
    bounds: [0, 0, 1, 1],
    shape: [1, 2],
    fields: { psi: [0, 1] },
    mask: [0, 0],
  },
  artifacts: [
    { name: 'solution.vtk', content_type: 'model/vnd.vtk', size: 10, url: '/api/v1/a/solution.vtk' },
  ],
};

describe('FenixSpoonClient', () => {
  it('submits a job and exposes its id', async () => {
    const client = makeClient(
      { 'POST /api/v1/jobs': () => ({ job_id: 'j-1', status: 'queued' }) },
      FakeSocket,
    );
    const job = await client.submit({
      solver: 'mock.laplace2d',
      geometry: {
        type: 'domain2d',
        bounds: [-1, -1, 2, 1],
        obstacle: { type: 'polygon2d', points: [[0, 0], [1, 0], [0.5, 0.5]] },
      },
    });
    expect(job.id).toBe('j-1');
  });

  it('surfaces HTTP errors with status and detail', async () => {
    const client = makeClient(
      {
        'POST /api/v1/jobs': () =>
          new Response(JSON.stringify({ detail: 'unknown solver' }), { status: 404 }),
      },
      FakeSocket,
    );
    await expect(
      client.submit({
        solver: 'nope',
        geometry: {
          type: 'domain2d',
          bounds: [-1, -1, 2, 1],
          obstacle: { type: 'polygon2d', points: [[0, 0], [1, 0], [0.5, 0.5]] },
        },
      }),
    ).rejects.toMatchObject({
      name: 'FenixSpoonError',
      status: 404,
      detail: 'unknown solver',
      // A prose detail belongs in the message too: callers print `error.message`.
      message: 'POST /api/v1/jobs failed: HTTP 404 — unknown solver',
    });
  });

  it('keeps a structured detail off the message but on the error', async () => {
    // pydantic validation errors are a list of objects; stringifying them into the
    // message would produce "[object Object]".
    const detail = [{ loc: ['body', 'params', 'resolution'], msg: 'less than 16' }];
    const client = makeClient(
      { '/api/v1/jobs/j-1/result': () => new Response(JSON.stringify({ detail }), { status: 422 }) },
      FakeSocket,
    );
    await expect(client.job('j-1').result()).rejects.toMatchObject({
      status: 422,
      detail,
      message: 'GET /api/v1/jobs/j-1/result failed: HTTP 422',
    });
  });

  it('streams events in order and stops after the terminal event', async () => {
    class Socket extends FakeSocket {
      script() {
        this.emit({ type: 'status', status: 'running' });
        this.emit({ type: 'progress', iteration: 1, total: 2 });
        this.emit({ type: 'status', status: 'done' });
      }
    }
    const client = makeClient({}, Socket);
    const seen: JobEvent[] = [];
    for await (const event of client.events('j-1')) seen.push(event);
    expect(seen).toEqual([
      { type: 'status', status: 'running' },
      { type: 'progress', iteration: 1, total: 2 },
      { type: 'status', status: 'done' },
    ]);
  });

  it('reconnects on a dropped stream without re-delivering replayed events', async () => {
    // The server replays a job's whole history on connect, so after a drop the second
    // socket resends what we already saw. The iterator must skip exactly those.
    let attempt = 0;
    class Socket extends FakeSocket {
      script() {
        attempt += 1;
        if (attempt === 1) {
          this.emit({ type: 'status', status: 'running' });
          this.emit({ type: 'progress', iteration: 1 });
          this.close(); // connection lost mid-job, no terminal event
        } else {
          this.emit({ type: 'status', status: 'running' });
          this.emit({ type: 'progress', iteration: 1 });
          this.emit({ type: 'progress', iteration: 2 });
          this.emit({ type: 'status', status: 'done' });
        }
      }
    }
    const client = makeClient({}, Socket);
    const seen: JobEvent[] = [];
    for await (const event of client.events('j-1')) seen.push(event);

    expect(attempt).toBe(2);
    expect(seen).toEqual([
      { type: 'status', status: 'running' },
      { type: 'progress', iteration: 1 },
      { type: 'progress', iteration: 2 },
      { type: 'status', status: 'done' },
    ]);
  });

  it('gives up after maxReconnects rather than looping forever', async () => {
    class Socket extends FakeSocket {
      script() {
        this.emit({ type: 'status', status: 'running' });
        this.close();
      }
    }
    const client = new FenixSpoonClient('http://server', {
      fetch: (() => {}) as unknown as typeof globalThis.fetch,
      WebSocket: Socket as unknown as typeof globalThis.WebSocket,
      maxReconnects: 2,
      reconnectDelayMs: 1,
    });
    const seen: JobEvent[] = [];
    await expect(
      (async () => {
        for await (const event of client.events('j-1')) seen.push(event);
      })(),
    ).rejects.toThrow(/without a terminal event/);
    expect(seen).toEqual([{ type: 'status', status: 'running' }]);
  });

  it('wait() reports progress then resolves with the result', async () => {
    class Socket extends FakeSocket {
      script() {
        this.emit({ type: 'status', status: 'running' });
        this.emit({ type: 'progress', iteration: 5, residual: 1e-6 });
        this.emit({ type: 'status', status: 'done' });
      }
    }
    const client = makeClient({ '/api/v1/jobs/j-1/result': () => GRID_RESULT }, Socket);
    const progress: JobEvent[] = [];
    const result = await client.job('j-1').wait((event) => progress.push(event));
    expect(result.kind).toBe('grid2d');
    expect(progress.filter((e) => e.type === 'progress')).toHaveLength(1);
  });

  it('wait() throws JobFailedError carrying the server error', async () => {
    class Socket extends FakeSocket {
      script() {
        this.emit({ type: 'status', status: 'running' });
        this.emit({ type: 'status', status: 'failed', error: 'ValueError: bad mesh' });
      }
    }
    const client = makeClient({}, Socket);
    await expect(client.job('j-1').wait()).rejects.toMatchObject({
      name: 'JobFailedError',
      status: 'failed',
      message: 'ValueError: bad mesh',
    });
  });

  it('wait() treats cancellation as a failure state', async () => {
    class Socket extends FakeSocket {
      script() {
        this.emit({ type: 'status', status: 'cancelled' });
      }
    }
    const client = makeClient({}, Socket);
    await expect(client.job('j-1').wait()).rejects.toBeInstanceOf(JobFailedError);
  });

  it('aborts a stream via AbortSignal', async () => {
    class Socket extends FakeSocket {
      script() {
        this.emit({ type: 'status', status: 'running' });
        // then nothing: the job is still running
      }
    }
    const client = makeClient({}, Socket);
    const controller = new AbortController();
    const seen: JobEvent[] = [];
    const pump = (async () => {
      for await (const event of client.events('j-1', { signal: controller.signal })) {
        seen.push(event);
        controller.abort();
      }
    })();
    await expect(pump).rejects.toMatchObject({ name: 'AbortError' });
    expect(seen).toHaveLength(1);
  });

  it('builds absolute artifact URLs', () => {
    const client = new FenixSpoonClient('http://server/');
    expect(
      client.artifactUrl({
        name: 'solution.vtk',
        content_type: 'model/vnd.vtk',
        size: 1,
        url: '/api/v1/jobs/j-1/artifacts/solution.vtk',
      }),
    ).toBe('http://server/api/v1/jobs/j-1/artifacts/solution.vtk');
  });

  it('exposes FenixSpoonError for instanceof checks', () => {
    expect(new FenixSpoonError('x', 500, null)).toBeInstanceOf(Error);
  });

  it('calls the global fetch with its own receiver', async () => {
    // Browsers throw "Illegal invocation" when `fetch` is called as a bare reference
    // rather than a method of `window`. Node is lenient, so assert the binding directly.
    const original = globalThis.fetch;
    let receiver: unknown = 'never called';
    Object.defineProperty(globalThis, 'fetch', {
      configurable: true,
      writable: true,
      value: function boundCheck(this: unknown) {
        receiver = this;
        return Promise.resolve(new Response('[]', { status: 200 }));
      },
    });
    try {
      await new FenixSpoonClient('http://server').listSolvers();
    } finally {
      Object.defineProperty(globalThis, 'fetch', {
        configurable: true,
        writable: true,
        value: original,
      });
    }
    expect(receiver).toBe(globalThis);
  });

  it('binds an injected fetch too, not just the global one', async () => {
    // Passing `window.fetch` through options is the natural thing to do, and it is
    // exactly as unbound as the global — without binding it would be invoked with the
    // client instance as its receiver and throw in a browser.
    let receiver: unknown = 'never called';
    const injected = function injectedFetch(this: unknown) {
      receiver = this;
      return Promise.resolve(new Response('[]', { status: 200 }));
    };
    await new FenixSpoonClient('http://server', {
      fetch: injected as unknown as typeof globalThis.fetch,
    }).listSolvers();
    expect(receiver).toBe(globalThis);
  });
});
