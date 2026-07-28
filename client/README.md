# Client packages (roadmap M2 — not yet implemented)

This directory will host the npm workspace for the embeddable browser components:

| Package | Purpose |
|---|---|
| `@fenix-spoon/client` | Typed TS SDK for the [wire protocol](../docs/04-wire-protocol.md): job submission, WebSocket event streams with reconnection, result fetching. Zero UI. |
| `@fenix-spoon/geometry-2d` | Framework-agnostic custom element for parametric 2D profile editing: draggable points, splines, snapping/constraints, undo, emits `domain2d` geometry JSON. |
| `@fenix-spoon/viewer` | Field viewer custom element built on [vtk.js](https://kitware.github.io/vtk-js/): `grid2d` and `mesh2d` results, colormaps, contours, vector glyphs, probes. |

Design constraints (from [architecture](../docs/02-architecture.md)):

- **Custom elements**, so they embed identically in React, Vue, Svelte, or plain HTML.
- **Protocol-only coupling**: packages depend on the wire protocol, never on server internals.
- **Mock-first development**: everything must be developable against the NumPy mock solver.

Until these exist, the reference implementation is the zero-dependency demo in
[`examples/airfoil-2d/`](../examples/airfoil-2d/).
