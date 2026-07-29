# Deploy the server

Three shapes, in order of how much you need. [Deployment](05-deployment.md) is the full
reference; this is the path through it.

## 1. A laptop

```bash
pip install -e "./server[dev]"
uvicorn fenixspoon.main:app --reload
```

<http://localhost:8000/> serves the demos, `/docs` the OpenAPI page. No FEniCSx needed —
the mock solvers implement the same protocol, so the whole loop works and only the
numbers are stand-ins.

With FEniCSx:

```bash
docker compose up --build
```

## 2. A team

Everything is environment variables. The whole recipe:

```yaml
environment:
  FENIXSPOON_API_KEYS: "alice:${ALICE_KEY},bob:${BOB_KEY}"
  FENIXSPOON_CORS_ORIGINS: "https://sim.example.com"
  FENIXSPOON_MAX_CELLS: "500000"
  FENIXSPOON_MAX_CONCURRENT_JOBS: "2"
  FENIXSPOON_JOB_TIMEOUT: "300"
  FENIXSPOON_DATA_DIR: /data
volumes:
  - fenixspoon-data:/data
```

Every request now needs a key, each person gets two concurrent jobs, no single job can
ask for more than half a million cells or five minutes, and job history survives a
restart. Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Three things catch people out:

- **Mount `FENIXSPOON_DATA_DIR`.** It holds the job database, result payloads and
  artifacts. Without the volume, persistence works and then evaporates with the
  container.
- **The progress WebSocket authenticates with `?api_key=`,** not a header — browsers have
  no API for headers on a WS handshake. The SDK does it when you pass `apiKey`. Because
  keys land in URLs, issue one per person and be ready to revoke.
- **CORS stops defaulting to `*` once keys are set.** Same-origin pages keep working;
  a separate front-end origin must be named explicitly.

## 3. More than one solve at a time

```bash
docker compose -f docker-compose.yml -f docker-compose.workers.yml up --scale worker=4
```

The API stops solving and dispatches to worker containers over a Redis queue. Progress
still streams to the browser, from a process it never talks to.

Reach for this when in-process solving starts to hurt — which the
[load test](06-load-test.md) quantifies: one API process handles 50 concurrent clients
without dropping a stream, but every solve shares the interpreter with the event loop, so
a Python-heavy solver's throughput *falls* as concurrency rises. Workers also make a
per-job memory limit expressible for the first time, since one solve is then one process.

## Behind a reverse proxy

The event stream is a WebSocket; a proxy that does not upgrade it makes the UI look hung.

```nginx
location /api/v1/ {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;   # must exceed FENIXSPOON_JOB_TIMEOUT
}
```

A `proxy_read_timeout` below the job timeout means the proxy drops the socket mid-solve.
The SDK reconnects and the server replays the event history, so it degrades to a stutter
rather than a failure — but every long job will stutter.

## Checking it works

```bash
curl -s localhost:8000/api/v1/solvers | jq '.[].name'
make loadtest CLIENTS=10 JOBS=2
```

`make loadtest` starts a server, drives it with concurrent clients holding WebSockets,
prints latency percentiles and exits non-zero if any job fails — so it works as a
deployment gate, not just a benchmark.
