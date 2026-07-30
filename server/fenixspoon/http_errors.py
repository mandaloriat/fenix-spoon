"""How domain errors become HTTP responses (roadmap M2.5, issue #42).

This is the whole of the HTTP binding for failure. One table, one handler — so the status
codes the wire protocol documents are visible in a single place rather than distributed
across fourteen `raise HTTPException` sites, and a JSON-RPC adapter can write its own table
against the same error classes.

Adding an error to the core without adding it here is safe: the fallback is `400`, which is
wrong-ish but not a crash, and `test_every_core_error_has_a_status` fails so it does not
stay wrong.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from .core import errors

#: Domain error → HTTP status. The values are the ones `docs/04-wire-protocol.md` promises.
STATUS: dict[type[errors.CoreError], int] = {
    errors.UnknownCapability: 404,
    errors.JobNotFound: 404,
    errors.ArtifactNotFound: 404,
    errors.ObjectNotFound: 404,
    errors.GeometryKindMismatch: 422,
    errors.UnknownSection: 422,
    errors.InvalidParams: 422,
    errors.CellBudgetExceeded: 422,
    errors.UnknownObjectType: 422,
    errors.MalformedReference: 422,
    errors.WrongObjectType: 422,
    errors.InvalidObject: 422,
    errors.InvalidPatch: 422,
    errors.UnknownLevel: 422,
    errors.FieldQueryFailed: 422,
    errors.RegionUnavailable: 422,
    errors.JobAlreadyFinished: 409,
    errors.JobNotFinished: 409,
    errors.JobDidNotSucceed: 409,
    errors.PatchChangedNothing: 409,
    errors.CannotPatchRevision: 409,
    errors.QuotaExceeded: 429,
    # The job succeeded and its arrays have since been removed. `410` rather than `404`
    # because the job itself is very much there — only the payload is gone, and the
    # compact levels still answer for it.
    errors.ResultPayloadMissing: 410,
}

# The workspace errors above have no route behind them yet: issue #44 is core-only, and the
# first transport to expose `object.patch` is the JSON-RPC adapter (#45). They are mapped
# anyway because `test_every_core_error_has_a_status` requires every domain error to be given
# a status deliberately — and because an HTTP binding, when it comes, should not be the moment
# somebody first thinks about whether a failed patch is a 409 or a 422.

FALLBACK_STATUS = 400


def status_for(error: errors.CoreError) -> int:
    """Status for this error, walking the MRO so a subclass inherits its parent's code."""
    for cls in type(error).__mro__:
        if cls in STATUS:
            return STATUS[cls]
    return FALLBACK_STATUS


async def core_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a `CoreError` as the response the protocol documents.

    `InvalidParams` sends pydantic's structured list as `detail` — unchanged from before,
    because clients parse it — while every other error sends prose. `QuotaExceeded` adds
    `Retry-After` only when the core said waiting helps.
    """
    del request
    # Not an `assert`: those vanish under `python -O`, and the next line would then fail
    # with `AttributeError: 'ValueError' object has no attribute 'detail'` — a traceback
    # that says nothing about the real problem, which is a handler registered for a type it
    # cannot render. Re-raising hands the exception back to the framework's default
    # handler, so it becomes an honest 500 with the original traceback intact.
    #
    # The signature says `Exception` because that is what FastAPI's handler protocol
    # requires; this is where it gets narrowed.
    if not isinstance(exc, errors.CoreError):
        raise exc
    detail = exc.errors if isinstance(exc, errors.InvalidParams) else exc.detail
    headers = {}
    if isinstance(exc, errors.QuotaExceeded) and exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(
        status_code=status_for(exc), content={"detail": detail}, headers=headers or None
    )
