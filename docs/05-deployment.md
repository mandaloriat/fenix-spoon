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

**There is no per-job memory ceiling, and adding one here would be dishonest.** Solves run
on threads inside the API process, and a memory limit is a property of a process — `RLIMIT_AS`
applies to all of them at once, so "cap this job at 2 GB" is not something this backend can
express. Two things actually bound memory today: the cell budget, which is a proxy for the
solve's footprint, and the container's own limit (`mem_limit` in compose, `resources.limits`
in Kubernetes), which is the real backstop. A genuine per-job ceiling arrives with the
out-of-process worker backend (#12), where each solve is a process that can be limited and
killed on its own.

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
