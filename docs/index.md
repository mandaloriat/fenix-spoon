# Fenix Spoon

**A Swiss-army toolkit for building web-based engineering applications powered by
[FEniCSx](https://fenicsproject.org/).**

Everything needed to put a finite-element solver behind a web page: a ready-to-deploy
simulation server, a JSON contract for submitting jobs and streaming results, and
embeddable browser widgets for geometry input and field visualization.

The canonical use case: an engineer opens a page, drags the control points of a 2D airfoil
or a solenoid cross-section, presses *Run*, and watches the result appear live. No local
install, no desktop tooling — a browser talking to a FEniCSx server.

## Start where you are

<div class="grid cards" markdown>

- **[Embed the widgets](start-embed-widgets.md)**

    You have a front-end and want a geometry editor and a field viewer in it. Two custom
    elements and an SDK; no framework required.

- **[Deploy the server](start-deploy-server.md)**

    You want the simulation server running for a team, with API keys, quotas and worker
    containers.

- **[Write a solver adapter](start-write-a-solver.md)**

    You have physics of your own. A solver is one class; the server needs no changes to
    accept it.

- **[See it working first](gallery.md)**

    Four worked examples with screenshots — potential flow, magnetostatics, a heat sink whose
    controls the server generates, and a lift polar swept from the browser.

</div>

## What is actually built

| Piece | State |
|---|---|
| Simulation server: jobs, progress streaming, cancellation, budgets, persistence, auth | working |
| FEniCSx adapters for seven physics: potential flow, magnetostatics, steady and transient heat, elasticity, axisymmetric electrostatics, natural frequencies | validated against dolfinx 0.11 |
| Pure-NumPy mock solvers mirroring every one of them | working — the whole loop runs without FEniCSx |
| Wire protocol 1.15 with a shared conformance corpus | implemented, checked from both sides |
| Four browser packages: SDK, geometry editor, field viewer, curve plot | working |
| Distributed execution: arq workers behind Redis | working |
| The same core from a local process: JSON-RPC over stdio, MCP, a CLI and a Python API | working |

Milestones M1, M2 and M2.5 are complete and M3 is, apart from a Helm chart. The
[roadmap](03-roadmap.md) tracks the rest — and
[driving the same core from a local process](07-local-agent-interface.md) rather than a browser
is no longer among it: that document is now a record of what was built rather than a plan.

## The one design decision worth knowing up front

**The protocol is the product.** The widgets and the server are replaceable; the JSON
contract between them is what makes the pieces compose. A consequence you will meet
early: solvers are *declarative*. A client picks a named solver and passes typed
parameters — it never sends code. "FEniCS as a service" that accepts Python from the
client is remote code execution with extra steps, so this toolkit does not offer it.
Custom physics is added by [deploying an adapter](start-write-a-solver.md), server-side.
