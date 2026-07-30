# @fenix-spoon/client

Typed client for the [Fenix Spoon](https://github.com/mandaloriat/fenix-spoon) wire protocol:
discover solvers, submit jobs, stream progress, fetch results. No UI, no rendering — just the
protocol, so it works in a browser, in Node, or in a worker.

```bash
npm install @fenix-spoon/client
```

## Usage

```ts
import { FenixSpoonClient } from '@fenix-spoon/client';

const client = new FenixSpoonClient('http://localhost:8000');

const job = await client.submit({
  solver: 'mock.laplace2d',
  geometry: {
    type: 'domain2d',
    bounds: [-1, -1, 2, 1],
    obstacle: { type: 'polygon2d', points: [[0, 0], [0.35, 0.09], [1, 0], [0.35, -0.06]] },
  },
  params: { resolution: 128 },
});

const result = await job.wait((event) => {
  if (event.type === 'progress') console.log(event.iteration, event.residual);
});

console.log(result.kind); // 'grid2d' | 'mesh2d' | 'series1d'
```

Prefer to drive the loop yourself? `job.events()` is an async iterator:

```ts
for await (const event of job.events()) {
  if (event.type === 'progress') updateProgressBar(event);
}
const result = await job.result();
```

### Discovering what the server can do

```ts
const solvers = await client.listSolvers();
// each entry carries a JSON Schema for its params — drive your forms from it
```

### Cancelling

```ts
await job.cancel();          // cooperative; the job ends in the `cancelled` state
```

`job.wait()` throws `JobFailedError` if the job fails or is cancelled. HTTP-level problems
throw `FenixSpoonError`, which carries `status` and the server's `detail`.

### Aborting a stream

```ts
const controller = new AbortController();
for await (const event of job.events({ signal: controller.signal })) { /* ... */ }
```

### Validating payloads

The package ships runtime validators mirroring the server's pydantic rules — useful when you
accept geometry from user input and want a clear error before the round trip:

```ts
import { validateGeometry, ProtocolValidationError } from '@fenix-spoon/client';

try {
  validateGeometry(userInput);
} catch (error) {
  if (error instanceof ProtocolValidationError) showMessage(error.message);
}
```

They enforce the same constraints the server does, including rejecting self-intersecting
polygons (which can hang the mesher) and partially overlapping material regions.

## Reconnection

The server replays a job's full event history whenever a client connects. That makes a dropped
connection cheap to recover from, and `events()` does it for you: on an unexpected close it
reconnects, skips the events it already delivered, and continues. Tune with `maxReconnects`
and `reconnectDelayMs`.

## ⚠️ Using this from Node

**Browsers work with a default Fenix Spoon server. Node's built-in `WebSocket` does not.**

uvicorn negotiates per-message deflate with `server_max_window_bits=12`, and Node's built-in
WebSocket (undici) cannot decode a stream with a window size below 15 — the socket opens and
then dies with code 1006 having delivered zero events. Nothing in the handshake fails, so the
symptom is silence.

Two fixes, both tested:

```ts
// Recommended: inject the `ws` package, which handles the negotiated window size.
import WebSocket from 'ws';
const client = new FenixSpoonClient(url, { WebSocket: WebSocket as never });
```

or start the server with compression off, at the cost of much larger frames:

```bash
uvicorn fenixspoon.main:app --ws-per-message-deflate false
```

This package's own integration tests take the first route, so they exercise a
default-configured server.

## API

| Export | What it is |
|---|---|
| `FenixSpoonClient` | Entry point: `listSolvers`, `submit`, `job`, `status`, `result`, `cancel`, `events`, `artifactUrl` |
| `Job` | Handle returned by `submit`: `events()`, `wait()`, `result()`, `cancel()`, `status()` |
| `FenixSpoonError`, `JobFailedError`, `ProtocolValidationError` | Error types |
| `validateGeometry`, `validateJobRequest`, `validateJobEvent`, `validateJobResult` | Runtime validators |
| `isGrid2D`, `isMesh2D`, `isTerminal`, `fieldNames`, `fieldValues` | Narrowing and field helpers |
| Types | `Geometry`, `Domain2D`, `Regions2D`, `SolverInfo`, `JobEvent`, `JobResult`, … |

Full protocol reference: [docs/04-wire-protocol.md](../../../docs/04-wire-protocol.md).

## License

MIT
