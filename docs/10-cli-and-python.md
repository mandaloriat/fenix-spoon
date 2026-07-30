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
`fs.submit(...)` twice with the same inputs returns the same job the second time.

Identity is not bypassed in-process either: `open_workspace(principal="alice")` and
`principal="bob"` cannot see each other's objects.
