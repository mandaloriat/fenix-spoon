# Load test

`server/loadtest.py` runs N concurrent clients, each submitting jobs, holding a WebSocket
for progress and fetching the result — the same sequence a browser performs. It uses only
`httpx` and `websockets`, which the server already depends on, because a load test that
needs another toolchain installed is a load test nobody runs.

```bash
make loadtest                                  # 25 clients x 2 jobs against a fresh server
make loadtest CLIENTS=50 JOBS=4                # heavier
python server/loadtest.py --url http://host:8000 --clients 25 --jobs 2 --json out.json
```

It exits non-zero if any job fails, so it can gate a deploy.

## What it measures

| Metric | Why it is the one to watch |
|---|---|
| submit p50/p95 | What a user feels as "the button is stuck" |
| time to first event | A UI showing nothing for seconds looks broken even when the solve is fine |
| inter-event gap p95 | The longest silence on one progress stream. Progress hops from a worker thread onto the event loop, so a starved loop shows up here first |
| dropped streams | Sockets closed before a terminal event — each one is a client that would have hung |
| API RSS | Results are held in memory *and* written to disk; growth under load is the likeliest thing to fall over |

## Results

Measured on **4 cores, 8 GB, Linux, Python 3.11**, with the load generator running on the
same box — so these are conservative: a real client is not stealing the server's CPU.
Reproduce with `python server/loadtest.py --clients 25 --jobs 2` (default params:
`resolution=96, iterations=400, report_every=50` on `mock.laplace2d`).

| clients | workers | jobs/s | submit p50/p95 (ms) | first event p95 (ms) | gap p95 (ms) | failed | RSS peak |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 1 | 11.9 | 279 / 1298 | 1449 | 565 | 0 | 94 MB |
| 25 | 2 | 9.2 | 112 / 1229 | 1409 | 1774 | 0 | 96 MB |
| 25 | 4 | 5.0 | 70 / 1291 | 1445 | 3581 | 0 | 97 MB |
| 25 | 8 | 5.1 | 76 / 1235 | 1396 | 2835 | 0 | 97 MB |
| 50 | 4 | 4.9 | 250 / 2647 | 2994 | 7627 | 0 | 126 MB |

**Nothing fell over.** Across every row — 100 jobs and 100 concurrent WebSockets at the top
end — zero jobs failed and zero streams dropped. Memory tracks concurrency rather than
total work: 94 MB at 25 clients, 126 MB at 50, flat within a run.

### The mock solver gets *slower* with more workers

Throughput at 25 clients falls from 11.9 jobs/s on one worker to 5.0 on four. That is not a
bug, it is the GIL: `mock.laplace2d` is a Jacobi loop of small NumPy operations on a 96×64
grid, so each operation releases the GIL only briefly and the interpreter spends its time
handing the lock around. The work is fixed; serializing it is optimal.

Note what does *not* change: submit p95 sits at ~1.3 s in every row. Submit latency at this
concurrency is 25 clients arriving at once against one event loop, not the pool size.

### The FEniCSx solver gets *faster*, which is why the default is the core count

Same test with `dolfinx.potential_flow2d` (8 clients × 2 jobs, `mesh_size=0.035`):

| workers | jobs/s | wall | submit p95 | gap p95 |
|---:|---:|---:|---:|---:|
| 1 | 2.0 | 7.9 s | 287 ms | 3165 ms |
| 4 | 2.7 | 5.9 s | 224 ms | 1437 ms |

Four workers is 33% faster and the progress stream is smoother, because PETSc and the Gmsh
mesher are C++ that releases the GIL properly. Real solves parallelize; the pure-Python
stand-in does not.

`FENIXSPOON_MAX_WORKERS` therefore defaults to the core count — right for the solver
production actually runs. Lower it if your workload is Python-heavy, and remember the mock
solvers are the atypical case, not the representative one.

## The tested envelope

On 4 cores, with the mock solver at these settings:

- **50 concurrent clients / 100 concurrent WebSocket streams**: no failures, no drops,
  126 MB peak. Submit p95 2.6 s.
- **25 concurrent clients**: submit p95 1.3 s, first event under 1.5 s.
- Beyond that, the limit you meet first is submit latency, not correctness. Fix it by
  adding API replicas (they are stateless apart from the job store) rather than cores.

Two things this does **not** yet cover, and should not be read as covering:

- **Sustained load.** Every run here is seconds, not hours. A leak with a slow slope, or
  the retention sweep interacting with load, would not show up.
- **Multiple API processes.** One `uvicorn` worker. Running several against one SQLite
  database is untested, and jobs live in the process that accepted them — a second replica
  cannot stream progress for a job the first is running. That is the constraint the
  out-of-process backend (#12) removes; until then, run one API process per job store.
