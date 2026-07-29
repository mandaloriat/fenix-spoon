# Write a solver adapter

A solver is one class. The server knows nothing about FEniCSx, or about your physics —
it hands you a validated geometry, validated parameters and a context, and expects a
result back. Registering the class is the whole integration.

## The smallest thing that works

```python
# myphysics/heat.py
import numpy as np
from pydantic import BaseModel, Field

from fenixspoon.geometry import Domain2D
from fenixspoon.solvers.base import ProgressEvent, Solver, SolverContext, SolverResult
from fenixspoon.solvers.registry import register


@register
class SteadyHeat2D(Solver):
    name = "myphysics.heat2d"          # what a client passes as `solver`
    title = "Steady heat conduction"   # what a picker shows
    description = "Laplace on temperature with a fixed-temperature obstacle."
    geometry_types = ["domain2d"]      # geometry kinds this accepts

    class Params(BaseModel):
        resolution: int = Field(default=128, ge=16, le=512)
        wall_temperature: float = 1.0

    def solve(
        self, geometry: Domain2D, params: "SteadyHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        temperature = np.zeros((params.resolution, params.resolution))
        for iteration in range(1, 501):
            ctx.check_cancelled()
            # ... your update here ...
            if iteration % 50 == 0:
                ctx.progress(ProgressEvent(iteration=iteration, total=500))

        ny, nx = temperature.shape
        return SolverResult(
            kind="grid2d",
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {"T": temperature.ravel().tolist()},
                "mask": np.zeros(ny * nx, dtype=int).tolist(),
            },
            stats={"cells": float(ny * nx)},
        )
```

Import the module once at startup and `@register` does the rest: the solver appears in
`GET /api/v1/solvers`, its `Params` model is published as JSON Schema so a client can
build a form from it, and `POST /api/v1/jobs` will accept it.

`fenixspoon/solvers/mock_laplace.py` is the reference implementation and is written to
be read.

## The contract, in four points

**`solve` runs on a worker thread, or in another process entirely.** It must be
self-contained — no reaching for request state, no globals that assume one solve at a
time. Whether it runs in the API process or in a worker container is a deployment
choice, and your adapter should not be able to tell.

**Call `ctx.check_cancelled()` inside long loops.** Cancellation is cooperative: nothing
kills your solve. It is an `Event.is_set()` check, so once per iteration is fine, and a
solver that never checks simply cannot be cancelled.

**Call `ctx.progress()` at a sensible cadence** — every few hundred milliseconds of work,
not every iteration. Each event crosses to the event loop, is persisted, and is published
to subscribers; a thousand per second is a self-inflicted load test.

**Return the result, raise for failure.** An exception fails that job with your message
and does not touch the server. There is no partial-success path: a job is `done`,
`failed` or `cancelled`.

## Files that come back to the user

`ctx.artifact(name)` registers an output file and returns the path to write it to:

```python
if params.write_vtk:
    write_my_vtk(ctx.artifact("solution.vtk"), ...)
```

The name must be a bare filename — separators are rejected, so an artifact cannot escape
the job directory. It appears in the result envelope with a download URL. Registered
names are the only servable ones, which is what makes the endpoint safe.

## Declaring what a job will cost

The server refuses over-sized work at submit rather than letting the timeout kill it
half-way. That needs a cheap estimate from you:

```python
@classmethod
def estimate_cells(cls, geometry: Domain2D, params: "SteadyHeat2D.Params") -> int:
    return params.resolution ** 2
```

It must not mesh anything — it runs before the job is accepted. Return `None` (the
default) if you genuinely cannot say, and the job is admitted with the wall-clock timeout
as the backstop.

**Err high.** An under-estimate admits work that then exhausts the box; an over-estimate
costs a user one parameter change and a clear error message. The FEniCSx adapters
learned this the hard way — the equilateral-tiling formula `2A/h²` looks like a triangle
count but is really a *floor*, and Gmsh comes in about 35% above it.

## Depending on something that might not be installed

The FEniCSx adapters register only when `dolfinx` imports, so the same codebase runs in a
plain venv and in the FEniCSx image. Follow the same pattern for a heavy dependency:

```python
try:
    import mysolverlib  # noqa: F401
except ImportError:  # pragma: no cover - depends on the deployment
    pass
else:
    from . import my_adapter  # noqa: F401  - registers on import
```

`fenixspoon/solvers/__init__.py` does exactly this. A solver that is not installed is
simply absent from `GET /solvers`, which is what a client should be reading anyway.

## Testing it

Call `solve` directly with a `SolverContext` — no server needed:

```python
def test_it_solves(tmp_path):
    events = []
    ctx = SolverContext(progress_cb=events.append, artifact_dir=tmp_path)
    result = SteadyHeat2D().solve(GEOMETRY, SteadyHeat2D.Params(), ctx)
    assert result.kind == "grid2d"
    assert any(e.iteration > 0 for e in events)
```

Then run it once through the job API as well. That is not redundant: solves execute on a
worker thread there, which catches thread-hostile library behaviour a direct call never
will. Gmsh installing signal handlers that are illegal off the main thread was found
exactly this way, and only in the browser.
