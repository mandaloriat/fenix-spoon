# CLI and Python API

Two more adapters over the same core, for the two callers that are neither a browser nor an
agent: a shell, and a script.

Added in [issue #50](https://github.com/mandaloriat/fenix-spoon/issues/50).

## The CLI

`fenix-spoon <noun> <verb>` over the same operation set every transport exposes.

```console
$ fenix-spoon capability list
name                   title                         physics          availability
---------------------  ----------------------------  ---------------  ------------
mock.laplace2d         Potential flow (mock, NumPy)  potential-flow   mock
mock.magnetostatics2d  Magnetostatics (mock, NumPy)  magnetostatics   mock
mock.heat2d            Heat sink (mock, NumPy)       heat-conduction  mock
```

**Every command dispatches through the JSON-RPC method table**, exactly as the
[MCP adapter](09-mcp.md) does. So `--json` output is not merely *comparable* to what an agent
gets over [JSON-RPC](08-json-rpc.md) — it is the same value, field for field, and a test
asserts equality rather than trusting it. That is what makes the CLI worth reaching for as
**the debugging surface for exactly what an agent sees**: a CLI that reformatted, rounded or
renamed anything would be showing you something the protocol does not say.

The one difference is encoding, not content: this pretty-prints and the transport frames
compactly, so the two write the same document differently. Pretty-printing is worth keeping
precisely because the reason to run it is a human reading what an agent received; pipe it
through `jq` and the two are indistinguishable.

Human-readable output is a *rendering* of that JSON, never a different answer. There is no
command that computes something for humans a machine caller cannot get.

### Documents come in on stdin

`object create`, `object patch` and an inline `job submit` read their JSON body from standard
input. A file redirect is how a shell passes a document, and putting a geometry on a command
line is how quoting bugs happen.

```console
$ fenix-spoon object create geometry --json < airfoil.json
$ echo '[{"op":"replace","path":"/params/resolution","value":128}]' \
    | fenix-spoon object patch design:d-1
```

The document an inline `job submit` reads is the whole request, so a load case travels with
the geometry it applies to:

```console
$ cat cantilever.json
{"geometry": {"type": "regions2d", "bounds": [0, -0.05, 1, 0.05], "...": "...",
              "boundaries": [{"name": "root", "select": {"type": "near", "axis": "x", "value": 0}},
                             {"name": "tip",  "select": {"type": "near", "axis": "x", "value": 1}}]},
 "conditions": {"root": {"fixed": 1}, "tip": {"traction_y": -1.0e6}}}
$ fenix-spoon job submit --solver mock.elasticity2d --json < cantilever.json
```

### `job submit` waits

**By default, `job submit` and `study run` block until the work is finished.** That is not a
convenience — it is the only behaviour that is not broken. With the in-process backend a
solve runs on this process's thread pool, so a command that submitted and exited would kill
the work it just started and leave a row saying `running`, which the next startup reconciles
to *failed*. Fire-and-forget from a one-shot process is a shell script's most natural
instinct and, here, its most reliable way to get nothing.

`--detach` skips the wait. It is meaningful exactly when the backend is *not* local — with
`FENIXSPOON_REDIS_URL` set and workers running, the solve outlives the command by design.
With the in-process backend it does what it says, and the job dies with the command.

### A worked example: a lift polar in four commands

A [sweep](07-local-agent-interface.md#study-kinds) is a study, so it is three objects and two
verbs. Nothing here is specific to aerodynamics — the same four commands over `mesh_size` and
`sigma_vm_max` are a mesh study of a bracket.

```console
$ fenix-spoon object create geometry < airfoil.json
ref: geometry:g-1
…                                                     # the whole object is echoed back
$ fenix-spoon object create design < design.json      # solver, geometry, base params
ref: design:d-1
$ fenix-spoon object create study < polar.json        # the sweep below
ref: study:s-1
$ fenix-spoon study run study:s-1
jobs:
  - j-3b92ec88fd96
…
submitted: 6
reused: 0
refused: 0
```

```json title="polar.json"
{
  "kind": "sweep",
  "design": "design:d-1",
  "axes": [{ "parameter": "alpha", "values": [-6, -3, 0, 3, 6, 9] }],
  "metrics": ["c_l", "c_m_c4"]
}
```

```console
$ fenix-spoon study get study:s-1
study: study:s-1@1
kind: sweep
design: design:d-1@1
parameters:
  - alpha
solver: mock.laplace2d
points:
  job_id          status  cached  metrics.c_l  metrics.c_m_c4  error  values.alpha
  --------------  ------  ------  -----------  --------------  -----  ------------
  j-3b92ec88fd96  done    no      -0.433738    -0.00537753     -      -6
  j-a898e0fc2318  done    no      -0.0732739   -0.0221928      -      -3
  j-0b288edbb693  done    no      0.287392     -0.0395583      -      0
  j-0e5759694d02  done    no      0.647269     -0.0572837      -      3
  j-1cd1ff80b1d0  done    no      1.00537      -0.0751749      -      6
  j-3380e696317f  done    no      1.36072      -0.0930359      -      9
curves:
  name    description           x
  ------  --------------------  -
  c_l     c_l against alpha     -
  c_m_c4  c_m_c4 against alpha  -
complete: yes
```

That is a lift polar, and it is worth checking rather than admiring: `c_l` rises by 1.79 over
15°, a slope of 6.9 per radian against thin-airfoil theory's 2π, and it crosses zero near
−2.4°, which is where a section with this much camber should. The `curves` rows print as names
here because a curve is a list and the table spreads maps, not lists — `--json` is where the
numbers are, in the `Series1DData` shape `<fs-plot>` takes.

`study run` waits, for the reason below. Run the same sweep again and it reports `submitted:
0, reused: 6` — every point is a content-addressed cache hit — so widening a polar costs only
the angles you added.

### The same question, searched instead of tabulated

The polar above crosses zero somewhere between −3° and 0°. An
[optimization](07-local-agent-interface.md#optimization) finds where, on the same design,
without tabulating anything either side of it.

```json title="trim.json"
{
  "design": "design:d-1",
  "parameter": "alpha",
  "bounds": [-10, 10],
  "objective": { "metric": "c_l", "sense": "target", "target": 0.0 },
  "max_evaluations": 12,
  "tolerance": 0.02
}
```

```console
$ fenix-spoon object create optimization < trim.json
ref: optimization:o-1
…
$ fenix-spoon optimize run optimization:o-1
optimization: optimization:o-1@1
design: design:d-1@1
solver: mock.laplace2d
parameter: alpha
objective:
  metric: c_l
  sense: target
  target: 0
evaluations:
  iteration  value      job_id          status  cached  metric       objective    error
  ---------  ---------  --------------  ------  ------  -----------  -----------  -----
  0          -2.36068   j-c571ee49563b  done    no      0.00360304   1.29819e-05  -
  1          2.36068    j-df6cb9686eb3  done    no      0.570692     0.32569      -
  2          -5.27864   j-8d8538ae935f  done    no      -0.347135    0.120503     -
  3          -0.557281  j-1dd14576c5bd  done    no      0.220424     0.0485869    -
  …
  10         -2.42279   j-beab6508a532  done    no      -0.00386598  1.49458e-05  -
best:
  iteration: 0
  value: -2.36068
  metric: 0.00360304
bracket:
  - -2.42279
  - -2.26018
evaluations_spent: 11
stopped: converged
```

Eleven solves to locate the zero-lift angle at −2.36°, inside a bracket of 0.16° — and the
sweep above puts the crossing between −3° and 0°, which is the same answer at lower
resolution. **`best` is iteration 0 here**, which is the reason a report carries a bracket as
well: the first probe happened to land almost on the answer, and "where the lowest value was
seen" and "where the minimum is known to be" are different claims. A search stopped by its
budget rather than its tolerance would show the difference more starkly.

Note what `optimize run` does *not* have: a `--detach`. `job submit` and `study run` take one
because there is a moment when the work is accepted and not yet done. A search has no such
moment — choosing the next point *is* the waiting — so the call returns the finished
trajectory.

### Exit codes

A script branches without parsing prose.

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | something else went wrong |
| `2` | usage error (argparse) |
| `3` | invalid request — fix the arguments |
| `4` | not found — no such capability, job, object or artifact |
| `5` | conflict — not finished, already finished, patch changed nothing |
| `6` | over quota — the payload says whether waiting helps |
| `7` | gone — the job is there and its field arrays are not |

They are derived from the [JSON-RPC error codes](08-json-rpc.md#errors), so the distinction a
shell sees is the distinction an agent sees.

## The Python API

A supported in-process entrypoint, replacing "import the internals and hope". It returns the
same typed models every other transport carries, not dicts.

```python
from fenixspoon import local

with local.open_workspace() as fs:
    geometry = fs.create("geometry", {"type": "domain2d", ...})
    design = fs.create("design", {"solver": "mock.laplace2d", "geometry": geometry.ref})

    job = fs.submit(design=design.ref).wait()
    print(job.metrics())                      # {'speed_max': 1.29, 'c_l': 0.09, ...}
    print(job.query("psi", "max").result)      # one bounded question, no arrays

    fs.patch(design.ref, [{"op": "replace", "path": "/params/resolution", "value": 128}])
    print(fs.submit(design=design.ref).wait().metrics())
```

`open_workspace()` defaults to the same directory everything else uses, so a design created
here is the one the CLI and an MCP host see.

### It owns an event loop, and that is the interesting part

The core is async and a notebook is not. There are three ways to bridge that and only one is
usable from a REPL:

1. **Make every method a coroutine.** Correct — and it makes the simplest use, solve
   something and print a number, require the caller to understand `asyncio.run`. That is
   precisely the audience that does not want to.
2. **Wrap each call in its own `asyncio.run`.** This is the trap. The in-process backend
   completes a solve through callbacks on the loop that *submitted* it, so a second
   `asyncio.run` watches a status that can never move. It appears to work for everything
   except actually finishing a job.
3. **Own one loop, on a background thread, for the session's lifetime.** Submit and wait are
   ordinary blocking calls, the solve completes on the loop that started it, and a caller can
   submit, do something else, and wait later — which is the notebook shape.

Three it is. The loop is a daemon thread, so an interpreter that exits without closing the
session is not held open by it; `close()` (or the context manager) stops it deterministically.

### It is not a second implementation

Every method is a call into the same core with a loop around it where one is needed. The
validation, the errors, the object ids, the quotas and the result cache are the ones every
other transport gets, because they are the same objects — which is why
`fs.submit(...)` twice with the same inputs returns the same job the second time — and the
load case is one of those inputs, so two load cases on one shape are two jobs rather than one
answer served to both.

Identity is not bypassed in-process either: `open_workspace(principal="alice")` and
`principal="bob"` cannot see each other's objects.
