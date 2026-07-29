"""Authentication and per-principal quotas (roadmap M3, issue #14).

The toolkit ships hooks, not an identity provider. Two modes:

- **Anonymous** (default, and what every demo in this repo runs): no keys configured,
  every request is the same principal ``anonymous``. Quotas still apply if you set them,
  which is how you put a public demo behind sane limits without running an IdP.
- **API keys**: ``FENIXSPOON_API_KEYS="alice:sk-…,bob:sk-…"``. A request must present one
  as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``; anything else is a 401.

OIDC is not implemented here and does not need to be: :func:`principal_from_request` is
one function, and an OIDC deployment replaces it via ``app.state.auth`` with something
that validates a JWT and returns a :class:`Principal`. See docs/05-deployment.md.

**WebSockets take the key in a query parameter.** Browsers cannot set headers on a
WebSocket handshake — there is no API for it — so ``?api_key=`` is the only way a page
can authenticate the progress stream. That puts the key in the URL, where it can reach
server logs, so keys are per-user and revocable rather than a single shared secret.
Non-browser clients should keep using the header, which is accepted on both transports.
"""

import hmac
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, WebSocket

log = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Quotas:
    """Per-principal limits. ``0`` means unlimited, which is every default."""

    concurrent_jobs: int = 0
    jobs_per_hour: int = 0
    artifact_bytes: int = 0

    @classmethod
    def from_env(cls) -> "Quotas":
        return cls(
            concurrent_jobs=int(os.environ.get("FENIXSPOON_MAX_CONCURRENT_JOBS", "0")),
            jobs_per_hour=int(os.environ.get("FENIXSPOON_MAX_JOBS_PER_HOUR", "0")),
            artifact_bytes=int(os.environ.get("FENIXSPOON_MAX_ARTIFACT_BYTES", "0")),
        )


@dataclass(frozen=True)
class Principal:
    """Who is asking. ``id`` is what job ownership and quotas are keyed on."""

    id: str
    quotas: Quotas


def parse_api_keys(raw: str) -> dict[str, str]:
    """``"alice:sk-a,bob:sk-b"`` → ``{"sk-a": "alice", "sk-b": "bob"}``.

    Keyed by secret because that is the lookup direction. A bare entry without a colon
    is its own principal name, so ``FENIXSPOON_API_KEYS=sk-shared`` works for one-off
    deployments that do not care who is who.
    """
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        # Test the separator, not the secret: for "alice:" the secret is empty and the
        # bare-entry branch would otherwise turn the typo into a working key "alice:".
        name, separator, secret = entry.partition(":")
        if not separator:
            name = secret = entry
        name, secret = name.strip(), secret.strip()
        if not secret:
            raise ValueError(f"empty API key for principal {name!r} in FENIXSPOON_API_KEYS")
        if not name:
            raise ValueError("empty principal name in FENIXSPOON_API_KEYS")
        keys[secret] = name
    return keys


class Authenticator:
    """Resolves a request to a :class:`Principal`, or refuses it.

    Constructed once per app and stashed on ``app.state.auth``, so a deployment can
    replace it wholesale (OIDC, mTLS, a header set by a trusted proxy) without the API
    layer knowing.
    """

    def __init__(self, api_keys: dict[str, str] | None = None, quotas: Quotas | None = None):
        if api_keys is None:
            api_keys = parse_api_keys(os.environ.get("FENIXSPOON_API_KEYS", ""))
        self._keys = api_keys
        self.quotas = quotas if quotas is not None else Quotas.from_env()

    @property
    def required(self) -> bool:
        """True when keys are configured; False is anonymous mode."""
        return bool(self._keys)

    def _lookup(self, presented: str) -> str | None:
        # Compare against every key rather than hashing into a dict: the cost is
        # negligible for a team-sized key list and it keeps the comparison constant-time.
        match = None
        for secret, name in self._keys.items():
            if hmac.compare_digest(secret, presented):
                match = name
        return match

    def principal(self, presented: str | None) -> Principal:
        if not self.required:
            return Principal(id=ANONYMOUS, quotas=self.quotas)
        if not presented:
            raise HTTPException(
                status_code=401,
                detail="this server requires an API key (Authorization: Bearer …)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        name = self._lookup(presented)
        if name is None:
            raise HTTPException(
                status_code=401,
                detail="unknown API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Principal(id=name, quotas=self.quotas)


def presented_key(headers, query_params) -> str | None:
    """Pull a key out of ``Authorization: Bearer``, ``X-API-Key``, or ``?api_key=``."""
    authorization = headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header_key = headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    query_key = query_params.get("api_key")
    return query_key.strip() if query_key else None


def principal_from_request(request: Request) -> Principal:
    """FastAPI dependency: the caller's identity, or a 401."""
    auth: Authenticator = request.app.state.auth
    return auth.principal(presented_key(request.headers, request.query_params))


def principal_from_websocket(websocket: WebSocket) -> Principal | None:
    """Same for a WebSocket, returning None instead of raising — the caller decides how
    to close the socket, since an HTTP status is not available after the handshake."""
    auth: Authenticator = websocket.app.state.auth
    try:
        return auth.principal(presented_key(websocket.headers, websocket.query_params))
    except HTTPException:
        return None


@dataclass
class QuotaUsage:
    """What a principal is currently using, for the error message as much as the check."""

    concurrent_jobs: int
    jobs_last_hour: int
    artifact_bytes: int


def check_quotas(principal: Principal, usage: QuotaUsage) -> None:
    """Raise 429 if accepting one more job would put ``principal`` over a limit."""
    quotas = principal.quotas
    if quotas.concurrent_jobs and usage.concurrent_jobs >= quotas.concurrent_jobs:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{usage.concurrent_jobs} of your jobs are already running, at this "
                f"server's limit of {quotas.concurrent_jobs}. Wait for one to finish "
                "or cancel it."
            ),
        )
    if quotas.jobs_per_hour and usage.jobs_last_hour >= quotas.jobs_per_hour:
        raise HTTPException(
            status_code=429,
            detail=(
                f"you have submitted {usage.jobs_last_hour} jobs in the last hour, at "
                f"this server's limit of {quotas.jobs_per_hour}."
            ),
            headers={"Retry-After": "3600"},
        )
    if quotas.artifact_bytes and usage.artifact_bytes >= quotas.artifact_bytes:
        raise HTTPException(
            status_code=429,
            detail=(
                f"your stored artifacts total {usage.artifact_bytes:,} bytes, at this "
                f"server's limit of {quotas.artifact_bytes:,}. Old jobs are removed "
                "after the retention period, or you can wait for the sweep."
            ),
        )


def hour_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


def cors_origins(auth_required: bool) -> list[str]:
    """Allowed origins, from ``FENIXSPOON_CORS_ORIGINS`` (comma-separated).

    Unset means ``*`` in anonymous mode — the dev default that lets a widget on any
    origin talk to a local server. But a server with API keys configured is not a dev
    server, and pairing credentials with a wildcard origin is the classic footgun, so
    unset there means *no* cross-origin access: same-origin pages (including the demos
    this server hosts) keep working, and anything else must be named explicitly.
    """
    raw = os.environ.get("FENIXSPOON_CORS_ORIGINS")
    if raw is None:
        if auth_required:
            log.info(
                "API keys are configured and FENIXSPOON_CORS_ORIGINS is unset: "
                "cross-origin requests are refused. Set it to allow a front-end origin."
            )
            return []
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]
