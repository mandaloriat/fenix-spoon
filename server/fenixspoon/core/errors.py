"""Domain errors the core raises instead of ``HTTPException`` (roadmap M2.5, issue #42).

Every one of these was previously an `HTTPException` raised inside a FastAPI route body,
which is why nothing but FastAPI could call that logic. They carry *what went wrong*, not
*how to say it over HTTP* — the mapping to status codes lives in the HTTP adapter, and
JSON-RPC or MCP adapters will map the same errors to their own codes.

The `detail` on each is the message a human reads. Where a caller needs more than prose —
`InvalidParams` carrying pydantic's structured error list, `QuotaExceeded` carrying a
retry hint — it is a separate attribute, so an adapter can render it appropriately rather
than parsing it back out of a sentence.
"""

from typing import Any


class CoreError(Exception):
    """Base for every error the application core raises.

    An adapter that handles this one class cannot be surprised by a new error kind: it
    will map an unrecognised subclass through the base's default rather than crashing.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnknownCapability(CoreError):
    """No solver by that name is installed."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown solver: {name!r}")
        self.name = name


class UnknownSection(CoreError):
    """`capability.describe` was asked for a section that does not exist.

    Refused rather than silently dropped. A caller that misspells `metrics` and gets a
    payload with no metrics in it will conclude the capability has none, which is a wrong
    answer arrived at quietly — the failure mode progressive discovery must not have.
    """

    def __init__(self, unknown: list[str], known: list[str]) -> None:
        super().__init__(
            f"unknown capability section(s) {unknown}: expected any of {known}"
        )
        self.unknown = unknown
        self.known = known


class GeometryKindMismatch(CoreError):
    """The solver exists but does not accept this geometry kind."""

    def __init__(self, solver: str, accepted: list[str], got: str) -> None:
        super().__init__(f"solver {solver!r} accepts geometry types {accepted}, got {got!r}")
        self.solver = solver
        self.accepted = accepted
        self.got = got


class InvalidParams(CoreError):
    """Params failed the solver's own schema.

    ``errors`` is pydantic's structured list. It stays separate from ``detail`` because a
    form generator wants the per-field breakdown, while a log line wants the sentence.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("params do not match this solver's schema")
        self.errors = errors


class CellBudgetExceeded(CoreError):
    """The job would exceed the server's submit-time cell budget."""

    def __init__(self, estimate: int, limit: int) -> None:
        super().__init__(
            f"job would use about {estimate:,} cells, over this server's limit of "
            f"{limit:,}. Lower the resolution or mesh size, or raise FENIXSPOON_MAX_CELLS."
        )
        self.estimate = estimate
        self.limit = limit


class QuotaExceeded(CoreError):
    """A per-principal quota would be exceeded by accepting this job."""

    def __init__(self, detail: str, retry_after: int | None = None) -> None:
        super().__init__(detail)
        #: Seconds, where waiting actually helps. `None` when it does not — an artifact
        #: quota is not relieved by waiting a minute, and a `Retry-After` there would be a
        #: lie the client would act on.
        self.retry_after = retry_after


class JobNotFound(CoreError):
    """No such job, or it belongs to another principal.

    The two cases are deliberately indistinguishable. Telling a stranger that a job id
    exists is itself a leak, and the ids are only 48 bits of randomness.
    """

    def __init__(self) -> None:
        super().__init__("job not found")


class JobAlreadyFinished(CoreError):
    """Cancellation arrived after the job reached a terminal status."""

    def __init__(self, status: str) -> None:
        super().__init__(f"job already finished (status: {status})")
        self.status = status


class JobNotFinished(CoreError):
    """A result was requested before the job produced one."""

    def __init__(self, status: str) -> None:
        super().__init__(f"job not finished (status: {status})")
        self.status = status


class JobDidNotSucceed(CoreError):
    """The job reached a terminal status other than `done`, so there is no result."""

    def __init__(self, status: str, error: str | None) -> None:
        super().__init__(f"job {status}" + (f": {error}" if error else ""))
        self.status = status
        self.error = error


class ArtifactNotFound(CoreError):
    """No artifact by that name on this job, or its file is gone from disk."""

    def __init__(self, detail: str = "artifact not found") -> None:
        super().__init__(detail)
