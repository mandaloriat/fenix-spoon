# Client packages

npm workspace for the Fenix Spoon browser packages.

| Package | Status | Purpose |
|---|---|---|
| [`@fenix-spoon/client`](packages/client) | ✅ implemented | Typed SDK for the [wire protocol](../docs/04-wire-protocol.md): solver discovery, job submission, WebSocket progress streams with reconnection, results, artifacts, runtime validators. Zero UI. |
| [`@fenix-spoon/geometry-2d`](packages/geometry-2d) | ✅ implemented | `<fs-geometry-2d>`: SVG-based parametric 2D profile editor. Draggable points, polygon and Catmull-Rom spline modes, undo/redo, full keyboard operation. Emits `domain2d` geometry JSON. |
| `@fenix-spoon/viewer` | 📋 planned (#9) | Field viewer custom element built on [vtk.js](https://kitware.github.io/vtk-js/): `grid2d` and `mesh2d` results, colormaps, contours, glyphs, probes. |

## Working on them

```bash
cd client
npm install
npm test          # every package
npm run build
npm run typecheck
```

`@fenix-spoon/client`'s test suite has three layers: conformance tests reading the shared
fixture corpus in [`protocol/fixtures/`](../protocol/fixtures) (the same files
`server/tests/test_protocol_fixtures.py` reads, so the two sides cannot drift), unit tests
over a fake transport, and integration tests against a real server booted by the harness.
The integration layer skips itself when the Python package isn't importable, so `npm test`
works in a JS-only checkout.

`@fenix-spoon/geometry-2d` renders with SVG rather than canvas, which is what lets its tests
drive real pointer and keyboard events in jsdom — and what gives the widget keyboard operation
and screen-reader labels for free. Tests resolve `@fenix-spoon/client` from source via a Vite
alias, so they don't depend on build order; `npm run build` does build the packages in
dependency order, since the emitted `.d.ts` must reference the sibling the way consumers do.

## Design constraints

Carried over from the [architecture](../docs/02-architecture.md):

- **Custom elements** for the UI packages, so they embed identically in React, Vue, Svelte or
  plain HTML.
- **Protocol-only coupling**: packages depend on the wire protocol, never on server internals.
- **Mock-first development**: everything must be developable against the NumPy mock solvers,
  with no FEniCSx installed.

Until the widget packages exist, the reference implementations are the zero-dependency demos in
[`examples/`](../examples).
