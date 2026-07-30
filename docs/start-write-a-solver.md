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

## Your physics on FEniCSx: where the UFL goes

The example above is NumPy because it has to run in a plain virtualenv. Nothing changes for a
real FEniCSx adapter — `solve` is ordinary Python, and **your variational form is written
there, in UFL, exactly as you would write it in a script**. The server never sees it.

That distinction is worth being precise about, because it is easy to read the security stance
backwards. Fenix Spoon refuses to accept UFL *over the wire* — a client cannot post a form and
have it compiled — because that is arbitrary code execution by another name (a sandboxed,
opt-in mode is the exploratory issue
[#24](https://github.com/mandaloriat/fenix-spoon/issues/24)). It does not refuse UFL in an
adapter. **You** are the author of the deployment; the anonymous caller is not. So the model an
application needs is: write the physics once, in Python, and expose it as a *named capability*
with typed parameters.

`dolfinx_poisson.py` is the reference, and its core is four lines that any FEniCSx user will
recognise:

```python
V = fem.functionspace(msh, ("Lagrange", 1))
u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
a = ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx
L = fem.Constant(msh, dolfinx.default_scalar_type(0.0)) * v * ufl.dx
problem = LinearProblem(a, L, bcs=bcs, petsc_options={"ksp_type": "preonly", "pc_type": "lu"})
```

Everything else in that file is the part you would have written anyway and now get to reuse:
meshing the protocol's geometry with Gmsh (`_build_mesh`, and `gmsh_session` for the
initialise/finalise pair that must not leak), reporting stages through `ctx.progress`, sampling
the solution onto a grid or emitting the triangulation directly, and writing the VTK.

Three practical notes for a FEniCSx adapter specifically:

**Gate the import.** Register the adapter only if `dolfinx` imports, using the pattern under
[*Depending on something that might not be installed*](#depending-on-something-that-might-not-be-installed).
That is what lets the same codebase run in a plain venv (where your adapter is simply absent
from `GET /solvers`) and in the FEniCSx image.

**Mesh in `solve`, estimate in `estimate_cells`.** The estimate runs before the job is
accepted and must not mesh anything. For a triangular mesh of a 2-D region, `2A/h²` is the
equilateral-tiling floor — Gmsh lands about 35% above it, so err high.

**MPI is a deployment question, not an adapter one.** The adapters here run on
`MPI.COMM_SELF` — one solve, one process — and horizontal scale comes from running more
worker containers (`docker-compose.workers.yml`). A genuinely parallel single solve is a
different shape and nothing in the protocol forbids it, but no adapter here does it yet.

If the geometry you need is not `domain2d` or `regions2d`, that is the one place where adding
physics reaches beyond your own file: a new geometry kind is a protocol change, because the
browser editor and the JS validators have to know it too. Adding a *solver* over an existing
geometry kind touches nothing outside your module.

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

## Returning a curve as well as a field

If your solve produces a curve — a surface distribution, a sweep, a convergence history — put it
in `result.series` rather than inventing keys in `stats` or shipping an artifact for it. One solve
may legitimately answer both questions, which is why this is a list beside `data` and not a
different result kind:

```python
from fenixspoon.series import Series1DData, SeriesAxis, SeriesTrace

return SolverResult(
    kind="grid2d",
    data=data,
    series=[
        Series1DData(
            name="convergence",
            description="Residual against sweep count.",
            x=SeriesAxis(name="sweep", unit="1", values=sweeps),
            traces=[SeriesTrace(name="residual", unit="1", values=residuals)],
        )
    ],
)
```

Units are required here and not on a field, because a curve is drawn with axis labels and the
client has no other way to know what the axis is. Give each trace its own `x` when they are
genuinely sampled differently; otherwise share one. And keep it small — the models refuse a
series large enough to be the field arrays under a different name, which is the whole reason the
level is safe to serve. The [wire protocol](04-wire-protocol.md#one-dimensional-results) has the
shape and the limits.

## Describing yourself to a caller that is not a form

Everything above makes your solver *runnable*. This makes it *discoverable*: the
[progressive discovery](04-wire-protocol.md#progressive-discovery) operations report
whatever you declare here, so a caller can decide whether to run you before it does.

```python
from fenixspoon.solvers.base import (
    ArtifactSpec, Assumption, CapabilityExample, MetricSpec,
)


class SteadyHeat2D(Solver):
    name = "myphysics.heat2d"
    title = "Steady heat conduction"
    physics = "heat-conduction"      # coarse tag a caller filters on
    availability = "mock"            # or "fenicsx" — is this the real thing?
    requires = ["mysolverlib"]       # informational; versions get reported

    metrics = [
        MetricSpec(name="t_max", unit="degC", description="Peak temperature.",
                   field="T", reduction="max"),
    ]
    assumptions = [
        Assumption(name="steady_state",
                   statement="No time derivative: every temperature is the equilibrium "
                             "the device settles at, reached after a time this cannot tell you.",
                   excludes=["thermal_time_constant", "transient_temperature"]),
        Assumption(name="two_dimensional",
                   statement="A cross-section of a body infinitely long in z."),
    ]
    artifacts = [
        ArtifactSpec(name="solution.vtk", content_type="model/vnd.vtk",
                     description="Full field, opens in ParaView.", when="write_vtk"),
    ]
    examples = [
        CapabilityExample(title="fast preview", description="Sub-second sanity check.",
                          params={"resolution": 64}),
    ]
```

**Every one of these has a default, so an existing adapter keeps working untouched** — it
reports `unspecified` where it has not said, and stays fully usable. They are plain class
attributes in the spirit of `Params`, not a registration framework.

Three things to get right:

**Metrics are the engineering *answer*; `stats` is what the solve *cost*.** Peak
temperature is a metric, `seconds` and `cells` are stats. Keeping them apart is what lets
an operator size a machine and an engineer make a decision from the same result.

**Declare what your model *assumes*, and say what that puts out of reach.** Every other
attribute here describes what your solver does; `assumptions` describes where it stops being
true, which is what a caller most needs before trusting your numbers. The `excludes` list is
the part that earns its keep: it turns *"can this tell me about drag?"* into a definite **no**
rather than a plausible zero. Where an assumption has a numeric edge and the quantity is
computable, add `quantity`, `limit` and `comparator` and it becomes checkable rather than
merely readable. This one attribute defaults to empty and *should not stay that way* — every
physical model assumes something, so an empty list reads as undeclared.

**Name a `field` and a `reduction` where the metric really is a reduction of one — and
then write no code for it.** The runtime computes those after your `solve` returns, from
the declaration itself, so four adapters do not each contain the same `float(T.max())`.
It also makes the declaration checkable rather than decorative: the test suite runs a
solve and fails if the field is not in the payload.

**A metric that is an integral over a boundary names `boundary` instead of `field`.** Lift and
moment are integrals over the body surface, gap force over an air gap, wall heat flux over a
wetted face — none of them is a reduction of a field over the domain. Declaring
`boundary="body"` says which, and it is checked the same way: a solve must return the metric.

What you *do* compute is the derived and boundary ones, in `result.metrics`. `t_rise` needs
`t_ambient` and `cp_min` needs `u_inf` — both parameters, and a parameter is not in the
result payload, so nothing generic can reach them:

```python
return SolverResult(
    kind="grid2d",
    data=data,
    stats={"cells": float(ny * nx)},
    metrics={"t_rise": float(temperature.max() - params.t_ambient)},
    converged=residual < 1e-9,     # None if your solve does not iterate
    residual=residual,
    warnings=[] if converged else ["stopped at the iteration cap"],
)
```

`converged`, `residual` and `warnings` are the [diagnostics
level](04-wire-protocol.md#compact-results). They exist because `stats` is typed
`dict[str, float]`, so a flag or a warning string had nowhere to go — a solve that
quietly stopped at its iteration cap could only say so to a client that happened to be
watching the progress stream.

If you ship a second adapter for physics that already exists — the way every mock solver
here has a FEniCSx twin — **declare the same metric names**. Cross-validated adapters that
answer the same question under different names are interchangeable in the gallery and not
in a caller's code, and `test_paired_adapters_declare_the_same_metrics` enforces it.

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
