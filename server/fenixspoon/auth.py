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

from fastapi import HTTPException, Request, WebSocket

from .core.identity import (  # noqa: F401  (re-exported: these were always importable here)
    ANONYMOUS,
    Principal,
    Quotas,
    QuotaUsage,
    check_quotas,
    hour_ago,
    parse_api_keys,
)

log = logging.getLogger(__name__)


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
