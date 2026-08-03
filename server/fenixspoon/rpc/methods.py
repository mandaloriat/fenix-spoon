"""The JSON-RPC vocabulary: method names, their params, and the core call behind each.

Roadmap M2.5, issue #45. This is the whole surface — one table, small enough to read in a
sitting, which is the property [§7 of the design draft](../../docs/07-local-agent-interface.md)
asks for. Every entry is a thin binding onto :class:`~fenixspoon.core.FenixSpoonCore`; none
of them contains logic the HTTP routes do not also reach.

Three rules the table holds to:

**Params are by name, never by position.** JSON-RPC allows an array, and it would be a
mistake here: most of these methods have optional arguments, several have five or more, and
positional binding turns "I omitted `sections`" into "I passed `sections` where
`inline_schemas` goes". A caller that sends an array is told so rather than being decoded
into something plausible.

**Nothing accepts code, a command, a package or an image.** The
[security posture](../../docs/02-architecture.md#security-posture-why-solvers-are-declarative)
is not relaxed by being on a pipe: physics is a capability name, a parameter set and declared
metrics. There is no `solve_magnetostatics` and no `run_python`.

**Structured params get a pydantic model, simple ones do not.** `job.submit` and
`result.query` have real shape, mutual exclusions and defaults, so they are validated by a
model whose failure is the same structured list the HTTP path returns. A method taking one
string does not need a class to say so.
"""

from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from ..core import FenixSpoonCore
from ..core.identity import Principal
from ..core.results import FieldQuery
from ..geometry import Geometry
from ..jobs import JobStatus
from ..protocol import PROTOCOL_VERSION

#: Validates a geometry dict into the discriminated union the core expects. Built once: a
#: `TypeAdapter` compiles a validator, and rebuilding it per request is pure overhead.
_GEOMETRY = TypeAdapter(Geometry)


class MethodError(Exception):
    """A failure in the transport binding rather than in the domain.

    Distinct from :class:`~fenixspoon.core.errors.CoreError` because the two mean different
    things to a caller: a `CoreError` says the *operation* refused, and this says the request
    never became an operation — an unknown method, params that are not an object, a missing
    required argument. Keeping them apart is what stops "you spelled the method wrong" from
    being reported as a domain outcome.
    """

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class SubmitParams(BaseModel):
    """`job.submit`: either a workspace design, or an inline solver + geometry.

    Both spellings exist for the reason [§9](../../docs/07-local-agent-interface.md#9-job-lifecycle)
    gives — a design is the reproducible one, an inline geometry is for one-shot use — and
    sending both is refused rather than resolved by precedence. A caller that passes a design
    *and* a geometry has two different intents in one request, and silently honouring one of
    them produces a job whose inputs are not what the caller thinks they are.
    """

    design: str | None = Field(default=None, description="A design reference, `design:d-1`.")
    solver: str | None = Field(default=None, description="A capability name, for inline use.")
    geometry: dict[str, Any] | None = Field(default=None, description="Inline geometry object.")
    params: dict[str, Any] = Field(default_factory=dict, description="Solver parameters.")
    conditions: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description=(
            "An inline load case (#85), for the inline form. A design names its load cases "
            "the way it names its geometry, so passing both is refused for the same reason."
        ),
    )

    @model_validator(mode="after")
    def _one_form_only(self) -> "SubmitParams":
        inline = self.solver is not None or self.geometry is not None or bool(self.conditions)
        if self.design is not None and inline:
            raise ValueError(
                "pass either `design` or `solver` + `geometry` (with `conditions` if the "
                "solve needs a load case), not both: a design already names its solver, its "
                "geometry and its load cases"
            )
        if self.design is None and not (self.solver and self.geometry):
            raise ValueError("pass a `design`, or both `solver` and `geometry`")
        return self


def _params(raw: Any) -> dict[str, Any]:
    """Named params as a dict, or a `MethodError` explaining why not."""
    from . import errors as rpc_errors

    if raw is None:
        return {}
    if isinstance(raw, list):
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            "params must be an object with named fields, not an array",
            {"type": "PositionalParams"},
        )
    if not isinstance(raw, dict):
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"params must be an object, got {type(raw).__name__}",
            {"type": "MalformedParams"},
        )
    return raw


def _required(params: dict[str, Any], name: str) -> Any:
    from . import errors as rpc_errors

    if name not in params:
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"missing required parameter {name!r}",
            {"type": "MissingParameter", "parameter": name},
        )
    return params[name]


def _bool(params: dict[str, Any], name: str, default: bool) -> bool:
    """A JSON boolean, or a `MethodError` naming what arrived instead.

    Not `bool(params.get(name))`, which was the first implementation and was wrong in the way
    that matters: `bool("false")` is `True`, so a client sending the string `"false"` — which
    a shell wrapper or a loosely typed client easily does — would silently get the *opposite*
    of what it asked for. For `inline_schemas` that means a few kilobytes of JSON Schema
    arriving in a context window that asked not to have it. Raised in review of #45.
    """
    from . import errors as rpc_errors

    if name not in params or params[name] is None:
        return default
    value = params[name]
    if not isinstance(value, bool):
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"{name!r} must be true or false, got {type(value).__name__}",
            {"type": "WrongParameterType", "parameter": name, "expected": "boolean"},
        )
    return value


def _int(params: dict[str, Any], name: str, default: int, *, minimum: int = 0) -> int:
    """A JSON integer at or above ``minimum``, or a `MethodError`.

    `int(params.get(name, default))` was the first implementation, and it turned malformed
    input into a *server* failure: `int({})` raises `TypeError`, which reaches the dispatcher's
    last-resort handler and is reported as `-32603 internal error`. That is precisely the
    distinction :class:`MethodError` exists to keep — the caller sent something wrong, and
    telling it the server has a bug sends it to the wrong place entirely. Raised in review
    of #45.

    Two smaller things it also fixes. `True` is an `int` in Python, so `int(True)` is a
    perfectly good `1` and `limit=true` would have been accepted. And a *negative* `since` or
    `limit` reaches a list slice, where `stored[-5:]` is a valid expression meaning something
    nobody asked for — a caller polling with `since=-1` would be handed the end of the log
    and told it was the beginning.
    """
    from . import errors as rpc_errors

    if name not in params or params[name] is None:
        return default
    value = params[name]
    # JSON has one number type, so an encoder emitting `50.0` for fifty is being correct
    # rather than sloppy — accepted when it is integral, refused when it is not.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"{name!r} must be an integer, got {type(value).__name__}",
            {"type": "WrongParameterType", "parameter": name, "expected": "integer"},
        )
    if isinstance(value, float) and not value.is_integer():
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"{name!r} must be a whole number, got {value}",
            {"type": "WrongParameterType", "parameter": name, "expected": "integer"},
        )
    number = int(value)
    if number < minimum:
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"{name!r} must be at least {minimum}, got {number}",
            {"type": "ParameterOutOfRange", "parameter": name, "minimum": minimum},
        )
    return number


def _model(model: type[BaseModel], params: dict[str, Any]) -> Any:
    """Validate params against a model, reporting failure the way the core does.

    `data.errors` is pydantic's structured list — the same payload
    :class:`~fenixspoon.core.errors.InvalidParams` carries over HTTP, so a caller writing one
    error handler for both transports writes it once.
    """
    from . import errors as rpc_errors

    try:
        return model.model_validate(params)
    except ValidationError as exc:
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            f"params do not match {model.__name__}",
            {"type": "InvalidParams", "errors": _jsonable_errors(exc)},
        ) from exc


def _jsonable_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """pydantic's error list, minus the parts that do not survive JSON.

    `ctx` can hold the original exception object, and `url` is a docs link that costs bytes
    on a transport whose whole point is that the caller has a context window.
    """
    out = []
    for entry in exc.errors():
        out.append(
            {
                "type": entry.get("type"),
                "loc": [str(part) for part in entry.get("loc", ())],
                "msg": entry.get("msg"),
            }
        )
    return out


def _geometry(raw: Any) -> Geometry:
    from . import errors as rpc_errors

    try:
        return _GEOMETRY.validate_python(raw)
    except ValidationError as exc:
        raise MethodError(
            rpc_errors.INVALID_PARAMS,
            "geometry does not match any known geometry type",
            {"type": "InvalidGeometry", "errors": _jsonable_errors(exc)},
        ) from exc


# --------------------------------------------------------------------------- handlers
#
# Each takes the core, the principal and the named params, and returns something the
# encoder can render. Async only where the core call is: `job.submit` and `job.cancel`
# await the execution backend, and nothing else does.


def environment_inspect(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    del params
    return core.environment(principal)


def capability_list(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    del principal, params
    return core.capability_list()


def capability_describe(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    del principal
    return core.capability_describe(
        _required(params, "name"),
        params.get("sections"),
        inline_schemas=_bool(params, "inline_schemas", False),
    )


def capability_schema(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    del principal
    return core.capability_schema(_required(params, "name"))


def workspace_open(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    del principal, params
    return core.workspace_info()


def workspace_list(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.objects(principal, params.get("type"))


def object_create(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.create_object(
        _required(params, "type"),
        _required(params, "body"),
        principal,
        params.get("label"),
    )


def object_get(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.object(_required(params, "ref"), principal)


def object_revisions(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    ref = _required(params, "ref")
    return {"ref": ref, "revisions": core.object_revisions(ref, principal)}


def object_patch(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.patch_object(
        _required(params, "ref"),
        _required(params, "patch"),
        principal,
        params.get("label"),
    )


def design_resolve(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.resolve_design(_required(params, "ref"), principal)


async def job_submit(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    request = _model(SubmitParams, params)
    if request.design is not None:
        job = await core.submit_design(request.design, principal)
    else:
        job = await core.submit(
            request.solver,
            _geometry(request.geometry),
            request.params,
            principal,
            conditions=request.conditions,
        )
    # Field-for-field what `POST /api/v1/jobs` answers, including `cached`. Built as a dict
    # rather than by importing `api.JobCreated`, because that module imports FastAPI and
    # this transport must work without it — `test_the_submit_reply_matches_the_http_model`
    # is what keeps the two from drifting apart now that they share no class.
    return {"job_id": job.id, "status": job.status, "cached": job.reused > 0}


def job_get(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return JobStatus.from_job(core.job(_required(params, "job_id"), principal))


def job_list(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    limit = _int(params, "limit", 50, minimum=1)
    offset = _int(params, "offset", 0)
    jobs, total = core.history(principal, limit=limit, offset=offset)
    return {
        "jobs": [JobStatus.from_job(job) for job in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def job_cancel(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return JobStatus.from_job(await core.cancel(_required(params, "job_id"), principal))


def job_events(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.job_events(
        _required(params, "job_id"),
        principal,
        since=_int(params, "since", 0),
        limit=_int(params, "limit", 200, minimum=1),
    )


def job_provenance(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.provenance(_required(params, "job_id"), principal)


def jobs_for_object(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    jobs = core.jobs_for_object(
        _required(params, "ref"), principal, limit=_int(params, "limit", 50, minimum=1)
    )
    return {"jobs": [JobStatus.from_job(job) for job in jobs]}


async def study_run(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return await core.run_study(_required(params, "ref"), principal)


def study_get(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.study_report(_required(params, "ref"), principal)


def result_get(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    return core.result_levels(_required(params, "job_id"), principal, params.get("levels"))


def result_query(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    job_id = _required(params, "job_id")
    query = _model(FieldQuery, {k: v for k, v in params.items() if k != "job_id"})
    return core.query_result(job_id, query, principal)


def artifact_get(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    handle = core.artifact(_required(params, "job_id"), _required(params, "name"), principal)
    # A path, not bytes. The caller is a process on this machine — that is the premise of the
    # transport — so handing it the filename lets it open a 40 MB VTK file with the tools
    # that read VTK files, instead of base64-ing it through a pipe into a context window.
    return {
        "name": handle.name,
        "content_type": handle.content_type,
        "size": handle.size,
        "path": str(handle.path),
    }


def rpc_describe(core: FenixSpoonCore, principal: Principal, params: dict) -> Any:
    """What this endpoint speaks — the pipe's answer to "what can I call?".

    A caller connecting over HTTP has OpenAPI; a caller that spawned a process has nothing
    until it asks. Deliberately tiny: names, the protocol version and the framing, so that
    discovering the vocabulary costs a few hundred bytes rather than every method's schema.
    """
    del core, principal, params
    return {
        "protocol_version": PROTOCOL_VERSION,
        "jsonrpc": "2.0",
        "framing": {"writes": "ndjson", "reads": ["ndjson", "content-length"]},
        "batch": False,
        "methods": method_names(),
        "notifications": ["job.progress"],
    }


#: The vocabulary. Names are dotted `noun.verb`, matching the design draft's operation table
#: so that one page describes what every transport exposes.
METHODS: dict[str, Any] = {
    "rpc.describe": rpc_describe,
    "environment.inspect": environment_inspect,
    "capability.list": capability_list,
    "capability.describe": capability_describe,
    "capability.schema": capability_schema,
    "workspace.open": workspace_open,
    "workspace.list": workspace_list,
    "object.create": object_create,
    "object.get": object_get,
    "object.revisions": object_revisions,
    "object.patch": object_patch,
    "design.resolve": design_resolve,
    "job.submit": job_submit,
    "job.get": job_get,
    "job.list": job_list,
    "job.cancel": job_cancel,
    "job.events": job_events,
    "job.provenance": job_provenance,
    "job.for_object": jobs_for_object,
    "study.run": study_run,
    "study.get": study_get,
    "result.get": result_get,
    "result.query": result_query,
    "artifact.get": artifact_get,
}

# The vocabulary is now the design draft's table in full: #48 defined the study object, so
# `study.run` and `study.get` stopped being names with nothing behind them. One study kind is
# implemented, which `study.run` says plainly rather than implying a framework.

#: Methods whose effect is on the connection rather than on the core, so they have no handler
#: here — :class:`~fenixspoon.rpc.server.RpcServer` owns them. They are still part of the
#: vocabulary a caller can invoke, so they are named rather than left for someone to discover
#: by having `rpc.describe` not mention them.
CONNECTION_METHODS = ("job.subscribe", "job.unsubscribe")

__all__ = ["CONNECTION_METHODS", "METHODS", "MethodError", "SubmitParams", "method_names"]


def method_names() -> list[str]:
    """Everything callable on this transport, core-bound and connection-scoped alike."""
    return sorted([*METHODS, *CONNECTION_METHODS])


def resolve(name: str):
    """The handler for a method name, or a `MethodError` a caller can act on."""
    from . import errors as rpc_errors

    handler = METHODS.get(name)
    if handler is None:
        raise MethodError(
            rpc_errors.METHOD_NOT_FOUND,
            f"unknown method {name!r}",
            # The list rather than a bare refusal: a caller that misspelled `job.sumbit`
            # gets its answer here instead of over another round trip, and it is one of the
            # cheapest payloads on this transport.
            {"type": "UnknownMethod", "methods": method_names()},
        )
    return handler
