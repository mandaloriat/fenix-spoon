"""Progressive capability discovery (roadmap M2.5, issue #43).

`GET /api/v1/solvers` answers one question — *give me everything about everything* — and
answers it well for a form generator, which needs every JSON Schema to build every control.
It is the wrong answer for a caller with a bounded context window that wants to know whether
magnetostatics is installed at all. Measured on the three mock adapters, that reply is 4.0 kB
against 0.5 kB for `capability.list`; both scale with the number of solvers installed, so the
ratio is what to look at rather than either number.

So three operations, each answering a smaller question than the last:

- :func:`environment_info` — what this installation *is*. Versions, what imported, which
  backends, the configured limits, this principal's quotas and usage. No schemas.
- :func:`capability_summaries` — one line per installed capability. No schemas, no prose.
- :func:`describe_capability` — one capability, in the sections the caller asked for.
  Unrequested sections are absent from the payload rather than null, and the params schema
  is a *reference* by default, resolved by a second call only if it is really wanted.

The reference mechanism is worth being precise about, because the obvious reading of it is
wrong: see :class:`ParamSummary` for what the `params` section actually saves and what it
does not.

Nothing here is HTTP: sections are a list of strings, the schema reference is an opaque
identifier rather than a URL, and the module imports no web framework. That is the same
rule the rest of :mod:`fenixspoon.core` follows, and it is what lets the JSON-RPC and MCP
adapters (#45, #49) expose these three operations without going through a server.
"""

import platform
import sys
from typing import Any

from pydantic import BaseModel, Field

from .. import __version__
from ..protocol import PROTOCOL_VERSION
from ..solvers.base import (
    ArtifactSpec,
    CapabilityExample,
    CapabilityFeatures,
    MetricSpec,
    Solver,
)
from . import errors
from .identity import Principal

#: Sections :func:`describe_capability` knows, in the order they appear in a full answer.
#: A tuple rather than an enum so the error message can list them, and so adding one is a
#: single edit here plus the branch that builds it.
SECTIONS: tuple[str, ...] = (
    "geometries",
    "params",
    "metrics",
    "artifacts",
    "cost",
    "features",
    "requirements",
    "examples",
)

#: Packages worth reporting on. `dolfinx` and `gmsh` decide whether the FEniCSx adapters
#: exist at all; `mpi4py` and `petsc4py` decide how they behave; `numpy` is what the mock
#: solvers are, and its version has changed answers before.
_REPORTED_PACKAGES = ("numpy", "dolfinx", "gmsh", "mpi4py", "petsc4py")


class PackageInfo(BaseModel):
    """Whether one dependency imported in this process, and at what version."""

    name: str = Field(description="Import name, e.g. `dolfinx`.")
    imported: bool = Field(
        description=(
            "True when the module is loaded in this process. Not the same as installed: "
            "`fenixspoon.solvers` attempts the FEniCSx imports at startup, so a broken "
            "install reports false here — which is the honest answer, because the FEniCSx "
            "capabilities are equally absent."
        )
    )
    version: str | None = Field(
        default=None, description="The module's `__version__`, when it has one."
    )


class MpiInfo(BaseModel):
    """Whether this process has MPI, and how many ranks it is running on."""

    available: bool = Field(description="True when `mpi4py` imported.")
    size: int | None = Field(
        default=None,
        description=(
            "Ranks in `COMM_WORLD`; 1 for the ordinary single-process server. Null when "
            "MPI is unavailable. Note no shipped capability declares `features.mpi`."
        ),
    )


class LimitsInfo(BaseModel):
    """The server-side caps a caller will meet, so it can size a request in advance."""

    job_timeout_seconds: float = Field(
        description="Wall-clock ceiling on one solve; 0 when disabled (`FENIXSPOON_JOB_TIMEOUT`)."
    )
    max_cells: int = Field(
        description="Submit-time cell budget; 0 when disabled (`FENIXSPOON_MAX_CELLS`)."
    )
    job_ttl_seconds: float = Field(
        description="How long a finished job survives; 0 keeps it forever (`FENIXSPOON_JOB_TTL`)."
    )
    max_workers: int = Field(
        description="Concurrent solves this process will run (`FENIXSPOON_MAX_WORKERS`)."
    )


class QuotaInfo(BaseModel):
    """This principal's per-user limits. `0` means unlimited, which is every default."""

    concurrent_jobs: int = Field(description="Jobs this principal may have in flight at once.")
    jobs_per_hour: int = Field(description="Submissions allowed in a rolling hour.")
    artifact_bytes: int = Field(description="Total artifact bytes this principal may store.")


class UsageInfo(BaseModel):
    """What this principal is using right now, against :class:`QuotaInfo`."""

    concurrent_jobs: int = Field(description="Jobs currently queued or running.")
    jobs_last_hour: int = Field(description="Jobs submitted in the last hour.")
    artifact_bytes: int = Field(description="Bytes of stored artifacts.")


class EnvironmentInfo(BaseModel):
    """What `environment.inspect` returns: this installation, in a few hundred bytes.

    Deliberately schema-free. It answers "can this machine do the kind of thing I want, and
    under what limits" — the questions that decide whether it is worth asking anything else.
    """

    implementation: str = Field(description="Version of the `fenixspoon` package.")
    protocol: str = Field(description="Wire-contract version, `MAJOR.MINOR`; same as `/version`.")
    python: str = Field(description="Interpreter version running the server.")
    platform: str = Field(description="OS and machine, as `platform.platform()` reports it.")
    packages: list[PackageInfo] = Field(
        description="Dependencies whose presence or version changes what a solve can do."
    )
    mpi: MpiInfo = Field(description="MPI availability and rank count.")
    execution_backend: str = Field(
        description="Where solves run: `in-process` (thread pool) or `arq` (worker containers)."
    )
    event_bus: str = Field(
        description=(
            "How progress is delivered: `in-process` or `redis`. Reported next to the "
            "backend because the two must agree — the symptom of a mismatch is a progress "
            "stream that stays silent."
        )
    )
    store: str = Field(description="Job store backend: `sqlite` or `memory`.")
    data_dir: str = Field(
        description="Absolute path holding the job database, results and artifacts."
    )
    capabilities: int = Field(description="How many capabilities are installed.")
    limits: LimitsInfo = Field(description="Server-side caps applied to every submission.")
    principal: str = Field(description="Who the server thinks is asking.")
    quotas: QuotaInfo = Field(description="That principal's limits.")
    usage: UsageInfo = Field(description="That principal's current usage against them.")
    cache: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-addressed result cache state. Null on this server: the cache is issue "
            "#47 and does not exist yet. Reported as null rather than omitted so a caller "
            "can tell 'no cache here' from 'this server is too old to say'."
        ),
    )


class CapabilitySummary(BaseModel):
    """One line of `capability.list`: enough to choose, not enough to submit.

    No `description` and no schemas. The whole point of the operation is that listing every
    capability costs a fraction of what describing one does, so a caller can look before it
    commits its context to a schema it may not need.
    """

    name: str = Field(description="Identifier to pass as `solver`, or to `capability.describe`.")
    title: str = Field(description="Short human-readable name.")
    physics: str = Field(
        description="Coarse tag to filter on: `potential-flow`, `magnetostatics`, ..."
    )
    geometry_types: list[str] = Field(description="Geometry `type` values it accepts.")
    availability: str = Field(
        description="`mock` for the NumPy stand-in, `fenicsx` for a real FEniCSx solve."
    )


class ParamSummary(BaseModel):
    """One parameter, flattened out of the JSON Schema into a line a caller can act on.

    **This is flatter than the schema, not smaller than it.** Measured on the adapters
    shipped today it comes out slightly *larger* — 1.5 kB against 1.5 kB for
    `mock.heat2d` — because their schemas are already flat and the descriptions dominate
    either way. Claiming a size win here would be inventing one.

    What it buys is that nothing has to be resolved. A `Literal` param reaches a caller as
    `$ref` → `$defs` → `enum`, and following that indirection to learn "this takes
    grid2d or mesh2d" is work a summary can do once. The property that *does* hold as
    schemas grow is boundedness: a nested params model expands `$defs`, while this stays
    one line per top-level parameter.

    The size win in this module is elsewhere — in `capability.list`, and in not sending
    sections nobody asked for.
    """

    name: str = Field(description="Parameter key as submitted in `params`.")
    type: str = Field(description="JSON Schema type, or `enum` for a closed set of values.")
    required: bool = Field(description="True when there is no default and it must be supplied.")
    default: Any | None = Field(default=None, description="Value used when it is omitted.")
    description: str | None = Field(default=None, description="What it controls.")
    minimum: float | None = Field(default=None, description="Inclusive or exclusive lower bound.")
    maximum: float | None = Field(default=None, description="Inclusive or exclusive upper bound.")
    choices: list[Any] | None = Field(
        default=None, description="Allowed values, when the parameter is an enum."
    )


class ParamsSection(BaseModel):
    """The `params` section: a compact parameter list, plus a way to get the real schema."""

    params: list[ParamSummary] = Field(description="One entry per top-level parameter.")
    schema_ref: str = Field(
        description=(
            "Opaque reference to the full JSON Schema, `schema:params/<capability>`. Resolve "
            "it with `capability.schema` — over HTTP, "
            "`GET /api/v1/capabilities/{name}/schema`."
        )
    )
    json_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The full JSON Schema, present only when `inline_schemas` was requested. Named "
            "`json_schema` because `schema` shadows a pydantic attribute."
        ),
    )


class GeometriesSection(BaseModel):
    """The `geometries` section: which geometry kinds this capability accepts."""

    kinds: list[str] = Field(
        description="Accepted `type` discriminator values, e.g. `[\"regions2d\"]`."
    )
    note: str = Field(
        description=(
            "Where the geometry schemas live. They are protocol-level rather than "
            "per-capability — every capability accepting `regions2d` accepts the same "
            "`regions2d` — so they are not repeated here."
        )
    )


class CostSection(BaseModel):
    """The `cost` section: can this request be sized before it is submitted?

    Nearly free to expose, because `Solver.estimate_cells` already exists for the
    submit-time budget check. Exposing it is the difference between a caller choosing a
    mesh size and a caller discovering the limit by being refused.
    """

    estimates_cells: bool = Field(
        description=(
            "True when the adapter implements `estimate_cells`, so an over-budget job is "
            "refused at submit with a number. False means the wall-clock timeout is the "
            "only backstop."
        )
    )
    max_cells: int = Field(description="This server's cell budget; 0 when disabled.")
    job_timeout_seconds: float = Field(
        description="This server's wall-clock ceiling on one solve; 0 when disabled."
    )


class RequirementsSection(BaseModel):
    """The `requirements` section: what this capability needs, and what is here.

    Always satisfied for a listed capability — a solver that could not import never reached
    the registry — which is exactly why the *versions* are the useful part.
    """

    packages: list[PackageInfo] = Field(
        description="The adapter's declared imports, with their state in this process."
    )
    satisfied: bool = Field(
        description="True when every declared requirement imported. Always true for a listed "
        "capability; false would mean the registry and the process disagree."
    )


class CapabilityDescription(BaseModel):
    """What `capability.describe` returns. Every section is optional and omitted unless asked.

    `name` is always present, so an answer is self-identifying no matter how narrow the
    request. Everything else — including the title and the physics tag, which
    `capability.list` already carries — is a section, because "returns metrics and nothing
    else" should mean what it says.
    """

    name: str = Field(description="The capability this describes.")
    title: str | None = Field(default=None, description="Short human-readable name.")
    description: str | None = Field(default=None, description="What it computes, and how.")
    physics: str | None = Field(default=None, description="Coarse physics tag.")
    availability: str | None = Field(default=None, description="`mock` or `fenicsx`.")
    geometries: GeometriesSection | None = Field(
        default=None, description="Section `geometries`: which geometry kinds it accepts."
    )
    params: ParamsSection | None = Field(
        default=None, description="Section `params`: the parameters, and a schema reference."
    )
    metrics: list[MetricSpec] | None = Field(
        default=None, description="Section `metrics`: the engineering scalars it reports."
    )
    artifacts: list[ArtifactSpec] | None = Field(
        default=None, description="Section `artifacts`: files a solve may write."
    )
    cost: CostSection | None = Field(
        default=None, description="Section `cost`: whether a request can be sized in advance."
    )
    features: CapabilityFeatures | None = Field(
        default=None, description="Section `features`: sweep, gradient and MPI support."
    )
    requirements: RequirementsSection | None = Field(
        default=None, description="Section `requirements`: declared imports and their state here."
    )
    examples: list[CapabilityExample] | None = Field(
        default=None, description="Section `examples`: known-good parameter sets."
    )


# ------------------------------------------------------------------------ builders


def _module_version(name: str) -> PackageInfo:
    """Report a package without importing it.

    `sys.modules` rather than `importlib.metadata`: the question is not "is a distribution
    installed" but "did this process manage to import it", which is what decides whether
    the FEniCSx capabilities are in the registry. `fenixspoon.solvers` has already made
    that attempt by the time anything can call this, so the lookup is both free and exact.
    """
    module = sys.modules.get(name)
    if module is None:
        return PackageInfo(name=name, imported=False)
    version = getattr(module, "__version__", None)
    return PackageInfo(name=name, imported=True, version=str(version) if version else None)


def _mpi_info() -> MpiInfo:
    mpi4py = sys.modules.get("mpi4py")
    if mpi4py is None:
        return MpiInfo(available=False)
    try:
        from mpi4py import MPI

        return MpiInfo(available=True, size=int(MPI.COMM_WORLD.Get_size()))
    except Exception:  # pragma: no cover - a broken mpi4py is not this call's problem
        # Reporting "MPI is there but would not answer" beats failing an inspect call: the
        # operation exists to describe a possibly-broken installation.
        return MpiInfo(available=True, size=None)


def environment_info(
    *,
    principal: Principal,
    usage: UsageInfo,
    execution_backend: str,
    event_bus: str,
    store: str,
    data_dir: str,
    capabilities: int,
    limits: LimitsInfo,
) -> EnvironmentInfo:
    """Assemble `environment.inspect`. The caller supplies what only it can know."""
    return EnvironmentInfo(
        implementation=__version__,
        protocol=PROTOCOL_VERSION,
        python=platform.python_version(),
        platform=platform.platform(),
        packages=[_module_version(name) for name in _REPORTED_PACKAGES],
        mpi=_mpi_info(),
        execution_backend=execution_backend,
        event_bus=event_bus,
        store=store,
        data_dir=data_dir,
        capabilities=capabilities,
        limits=limits,
        principal=principal.id,
        quotas=QuotaInfo(
            concurrent_jobs=principal.quotas.concurrent_jobs,
            jobs_per_hour=principal.quotas.jobs_per_hour,
            artifact_bytes=principal.quotas.artifact_bytes,
        ),
        usage=usage,
    )


def summarize(solver_cls: type[Solver]) -> CapabilitySummary:
    return CapabilitySummary(
        name=solver_cls.name,
        title=solver_cls.title,
        physics=solver_cls.physics,
        geometry_types=list(solver_cls.geometry_types),
        availability=solver_cls.availability,
    )


def schema_ref(name: str) -> str:
    """The opaque reference a caller resolves to get a capability's full params schema.

    A `schema:` scheme rather than a URL, because the core does not know it is being served
    over HTTP — the same string has to mean something to a JSON-RPC caller reading it over
    a pipe.
    """
    return f"schema:params/{name}"


def _param_summaries(json_schema: dict[str, Any]) -> list[ParamSummary]:
    """Flatten a pydantic-generated JSON Schema into one line per parameter.

    Handles the two shapes pydantic emits that matter here: a plain typed property, and a
    `Literal[...]` field, which becomes an `$ref` to a `$defs` enum. Anything else falls
    back to reporting the type as `unknown` rather than guessing — the full schema is one
    call away, and a wrong summary is worse than an incomplete one.
    """
    defs = json_schema.get("$defs", {})
    required = set(json_schema.get("required", []))
    summaries = []
    for name, prop in json_schema.get("properties", {}).items():
        resolved = prop
        ref = prop.get("$ref") or next(
            (item["$ref"] for item in prop.get("allOf", []) if "$ref" in item), None
        )
        if ref and ref.startswith("#/$defs/"):
            resolved = {**defs.get(ref.split("/")[-1], {}), **prop}

        choices = resolved.get("enum")
        summaries.append(
            ParamSummary(
                name=name,
                type="enum" if choices else str(resolved.get("type", "unknown")),
                required=name in required,
                default=prop.get("default"),
                description=prop.get("description") or resolved.get("description"),
                # Pydantic writes `ge`/`le` as `minimum`/`maximum` and `gt`/`lt` as the
                # exclusive pair. The distinction is not worth a second field in a summary
                # whose job is "roughly how big may this be" — the schema has it exactly.
                minimum=resolved.get("minimum", resolved.get("exclusiveMinimum")),
                maximum=resolved.get("maximum", resolved.get("exclusiveMaximum")),
                choices=choices,
            )
        )
    return summaries


def describe_capability(
    solver_cls: type[Solver],
    *,
    sections: list[str] | None = None,
    inline_schemas: bool = False,
    max_cells: int,
    job_timeout: float,
) -> CapabilityDescription:
    """Build `capability.describe` for one capability.

    ``sections=None`` means every section — the friendly default for a human at a CLI.
    An explicit list means those and nothing else, which is the behaviour an agent wants
    and the one the acceptance criterion names.
    """
    if sections is None:
        wanted = set(SECTIONS)
        identify = True
    else:
        unknown = [s for s in sections if s not in SECTIONS]
        if unknown:
            raise errors.UnknownSection(unknown, list(SECTIONS))
        wanted = set(sections)
        identify = False

    out = CapabilityDescription(name=solver_cls.name)
    if identify:
        out.title = solver_cls.title
        out.description = solver_cls.description
        out.physics = solver_cls.physics
        out.availability = solver_cls.availability

    if "geometries" in wanted:
        out.geometries = GeometriesSection(
            kinds=list(solver_cls.geometry_types),
            note=(
                "Geometry schemas are protocol-level, not per-capability: see the geometry "
                "models in the protocol reference."
            ),
        )
    if "params" in wanted:
        json_schema = solver_cls.Params.model_json_schema()
        out.params = ParamsSection(
            params=_param_summaries(json_schema),
            schema_ref=schema_ref(solver_cls.name),
            json_schema=json_schema if inline_schemas else None,
        )
    if "metrics" in wanted:
        out.metrics = list(solver_cls.metrics)
    if "artifacts" in wanted:
        out.artifacts = list(solver_cls.artifacts)
    if "cost" in wanted:
        out.cost = CostSection(
            estimates_cells=solver_cls.estimates_cost(),
            max_cells=max_cells,
            job_timeout_seconds=job_timeout,
        )
    if "features" in wanted:
        out.features = solver_cls.features
    if "requirements" in wanted:
        packages = [_module_version(name) for name in solver_cls.requires]
        out.requirements = RequirementsSection(
            packages=packages, satisfied=all(p.imported for p in packages)
        )
    if "examples" in wanted:
        out.examples = list(solver_cls.examples)
    return out


__all__ = [
    "SECTIONS",
    "CapabilityDescription",
    "CapabilitySummary",
    "CostSection",
    "EnvironmentInfo",
    "GeometriesSection",
    "LimitsInfo",
    "MpiInfo",
    "PackageInfo",
    "ParamSummary",
    "ParamsSection",
    "QuotaInfo",
    "RequirementsSection",
    "UsageInfo",
    "describe_capability",
    "environment_info",
    "schema_ref",
    "summarize",
]
