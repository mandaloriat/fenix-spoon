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


class CapabilityTakesNoConditions(CoreError):
    """A load case was submitted to a capability that declares no condition keys (#85).

    Refused rather than ignored, and this is the refusal the whole load-case design turns on.
    A capability that reads no conditions and is handed some would solve exactly the problem
    it would have solved without them — converged, plausible, and answering a question the
    caller did not ask. The failure has no symptom, which is why it has to be an error.
    """

    def __init__(self, solver: str) -> None:
        super().__init__(
            f"capability {solver!r} declares no boundary conditions, so it cannot honour a "
            "load case; it would solve as if none had been sent. Its boundary conditions come "
            "from its params — see `capability.describe` section `params`."
        )
        self.solver = solver


class UnknownBoundary(CoreError):
    """A load case named a boundary the geometry does not declare (#85).

    Names both sides on purpose. The caller's mistake is usually a typo or a load case written
    against a different geometry, and both are diagnosed instantly by seeing the two lists
    together — where "no such boundary" alone sends someone to read a geometry file.
    """

    def __init__(self, boundary: str, solver: str, declared: list[str]) -> None:
        super().__init__(
            f"the load case applies conditions to boundary {boundary!r}, which this geometry "
            + (
                f"does not declare; it names {declared}"
                if declared
                else "does not declare — it names no boundaries at all, so add a `boundaries` "
                "entry to the geometry before a load case can refer to one"
            )
        )
        self.boundary = boundary
        self.solver = solver
        self.declared = declared


class UnknownConditionKey(CoreError):
    """A load case used a condition key the capability does not read (#85).

    The counterpart to a material key, and deliberately not treated like one. An unread
    material key leaves the solve using a default; an unread condition key leaves a constraint
    or a load *out of the assembly*, so the mesh is under-constrained or unloaded and the
    answer is for a different problem. Issue #85 asked for exactly this asymmetry.
    """

    def __init__(self, key: str, boundary: str, solver: str, declared: list[str]) -> None:
        super().__init__(
            f"capability {solver!r} does not read the condition {key!r} applied to boundary "
            f"{boundary!r}"
            + (f"; it reads {declared}" if declared else "")
        )
        self.key = key
        self.boundary = boundary
        self.solver = solver
        self.declared = declared


class ConditionNeedsCompanion(CoreError):
    """A condition key was applied without another key it declares it requires (#85)."""

    def __init__(self, key: str, boundary: str, missing: list[str]) -> None:
        super().__init__(
            f"condition {key!r} on boundary {boundary!r} needs {missing} on the same boundary; "
            "the capability declares it as incomplete on its own"
        )
        self.key = key
        self.boundary = boundary
        self.missing = missing


class ConflictingLoadCases(CoreError):
    """Two load cases on one design apply conditions to the same boundary (#85).

    Refused rather than merged by precedence. A design naming two load cases that both speak
    for `root` has two intents in it, and picking the later one silently would make which
    conditions applied a function of list order — reproducible, and impossible to notice.
    """

    def __init__(self, boundary: str, first: str, second: str) -> None:
        super().__init__(
            f"load cases {first} and {second} both apply conditions to boundary {boundary!r}; "
            "a design may name several load cases only while they cover different boundaries"
        )
        self.boundary = boundary
        self.first = first
        self.second = second


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
