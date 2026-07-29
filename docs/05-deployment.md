# Deployment

Everything here is environment variables. There is no config file format to learn, and no
step that requires editing Python.

## Lock a server to a team

The whole recipe:

```yaml
# docker-compose.yml
services:
  api:
    image: fenix-spoon:latest
    environment:
      FENIXSPOON_API_KEYS: "alice:${ALICE_KEY},bob:${BOB_KEY}"
      FENIXSPOON_CORS_ORIGINS: "https://sim.example.com"
      FENIXSPOON_MAX_CELLS: "500000"
      FENIXSPOON_MAX_CONCURRENT_JOBS: "2"
      FENIXSPOON_MAX_JOBS_PER_HOUR: "60"
      FENIXSPOON_MAX_ARTIFACT_BYTES: "2000000000"
      FENIXSPOON_JOB_TIMEOUT: "300"
      FENIXSPOON_DATA_DIR: /data
    volumes:
      - fenixspoon-data:/data
volumes:
  fenixspoon-data:
```

That is a server where every request needs a key, each person gets two concurrent jobs and
sixty an hour, no single job can ask for more than half a million cells or five minutes,
and job history survives a restart. Generate keys with `python -c "import secrets;
print(secrets.token_urlsafe(32))"`.

## Authentication

| Mode | Configuration | Behaviour |
|---|---|---|
| Anonymous | `FENIXSPOON_API_KEYS` unset | Every caller is the principal `anonymous`. The dev default; every demo in this repo relies on it |
| API keys | `FENIXSPOON_API_KEYS="name:secret,…"` | A key is required on every route. Present it as `Authorization: Bearer <key>` or `X-API-Key: <key>` |

A bare entry without a colon (`FENIXSPOON_API_KEYS="sk-shared"`) is its own principal, for
a one-off deployment that does not care who is who. A trailing colon (`"alice:"`) is
refused at startup rather than turned into a key called `alice:`.

### The WebSocket takes the key in the query string

```js
new WebSocket(`wss://sim.example.com/api/v1/jobs/${id}/events?api_key=${key}`);
```

Browsers have no API for setting headers on a WebSocket handshake, so this is the only way
a page can authenticate the progress stream — the SDK does it for you when you pass
`apiKey`. Keys therefore end up in URLs, where proxies and access logs may keep them:
**issue one key per person and be ready to revoke**, rather than sharing one secret.
Non-browser clients should keep using the header, which both transports accept.

### Quotas and isolation

Quotas are per principal, and every default is unlimited. Under API keys, a principal sees
only its own jobs: someone else's job id returns `404`, not `403` — confirming that an id
exists is itself a leak.

In anonymous mode everyone shares the principal `anonymous`, so quotas there are
server-wide. That is a legitimate configuration: it is how you put a public demo behind a
rate limit without running an identity provider.

### OIDC, mTLS, or a header from a trusted proxy

Not implemented, and not blocked either. `app.state.auth` holds an `Authenticator`;
replace it with anything exposing `principal(presented_key) -> Principal`:

```python
from fenixspoon.auth import Authenticator, Principal, Quotas
from fenixspoon.main import create_app

class OidcAuthenticator(Authenticator):
    @property
    def required(self) -> bool:
        return True

    def principal(self, presented: str | None) -> Principal:
        claims = verify_jwt(presented)          # your library of choice
        return Principal(id=claims["sub"], quotas=Quotas(concurrent_jobs=4))

app = create_app()
app.state.auth = OidcAuthenticator()
```

The API layer never knows the difference; per-principal quotas and job isolation work
unchanged, because both key off `Principal.id`.

## Resource limits

| Variable | Default | Enforced |
|---|---|---|
| `FENIXSPOON_MAX_CELLS` | `2000000` | At submit, from the solver's own estimate — over-budget work is refused before it starts |
| `FENIXSPOON_JOB_TIMEOUT` | `600` | Cooperatively, at the solver's next check point |
| `FENIXSPOON_MAX_CONCURRENT_JOBS` | `0` (unlimited) | At submit, counting the principal's queued and running jobs |
| `FENIXSPOON_MAX_JOBS_PER_HOUR` | `0` (unlimited) | At submit, over a rolling hour |
| `FENIXSPOON_MAX_ARTIFACT_BYTES` | `0` (unlimited) | At submit, against the principal's stored artifacts |
| `FENIXSPOON_MAX_WORKERS` | core count | Solves running at once, in a pool of the job manager's own |

The worker count is the one knob whose right value depends on your solver. FEniCSx spends
its time in PETSc and Gmsh, which release the GIL, so it parallelizes and the core count is
right. A pure-Python solver does not, and over-subscribing it *lowers* throughput —
measurably, see the [load test](06-load-test.md).

**A per-job memory ceiling needs the worker backend.** With in-process solving there is
nothing to limit: solves run on threads, and a memory limit is a property of a process —
`RLIMIT_AS` applies to all of them at once, so "cap this job at 2 GB" cannot be expressed.
What bounds memory there is the cell budget, a proxy for the solve's footprint, and the
container's own limit.

Run workers and it becomes real: one solve per container means `mem_limit: 2g` in compose
(or `resources.limits` in Kubernetes) *is* the per-job ceiling, and the kernel enforces it
whatever the solver does.

## Running solves in worker containers

By default the API solves in its own process. That is right for a laptop and wrong for a
shared server: a heavy solve competes with the event loop for the interpreter, so the API
gets slower to answer exactly when it is busiest — measurably, see the
[load test](06-load-test.md).

Setting `FENIXSPOON_REDIS_URL` moves solving out:

```bash
docker compose -f docker-compose.yml -f docker-compose.workers.yml up --scale worker=4
```

The API then dispatches to a Redis queue and does no solving at all. Workers write
results and artifacts to the shared data directory and publish progress over Redis
pub/sub, which the API relays to the WebSocket — so live progress works exactly as
before, from a process the browser never talks to.

| Variable | Where | What it does |
|---|---|---|
| `FENIXSPOON_REDIS_URL` | API and worker | Unset: the API solves in-process. Set: it dispatches, and its event bus becomes Redis. **One variable drives both**, so a half-distributed deployment — dispatching jobs but listening on an in-process bus, whose only symptom is a progress bar that never moves — is not reachable by misconfiguration |
| `FENIXSPOON_WORKER_CONCURRENCY` | worker | Solves per worker container (default 1). Keep it at 1 and scale containers, so a memory limit applies to one job |

Three things to get right:

- **The data directory must be the same volume** for the API and every worker. Workers
  write `result.json` and artifacts there; the API serves them. Redis carries only the
  queue and live events, and needs no persistence — job state is in the store.
- **Deploy the API and workers together.** A worker without a solver the API advertises
  fails that job immediately with `this worker has no solver named …` rather than
  hanging it, but that is damage control, not a supported configuration.
- **A worker killed mid-solve leaves its job `running`.** Nothing in the queue notices —
  that is what an out-of-process backend costs. The API deliberately does *not* fail
  running jobs on startup in this mode, because the workers are still going; a job whose
  worker died is stuck until the retention sweep removes it. Heartbeats are the real
  answer and are not implemented.

Cancellation crosses the boundary through a Redis flag the running solve polls, so it
stays cooperative: a solve stops at its next check point, typically within a second.

`arq` is the queue, not Celery. Celery is synchronous-first in an application that is
asyncio throughout, it wants to own job state that the store already owns, and its
dependency tree is larger than this server. arq is used here purely as a dispatcher with
`max_tries=1` and its results ignored, so there is exactly one source of truth. A
deployment that must run Celery implements `ExecutionBackend` against it.

## Durability

`FENIXSPOON_DATA_DIR` holds the job database, result payloads and artifacts. Mount it and a
restarted server answers for jobs the previous one ran; don't, and every restart is amnesia.
`FENIXSPOON_JOB_TTL` (default 7 days, `0` forever) drops records and files together, swept
at startup and hourly.

Set `FENIXSPOON_STORE=memory` to opt out of persistence entirely — appropriate for a
stateless demo behind a load balancer, where each replica having its own job history would
be worse than having none.

## CORS

`FENIXSPOON_CORS_ORIGINS` is a comma-separated list. Unset, it defaults to `*` in anonymous
mode (a local server talking to a widget on any origin) and to **nothing** when API keys are
configured — pairing credentials with a wildcard origin is the classic footgun. Same-origin
pages keep working either way, including the demos this server hosts, so a front-end served
from the same host needs no CORS configuration at all.

## Reverse proxy

The event stream is a WebSocket; a proxy that doesn't upgrade it will make the UI look
hung. For nginx:

```nginx
location /api/v1/ {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;   # longer than FENIXSPOON_JOB_TIMEOUT
}
```

`proxy_read_timeout` must exceed the job timeout, or the proxy drops the socket mid-solve.
The SDK reconnects and the server replays the whole event history, so this degrades to a
stutter rather than a failure — but a proxy timeout below the job timeout means every long
job stutters.
