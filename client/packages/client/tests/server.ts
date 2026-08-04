/**
 * Vitest global setup: boots a real Fenix Spoon server for the integration tests.
 *
 * Skipped (leaving the integration suite to skip too) when the server package isn't
 * importable — so `npm test` still works in a checkout without the Python side set up.
 * Set `FENIXSPOON_TEST_URL` to point at an already-running server instead.
 */

import { type ChildProcess, spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SERVER_DIR = join(dirname(fileURLToPath(import.meta.url)), '../../../../server');
const PORT = Number(process.env.FENIXSPOON_TEST_PORT ?? 8765);

let child: ChildProcess | undefined;
let dataDir: string | undefined;

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

  // A data directory of our own, thrown away afterwards. Without it the server falls back
  // to `/tmp/fenixspoon-jobs`, which is shared by every run on the machine — and the result
  // cache is content-addressed, so a job from *last* week's run answers this week's
  // submission. That is fine for a solve and quietly wrong for anything reading
  // `provenance`: the workspace test would get back the object ids of whichever run solved
  // it first and fail against its own. Found by running `npm test` twice.
  dataDir = mkdtempSync(join(tmpdir(), 'fenixspoon-sdk-'));

  child = spawn(
    'python3',
    ['-m', 'uvicorn', 'fenixspoon.main:app', '--port', String(PORT), '--log-level', 'warning'],
    {
      cwd: SERVER_DIR,
      stdio: 'ignore',
      detached: false,
      env: { ...process.env, FENIXSPOON_DATA_DIR: dataDir },
    },
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
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
  dataDir = undefined;
}
