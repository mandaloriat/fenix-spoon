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


class UnknownObjectType(CoreError):
    """A workspace operation named a type that does not exist (roadmap M2.5, issue #44)."""

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(f"unknown object type {name!r}: expected one of {known}")
        self.name = name
        self.known = known


class MalformedReference(CoreError):
    """An object reference did not parse.

    Separate from :class:`ObjectNotFound` because the two send a caller to different places:
    a malformed reference is a string it built wrongly, a missing one is a workspace that
    does not hold what it expected.
    """

    def __init__(self, ref: str, reason: str) -> None:
        super().__init__(reason)
        self.ref = ref


class ObjectNotFound(CoreError):
    """No such object, or it belongs to another principal — indistinguishable, as for jobs."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"object not found: {ref}")
        self.ref = ref


class WrongObjectType(CoreError):
    """A reference resolved, but to the wrong kind of thing.

    Distinct from a missing object because it is a much more useful message: the caller
    passed `material:m-2` where a design was wanted, and saying so beats "not found".
    """

    def __init__(self, ref: str, expected: str, got: str) -> None:
        super().__init__(f"{ref} is a {got}, expected a {expected}")
        self.ref = ref
        self.expected = expected
        self.got = got


class InvalidObject(CoreError):
    """An object body failed its type's schema.

    Carries pydantic's structured list for the same reason :class:`InvalidParams` does: a
    caller fixing a geometry wants the field, not a sentence about it.
    """

    def __init__(self, object_type: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"body does not match the schema for a {object_type}")
        self.object_type = object_type
        self.errors = errors


class InvalidPatch(CoreError):
    """An RFC 6902 patch was malformed, or did not apply to this object."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"patch could not be applied: {reason}")
        self.reason = reason


class PatchChangedNothing(CoreError):
    """A patch applied cleanly and produced an identical body.

    An error rather than a no-op: writing the revision would move the head without changing
    anything, so every reference pinned afterwards would name a new number for the same
    content. A caller that believed it was editing something needs to hear that it was not.
    """

    def __init__(self, ref: str) -> None:
        super().__init__(f"patch left {ref} unchanged; nothing was written")
        self.ref = ref


class CannotPatchRevision(CoreError):
    """A patch named a pinned revision. Revisions are immutable and there are no branches."""

    def __init__(self, ref: str) -> None:
        super().__init__(
            f"cannot patch {ref}: revisions are immutable. Patch the object itself "
            f"({ref.split('@')[0]}) to write the next revision."
        )
        self.ref = ref


class UnknownBoundary(CoreError):
    """A load case named a boundary the geometry does not declare (issue #85).

    The refusal #85 asked for by name, and the one that carries the whole design. A
    condition applied to nothing does not fail — it produces a solve that runs, converges,
    and answers a different problem than the caller described: a cantilever that was never
    clamped comes back as a rigid-body motion, and a plate that was never loaded comes back
    at zero stress. Both look like results.

    So it is refused at submit, and the message names both halves of the mistake: what was
    asked for and what is on offer. A typo in a boundary name is then a one-line fix rather
    than an afternoon.
    """

    def __init__(self, boundary: str, declared: list[str]) -> None:
        super().__init__(
            f"the load case names the boundary {boundary!r}, which this geometry does not "
            + (
                f"declare; it declares {declared}"
                if declared
                else "declare — it names no boundaries at all"
            )
        )
        self.boundary = boundary
        self.declared = declared


class UnknownConditionKey(CoreError):
    """A load case used a condition key this capability does not read (issue #85).

    Load-case values are an open map of scalars on purpose — a typed enum of condition
    kinds would put physics into the protocol. :class:`~fenixspoon.solvers.base.ConditionSpec`
    is what keeps that openness from costing a caller a silent typo, and this is where the
    declaration is spent.
    """

    def __init__(self, solver: str, boundary: str, key: str, accepted: list[str]) -> None:
        super().__init__(
            f"the load case sets {key!r} on the boundary {boundary!r}, which the capability "
            f"{solver!r} does not read; it reads "
            + (f"{accepted}" if accepted else "no boundary-condition keys at all")
        )
        self.solver = solver
        self.boundary = boundary
        self.key = key
        self.accepted = accepted


class ConflictingConditions(CoreError):
    """Two load cases of one design set the same key on the same boundary (issue #85).

    Refused rather than resolved by order. A design listing two load cases that both say
    what happens on `root` has two intents in one request, and the same argument that makes
    `design` and an inline geometry mutually exclusive applies: honouring one silently
    produces a solve whose inputs are not what the caller thinks they are.
    """

    def __init__(self, boundary: str, key: str, sources: list[str]) -> None:
        super().__init__(
            f"load cases {sources} both set {key!r} on the boundary {boundary!r}; a design "
            "cannot say what happens there twice"
        )
        self.boundary = boundary
        self.key = key
        self.sources = sources


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


class UnknownLevel(CoreError):
    """A result was requested at a level that does not exist (roadmap M2.5, issue #46).

    Refused rather than ignored, for the reason :class:`UnknownSection` is: a caller that
    asks for `metric` and receives an answer with no metrics in it would conclude the solve
    reported none.
    """

    def __init__(self, unknown: list[str], known: list[str]) -> None:
        super().__init__(f"unknown result level(s) {unknown}: expected any of {known}")
        self.unknown = unknown
        self.known = known


class ResultPayloadMissing(CoreError):
    """The job succeeded, but its field arrays are no longer on disk.

    A real state, not a defensive branch: the store deliberately tolerates a missing
    `result.json` — manual cleanup, a half-deleted job, a partially swept retention run —
    and reports the job rather than crashing. The metadata, metrics and diagnostics all
    survive in the database, so the compact levels still answer; only the arrays are gone.

    It needs a name of its own because the alternative was reporting `JobNotFinished` with
    a status of `done`, which is self-contradictory and sends a caller to poll something
    that will never change. Mapped to `410 Gone`: the resource existed and does not now,
    which is exactly the case.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"the field data for job {job_id} is no longer on disk; its metrics, "
            "diagnostics and artifacts are still available"
        )
        self.job_id = job_id


class FieldQueryFailed(CoreError):
    """A field query could not be answered as asked.

    Wraps :class:`~fenixspoon.fields.FieldError`, which carries prose naming what is
    available — the field names this result actually has, the operations that exist. That
    detail is the whole value of the error: a caller that guessed a field name needs the
    list, not a status code.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RegionUnavailable(CoreError):
    """`over_region` named a region this job's geometry cannot supply.

    A result carries no region names — only arrays — so the region is resolved against the
    geometry the job recorded in its workspace provenance. A job submitted with an inline
    geometry kept none, and this says so rather than reporting an empty selection.
    """

    def __init__(self, region: str, reason: str) -> None:
        super().__init__(f"cannot resolve region {region!r}: {reason}")
        self.region = region
        self.reason = reason


class ArtifactNotFound(CoreError):
    """No artifact by that name on this job, or its file is gone from disk."""

    def __init__(self, detail: str = "artifact not found") -> None:
        super().__init__(detail)
