"""How domain errors become JSON-RPC errors (roadmap M2.5, issue #45).

The counterpart of :mod:`fenixspoon.http_errors`, and deliberately shaped like it: one
table, one renderer, so the codes this transport promises are visible in a single place.
The two tables are independent — a JSON-RPC code is not an HTTP status with a minus sign —
but they classify the *same* error objects, which is what lets #51 assert that a validation
failure is the same failure on both transports.

Codes come from two pools. Where a reserved JSON-RPC code genuinely means what happened it
is used unchanged: params that fail a schema really are `-32602 Invalid params`, and
pretending otherwise would make a generic client's error handling wrong. Everything else
gets a code from the `-32000..-32099` implementation-defined server range, grouped by what
the caller should *do* — not found, conflict, refused for cost — because that is the
distinction a caller acts on and the spec offers no vocabulary for it.

Adding an error to the core without adding it here is safe: the fallback is
`SERVER_ERROR`, and `test_every_core_error_has_a_json_rpc_code` fails so it does not stay
that way.
"""

from typing import Any

from ..core import errors

# Reserved by the JSON-RPC 2.0 specification.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Implementation-defined server errors (-32000..-32099), one per thing a caller can do
# about it. Fewer codes than error classes on purpose: the class name travels in `data.type`
# for anyone who wants to switch on the exact case, and a code per class would be a hundred
# constants nobody can hold in their head.
SERVER_ERROR = -32000
NOT_FOUND = -32001
CONFLICT = -32002
QUOTA_EXCEEDED = -32003
GONE = -32004

#: Domain error → JSON-RPC code. Compare `http_errors.STATUS`: the groupings match, because
#: they answer the same question about the same errors.
CODES: dict[type[errors.CoreError], int] = {
    errors.UnknownCapability: NOT_FOUND,
    errors.JobNotFound: NOT_FOUND,
    errors.ArtifactNotFound: NOT_FOUND,
    errors.ObjectNotFound: NOT_FOUND,
    # Everything a caller fixes by sending different arguments. `-32602` rather than a
    # private code: this is precisely the reserved meaning, and a generic JSON-RPC client
    # already knows not to retry it unchanged.
    errors.GeometryKindMismatch: INVALID_PARAMS,
    errors.UnknownSection: INVALID_PARAMS,
    errors.InvalidParams: INVALID_PARAMS,
    errors.CellBudgetExceeded: INVALID_PARAMS,
    errors.UnknownObjectType: INVALID_PARAMS,
    errors.MalformedReference: INVALID_PARAMS,
    errors.WrongObjectType: INVALID_PARAMS,
    errors.InvalidObject: INVALID_PARAMS,
    errors.InvalidPatch: INVALID_PARAMS,
    errors.UnknownLevel: INVALID_PARAMS,
    errors.FieldQueryFailed: INVALID_PARAMS,
    errors.RegionUnavailable: INVALID_PARAMS,
    # A load case the geometry or the capability cannot honour (#85). `invalid_request`
    # rather than `conflict`, including for two load cases that disagree: nothing about the
    # server's state is in the way, and the fix is always a different request.
    errors.UnknownBoundary: INVALID_PARAMS,
    errors.UnknownConditionKey: INVALID_PARAMS,
    errors.ConflictingConditions: INVALID_PARAMS,
    # The request was well-formed and the state says no. Retrying the identical call is
    # pointless; doing something else first may help.
    errors.JobAlreadyFinished: CONFLICT,
    errors.JobNotFinished: CONFLICT,
    errors.JobDidNotSucceed: CONFLICT,
    errors.PatchChangedNothing: CONFLICT,
    errors.CannotPatchRevision: CONFLICT,
    # Its own code because it is the one refusal where *waiting* is the remedy, and
    # `data.retry_after` says how long when waiting actually helps.
    errors.QuotaExceeded: QUOTA_EXCEEDED,
    # The job is there and its arrays are not — the same distinction `410` draws over HTTP.
    errors.ResultPayloadMissing: GONE,
}

#: Attributes every `CoreError` has, which are the message rather than structure.
_NOT_DATA = frozenset({"args", "detail"})


def code_for(error: errors.CoreError) -> int:
    """Code for this error, walking the MRO so a subclass inherits its parent's code."""
    for cls in type(error).__mro__:
        if cls in CODES:
            return CODES[cls]
    return SERVER_ERROR


def error_object(error: errors.CoreError) -> dict[str, Any]:
    """Render a domain error as a JSON-RPC error object.

    `data.type` is the error's class name — the stable key a caller switches on when the
    code's granularity is not enough, and the key #51 uses to assert that HTTP and JSON-RPC
    are refusing for the same reason.

    The rest of `data` is the error's own public attributes, collected rather than listed.
    Every one of them exists because some adapter needed the structure behind the sentence —
    `InvalidParams.errors`, `CellBudgetExceeded.estimate`, `QuotaExceeded.retry_after` — so
    a table repeating them here would be a second place to forget. The consequence is worth
    stating plainly: **a new public attribute on a core error is new wire contract.** That is
    the intended direction, but it means naming an internal helper attribute `self.foo` on a
    `CoreError` would publish it.

    Never a stack trace, and never prose wrapped around one: the issue asks for typed compact
    errors, and an exception's `__str__` is `detail`, which is already the message.
    """
    data: dict[str, Any] = {"type": type(error).__name__}
    for name, value in vars(error).items():
        if name in _NOT_DATA or name.startswith("_"):
            continue
        if _is_jsonable(value):
            data[name] = value
    return {"code": code_for(error), "message": error.detail, "data": data}


def _is_jsonable(value: Any) -> bool:
    """Whether a value survives the encoder, checked structurally rather than by trying it.

    Dropping what does not is the conservative half of collecting attributes automatically:
    an error that grows an attribute holding a `Path` or a solver class publishes nothing
    rather than making every call that raises it fail to serialise. Only the types
    `json.dumps` handles with no `default=` hook count, so what this admits and what the
    encoder accepts cannot drift apart.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list | tuple):
        return all(_is_jsonable(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_jsonable(v) for k, v in value.items())
    return False
