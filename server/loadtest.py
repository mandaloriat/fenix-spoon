#!/usr/bin/env python3
"""Load test: N concurrent clients, each submitting jobs and holding a WebSocket (#16).

Run it against a server that is already up:

    python server/loadtest.py --url http://127.0.0.1:8000 --clients 25 --jobs 2

or via ``make loadtest``, which starts a server, runs the test and stops it again.

It deliberately uses only what the server already depends on — ``httpx`` and
``websockets`` — rather than locust or k6. A load test nobody can run because it needs
another toolchain installed is a load test nobody runs.

What it measures, and why each one:

- **submit latency** — the API's responsiveness while the executor is saturated. This is
  the number a user feels as "the button is stuck".
- **time to first event** — how long the progress stream takes to say anything. A UI that
  shows nothing for seconds looks broken even when the solve is fine.
- **inter-event gap** — the largest silence between two progress events on one stream.
  Progress is marshalled from a worker thread onto the event loop, so a starved loop
  shows up here before it shows up anywhere else.
- **dropped streams** — sockets that closed before a terminal event. Each one is a client
  that would have hung waiting.
- **API process RSS** — sampled throughout. Results are held in memory *and* written to
  disk, so growth under load is the thing most likely to fall over first.
"""

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

GEOMETRY = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [0.8, 0.05], [1.0, 0.0], [0.8, -0.03], [0.35, -0.06]],
    },
}


@dataclass
class JobRun:
    """One job, from submit to result fetched, as seen by the client."""

    submit_ms: float = 0.0
    first_event_ms: float = 0.0
    terminal_ms: float = 0.0
    result_ms: float = 0.0
    events: int = 0
    max_gap_ms: float = 0.0
    gaps_ms: list[float] = field(default_factory=list)
    dropped: bool = False
    error: str | None = None


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
        "max": round(ordered[-1], 1),
    }


def rss_mb(pid: int) -> float | None:
    """Resident set size of a process, straight from /proc — no psutil dependency."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


async def sample_rss(pid: int, stop: asyncio.Event, out: list[float]) -> None:
    while not stop.is_set():
        value = rss_mb(pid)
        if value is not None:
            out.append(value)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except TimeoutError:
            pass


async def run_one_job(client: httpx.AsyncClient, args, ws_base: str) -> JobRun:
    run = JobRun()
    body = {"solver": args.solver, "geometry": GEOMETRY, "params": args.params}
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    started = time.perf_counter()
    try:
        response = await client.post("/api/v1/jobs", json=body, headers=headers)
    except Exception as exc:
        run.error = f"submit failed: {type(exc).__name__}: {exc}"
        return run
    run.submit_ms = (time.perf_counter() - started) * 1000
    if response.status_code != 202:
        run.error = f"submit HTTP {response.status_code}: {response.text[:120]}"
        return run
    job_id = response.json()["job_id"]

    # Subscribe as a browser would: one socket per job, held to the terminal event.
    url = f"{ws_base}/api/v1/jobs/{job_id}/events"
    if args.api_key:
        url += f"?api_key={args.api_key}"
    last = time.perf_counter()
    terminal_status = None
    try:
        async with websockets.connect(url, max_size=None) as socket:
            async for message in socket:
                now = time.perf_counter()
                event = json.loads(message)
                run.events += 1
                if run.first_event_ms == 0.0:
                    run.first_event_ms = (now - started) * 1000
                else:
                    gap = (now - last) * 1000
                    run.gaps_ms.append(gap)
                    run.max_gap_ms = max(run.max_gap_ms, gap)
                last = now
                if event.get("type") == "status" and event.get("status") in (
                    "done",
                    "failed",
                    "cancelled",
                ):
                    terminal_status = event["status"]
                    run.terminal_ms = (now - started) * 1000
                    break
    except Exception as exc:
        run.error = f"stream failed: {type(exc).__name__}: {exc}"
        run.dropped = True
        return run

    if terminal_status is None:
        run.dropped = True
        run.error = "stream closed before a terminal event"
        return run
    if terminal_status != "done":
        run.error = f"job ended {terminal_status}"
        return run

    try:
        result = await client.get(f"/api/v1/jobs/{job_id}/result", headers=headers)
        result.raise_for_status()
        # Read the body: a result nobody deserializes does not measure serialization.
        payload = result.json()
        assert payload["kind"] in ("grid2d", "mesh2d")
    except Exception as exc:
        run.error = f"result failed: {type(exc).__name__}: {exc}"
        return run
    run.result_ms = (time.perf_counter() - started) * 1000
    return run


async def run_client(index: int, args, ws_base: str) -> list[JobRun]:
    del index  # each client is identical; the index is only for readability at call sites
    runs = []
    async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout) as client:
        for _ in range(args.jobs):
            runs.append(await run_one_job(client, args, ws_base))
    return runs


async def main_async(args) -> int:
    ws_base = args.url.replace("https://", "wss://").replace("http://", "ws://")

    rss: list[float] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample_rss(args.pid, stop, rss)) if args.pid else None

    print(
        f"{args.clients} clients x {args.jobs} jobs on {args.solver} "
        f"{json.dumps(args.params)} -> {args.url}"
    )
    wall_start = time.perf_counter()
    results = await asyncio.gather(
        *(run_client(i, args, ws_base) for i in range(args.clients))
    )
    wall = time.perf_counter() - wall_start

    if sampler is not None:
        stop.set()
        await sampler

    runs = [run for client_runs in results for run in client_runs]
    ok = [run for run in runs if run.error is None]
    failed = [run for run in runs if run.error is not None]
    all_gaps = [gap for run in ok for gap in run.gaps_ms]

    report = {
        "clients": args.clients,
        "jobs_per_client": args.jobs,
        "solver": args.solver,
        "params": args.params,
        "jobs_total": len(runs),
        "jobs_ok": len(ok),
        "jobs_failed": len(failed),
        "streams_dropped": sum(1 for run in runs if run.dropped),
        "wall_seconds": round(wall, 2),
        "throughput_jobs_per_second": round(len(ok) / wall, 2) if wall else 0.0,
        "submit_ms": percentiles([run.submit_ms for run in ok]),
        "first_event_ms": percentiles([run.first_event_ms for run in ok]),
        "terminal_ms": percentiles([run.terminal_ms for run in ok]),
        "result_ms": percentiles([run.result_ms for run in ok]),
        "inter_event_gap_ms": percentiles(all_gaps),
        "events_per_job": round(statistics.mean([run.events for run in ok]), 1) if ok else 0,
    }
    if rss:
        report["api_rss_mb"] = {
            "start": round(rss[0], 1),
            "peak": round(max(rss), 1),
            "end": round(rss[-1], 1),
        }

    print(json.dumps(report, indent=2))
    if failed:
        print(f"\n{len(failed)} failed:")
        for run in failed[:10]:
            print(f"  - {run.error}")
        if len(failed) > 10:
            print(f"  … and {len(failed) - 10} more")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.json}")

    # Exit non-zero on any failure so `make loadtest` can gate on it.
    return 1 if failed else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="server base URL")
    parser.add_argument("--clients", type=int, default=25, help="concurrent clients")
    parser.add_argument("--jobs", type=int, default=2, help="jobs each client runs in sequence")
    parser.add_argument("--solver", default="mock.laplace2d")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "solver parameter, repeatable; values are parsed as JSON where possible "
            "(--param resolution=96 --param write_vtk=false). Defaults suit "
            "mock.laplace2d; pass your own for any other solver."
        ),
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout, seconds")
    parser.add_argument("--api-key", default=None, help="for a server with FENIXSPOON_API_KEYS")
    parser.add_argument("--pid", type=int, default=None, help="server PID, to sample its RSS")
    parser.add_argument("--json", default=None, help="also write the report to this path")
    args = parser.parse_args(argv)
    args.params = parse_params(args.param) or dict(DEFAULT_PARAMS)
    return args


DEFAULT_PARAMS = {"resolution": 96, "iterations": 400, "report_every": 50}


def parse_params(pairs: list[str]) -> dict:
    """``["resolution=96", "write_vtk=false"]`` → ``{"resolution": 96, "write_vtk": False}``.

    JSON first so numbers and booleans arrive as numbers and booleans — a solver's params
    model would reject the string "96" for an int field in strict mode, and silently
    coerce it in lenient mode, which is worse.
    """
    params: dict = {}
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not separator:
            raise SystemExit(f"--param needs KEY=VALUE, got {pair!r}")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
