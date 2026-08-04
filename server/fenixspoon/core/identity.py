"""Identity and quotas, with no transport attached (roadmap M2.5, issue #42).

Split out of ``auth.py`` because the core needs `Principal` and `check_quotas`, and
``auth.py`` imports FastAPI — so importing the core pulled in a web framework to submit a
job from a script. Caught by the acceptance check for #42 rather than by the API tests,
which of course had FastAPI loaded already.

What lives here is the *rule*: who a caller is, what they are allowed, whether one more job
is too many. What stays in ``auth.py`` is the HTTP *binding*: reading a credential out of
headers or a query string, and the `401` challenge with `WWW-Authenticate`. A JSON-RPC or
CLI caller has a `Principal` too; it just does not arrive in a header.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import QuotaExceeded

ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Quotas:
    """Per-principal limits. ``0`` means unlimited, which is every default."""

    concurrent_jobs: int = 0
    jobs_per_hour: int = 0
    artifact_bytes: int = 0
    #: How many workspace objects one principal may own. Added by ADR 0002, and the reason
    #: it did not exist before is the reason it is needed now: creating an object was free
    #: and local, so nothing counted them. Over HTTP an authenticated caller can create them
    #: without bound, and the design pass named this rather than leaving it to be found.
    objects: int = 0

    @classmethod
    def from_env(cls) -> "Quotas":
        return cls(
            concurrent_jobs=int(os.environ.get("FENIXSPOON_MAX_CONCURRENT_JOBS", "0")),
            jobs_per_hour=int(os.environ.get("FENIXSPOON_MAX_JOBS_PER_HOUR", "0")),
            artifact_bytes=int(os.environ.get("FENIXSPOON_MAX_ARTIFACT_BYTES", "0")),
            objects=int(os.environ.get("FENIXSPOON_MAX_OBJECTS", "0")),
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


@dataclass
class QuotaUsage:
    """What a principal is currently using, for the error message as much as the check."""

    concurrent_jobs: int
    jobs_last_hour: int
    artifact_bytes: int


def check_quotas(principal: Principal, usage: QuotaUsage) -> None:
    """Raise :class:`~fenixspoon.core.errors.QuotaExceeded` if one more job is too many.

    A domain error rather than an `HTTPException`: quotas are an application rule, and the
    core has to enforce them for callers that are not speaking HTTP. The HTTP adapter maps
    this to `429`, and to `Retry-After` where `retry_after` is set — which is only where
    waiting actually helps. An artifact quota is not relieved by waiting an hour, so it
    carries no hint rather than a misleading one.
    """
    quotas = principal.quotas
    if quotas.concurrent_jobs and usage.concurrent_jobs >= quotas.concurrent_jobs:
        raise QuotaExceeded(
            f"{usage.concurrent_jobs} of your jobs are already running, at this "
            f"server's limit of {quotas.concurrent_jobs}. Wait for one to finish "
            "or cancel it."
        )
    if quotas.jobs_per_hour and usage.jobs_last_hour >= quotas.jobs_per_hour:
        raise QuotaExceeded(
            f"you have submitted {usage.jobs_last_hour} jobs in the last hour, at "
            f"this server's limit of {quotas.jobs_per_hour}.",
            retry_after=3600,
        )
    if quotas.artifact_bytes and usage.artifact_bytes >= quotas.artifact_bytes:
        raise QuotaExceeded(
            f"your stored artifacts total {usage.artifact_bytes:,} bytes, at this "
            f"server's limit of {quotas.artifact_bytes:,}. Old jobs are removed "
            "after the retention period, or you can wait for the sweep."
        )


def hour_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)




def check_object_quota(principal: Principal, owned: int) -> None:
    """Raise if one more object is too many — the counterpart of :func:`check_quotas`.

    Its own function rather than a fourth branch in that one, because the two are asked at
    different moments about different things: `check_quotas` is asked whether a *solve* may
    start and reads three numbers a job store computes, and this is asked whether an *object*
    may be written and reads one number the object store counts.

    **No `retry_after`.** The job quotas carry one where waiting genuinely relieves them —
    an hourly window rolls, a running job finishes. Nothing about waiting deletes an object,
    so a hint here would be a lie of exactly the kind the artifact-bytes quota already
    refuses to tell.
    """
    limit = principal.quotas.objects
    if limit and owned >= limit:
        raise QuotaExceeded(
            f"you own {owned} workspace objects, at this server's limit of {limit}. "
            "Objects are kept until deleted and there is no deletion, so this does not "
            "relieve itself — ask for a higher limit or use a separate workspace."
        )
