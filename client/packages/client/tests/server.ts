/**
 * Vitest global setup: boots a real Fenix Spoon server for the integration tests.
 *
 * Skipped (leaving the integration suite to skip too) when the server package isn't
 * importable — so `npm test` still works in a checkout without the Python side set up.
 * Set `FENIXSPOON_TEST_URL` to point at an already-running server instead.
 */

import { type ChildProcess, spawn, spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SERVER_DIR = join(dirname(fileURLToPath(import.meta.url)), '../../../../server');
const PORT = Number(process.env.FENIXSPOON_TEST_PORT ?? 8765);

let child: ChildProcess | undefined;

function pythonHasServer(): boolean {
  const probe = spawnSync('python3', ['-c', 'import fenixspoon'], {
    cwd: SERVER_DIR,
    stdio: 'ignore',
  });
  return probe.status === 0;
}

async function waitForReady(url: string, timeoutMs = 30_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${url}/api/v1/solvers`);
      if (response.ok) return true;
    } catch {
      /* not up yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return false;
}

export async function setup(): Promise<void> {
  if (process.env.FENIXSPOON_TEST_URL) return; // caller supplied a server
  if (!pythonHasServer()) {
    console.warn('[integration] fenixspoon not importable — integration tests will skip');
    return;
  }

  child = spawn(
    'python3',
    ['-m', 'uvicorn', 'fenixspoon.main:app', '--port', String(PORT), '--log-level', 'warning'],
    { cwd: SERVER_DIR, stdio: 'ignore', detached: false },
  );

  const url = `http://127.0.0.1:${PORT}`;
  if (await waitForReady(url)) {
    process.env.FENIXSPOON_TEST_URL = url;
  } else {
    console.warn('[integration] server did not become ready — integration tests will skip');
    child.kill();
    child = undefined;
  }
}

export async function teardown(): Promise<void> {
  child?.kill();
  child = undefined;
}
