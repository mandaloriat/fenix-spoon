/**
 * The SDK against a real server. This is what proves the types and the protocol agree —
 * the fake-transport tests in `client.test.ts` can only prove the SDK is self-consistent.
 *
 * The server is booted by `tests/server.ts`; the whole suite skips when the Python
 * package isn't importable, so `npm test` works in a JS-only checkout.
 */

import { describe, expect, it } from 'vitest';
import WebSocketImpl from 'ws';

import { FenixSpoonClient, JobFailedError } from '../src/client.js';
import { type Domain2D, type Regions2D, fieldNames, fieldValues, isMesh2D } from '../src/types.js';
import { validateGeometry, validateJobEvent, validateJobResult } from '../src/validate.js';

const BASE_URL = process.env.FENIXSPOON_TEST_URL;
const describeLive = BASE_URL ? describe : describe.skip;

// Node's built-in WebSocket cannot decode a stream where the server negotiated
// `server_max_window_bits` below 15, which is exactly what uvicorn does by default —
// the socket opens and then dies with code 1006 having delivered nothing. The `ws`
// package handles it, so we inject it here and test against a *default* server rather
// than one specially configured for us. See the package README.
const NODE_WS = WebSocketImpl as unknown as typeof globalThis.WebSocket;

const AIRFOIL: Domain2D = {
  type: 'domain2d',
  bounds: [-1, -1, 2, 1] as [number, number, number, number],
  obstacle: {
    type: 'polygon2d' as const,
    points: [
      [0, 0],
      [0.35, 0.09],
      [1, 0],
      [0.35, -0.06],
    ] as [number, number][],
  },
};

const SOLENOID: Regions2D = {
  type: 'regions2d',
  bounds: [-0.06, -0.06, 0.06, 0.06] as [number, number, number, number],
  background: { mu_r: 1 },
  regions: [
    {
      name: 'core',
      shape: {
        type: 'polygon2d' as const,
        points: [
          [-0.01, -0.03],
          [0.01, -0.03],
          [0.01, 0.03],
          [-0.01, 0.03],
        ] as [number, number][],
      },
      material: { mu_r: 1000 },
    },
    {
      name: 'coil',
      shape: {
        type: 'polygon2d' as const,
        points: [
          [0.015, -0.03],
          [0.025, -0.03],
          [0.025, 0.03],
          [0.015, 0.03],
        ] as [number, number][],
      },
      material: { current_density: 5e6 },
    },
  ],
};

const FAST = { resolution: 32, iterations: 100, report_every: 20 };

describeLive('against a live server', () => {
  const client = new FenixSpoonClient(BASE_URL!, { WebSocket: NODE_WS });

  it('lists solvers with schemas the UI can render', async () => {
    const solvers = await client.listSolvers();
    const names = solvers.map((s) => s.name);
    expect(names).toContain('mock.laplace2d');
    expect(names).toContain('mock.magnetostatics2d');

    const mock = solvers.find((s) => s.name === 'mock.laplace2d')!;
    expect(mock.geometry_types).toEqual(['domain2d']);
    expect(mock.params_schema).toHaveProperty('properties.resolution');
  });

  it('runs a job end to end: submit, stream, result', async () => {
    const job = await client.submit({
      solver: 'mock.laplace2d',
      geometry: AIRFOIL,
      params: FAST,
    });

    const events = [];
    for await (const event of job.events()) {
      // Every event the server emits must satisfy the SDK's own validator.
      events.push(validateJobEvent(event));
    }
    expect(events[0]).toMatchObject({ type: 'status', status: 'running' });
    expect(events.at(-1)).toMatchObject({ type: 'status', status: 'done' });
    expect(events.some((e) => e.type === 'progress')).toBe(true);

    const result = validateJobResult(await job.result());
    expect(result.kind).toBe('grid2d');
    expect(fieldNames(result).sort()).toEqual(['psi', 'speed']);
    const [ny, nx] = result.kind === 'grid2d' ? result.data.shape : [0, 0];
    expect(fieldValues(result, 'psi')).toHaveLength(ny * nx);
  });

  it('wait() is the one-call path and reports progress', async () => {
    const job = await client.submit({
      solver: 'mock.laplace2d',
      geometry: AIRFOIL,
      params: FAST,
    });
    let progressSeen = 0;
    const result = await job.wait((event) => {
      if (event.type === 'progress') progressSeen += 1;
    });
    expect(progressSeen).toBeGreaterThan(0);
    expect(result.job_id).toBe(job.id);
  });

  it('handles mesh2d results and downloadable artifacts', async () => {
    const job = await client.submit({
      solver: 'mock.laplace2d',
      geometry: AIRFOIL,
      params: { ...FAST, output: 'mesh2d' },
    });
    const result = await job.wait();
    expect(isMesh2D(result)).toBe(true);
    if (!isMesh2D(result)) return;
    expect(result.data.triangles.length).toBeGreaterThan(0);

    expect(result.artifacts.length).toBeGreaterThan(0);
    const artifact = result.artifacts[0]!;
    const response = await fetch(client.artifactUrl(artifact));
    expect(response.ok).toBe(true);
    expect(await response.text()).toContain('# vtk DataFile');
  });

  it('carries regions2d geometry through to a magnetostatics solve', async () => {
    validateGeometry(SOLENOID); // the SDK accepts what the server accepts
    const job = await client.submit({
      solver: 'mock.magnetostatics2d',
      geometry: SOLENOID,
      params: { resolution: 32, iterations: 200 },
    });
    const result = await job.wait();
    expect(fieldNames(result).sort()).toEqual(['A', 'B', 'mu_r']);
  });

  it('cancels a running job', async () => {
    const job = await client.submit({
      solver: 'mock.laplace2d',
      geometry: AIRFOIL,
      params: { resolution: 256, iterations: 20000 },
    });
    await job.cancel();
    await expect(job.wait()).rejects.toBeInstanceOf(JobFailedError);
    expect((await job.status()).status).toBe('cancelled');
  });

  it('rejects a geometry the solver does not accept', async () => {
    await expect(
      client.submit({ solver: 'mock.laplace2d', geometry: SOLENOID }),
    ).rejects.toMatchObject({ name: 'FenixSpoonError', status: 422 });
  });

  it('reports an unknown solver as 404', async () => {
    await expect(
      client.submit({ solver: 'nope.nope', geometry: AIRFOIL }),
    ).rejects.toMatchObject({ name: 'FenixSpoonError', status: 404 });
  });

  it('replays history to a late subscriber', async () => {
    const job = await client.submit({
      solver: 'mock.laplace2d',
      geometry: AIRFOIL,
      params: FAST,
    });
    await job.wait(); // finish first, then subscribe
    const replayed = [];
    for await (const event of job.events()) replayed.push(event);
    expect(replayed.length).toBeGreaterThan(1);
    expect(replayed.at(-1)).toMatchObject({ type: 'status', status: 'done' });
  });
});
