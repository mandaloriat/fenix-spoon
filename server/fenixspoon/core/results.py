"""Compact results: levels, metrics, diagnostics and field queries (M2.5, issue #46).

A finished solve has always been able to answer one question — "give me everything" — and a
`grid2d` result is tens of thousands of floats. That is right for the viewer widget, which
draws every one of them, and wrong for every other caller: an agent reasoning about the
answer, a CI check comparing against a baseline, a convergence script reading one number per
run. This module separates *what happened* from *the arrays*.

Five levels, requested independently:

| Level | What it carries | Cost |
|---|---|---|
| `status` | terminal status, timing, error | tens of bytes |
| `metrics` | the declared engineering scalars | hundreds of bytes |
| `diagnostics` | `stats`, convergence, residual, warnings | hundreds of bytes |
| `fields` | the full arrays | megabytes — never a default |
| `artifacts` | files by reference | reference only |

**The default omits `fields`.** That is the entire behavioural change: a caller that says
nothing gets an answer it can read, and the arrays are reached deliberately — by asking for
the level, by fetching the artifact, or by querying.

**The compact levels never touch the payload file.** Metrics, stats and diagnostics live in
database columns, so answering them is a row read rather than a multi-megabyte file read and
a JSON parse. A "compact" level that had to load the field arrays to avoid sending them would
have saved the smaller half of the cost: the local caller this milestone is for shares a
filesystem with the server, and what it cannot afford is the parse and the context, not the
bytes on a socket.

`result.query` covers what the declared metrics do not: any scalar a caller can name, from
any field, without the field moving. The arithmetic is in :mod:`fenixspoon.fields`; what is
here is resolving the job, resolving a named region against the workspace, and turning a
:class:`~fenixspoon.fields.FieldError` into the vocabulary the rest of the core speaks.
"""

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from .. import fields as fieldlib
from ..store import ResultSummary
from . import errors

#: Response levels, in the order a full answer presents them.
LEVELS: tuple[str, ...] = ("status", "metrics", "diagnostics", "fields", "artifacts")

#: What a caller gets for asking nothing. Everything except the arrays — the one level that
#: is measured in megabytes is the one level you have to ask for by name.
DEFAULT_LEVELS: tuple[str, ...] = ("status", "metrics", "diagnostics", "artifacts")


class StatusView(BaseModel):
    """The `status` level: did it finish, when, and why not."""

    status: str = Field(description="`done`, `failed` or `cancelled`.")
    error: str | None = Field(default=None, description="Why it failed; null otherwise.")
    created_at: str = Field(description="When the job was accepted (RFC 3339, UTC).")
    finished_at: str | None = Field(
        default=None, description="When it reached a terminal status."
    )
    seconds: float | None = Field(
        default=None, description="Wall-clock seconds the solve took, when the run recorded it."
    )


class DiagnosticsView(BaseModel):
    """The `diagnostics` level: what the solve cost and how well it went.

    `stats` is the existing thing, formalised rather than replaced — issue #46 was explicit
    that the diagnostics level should grow out of `stats` instead of opening a parallel
    channel beside it. What is genuinely new is the three fields below it, which had nowhere
    to live: `stats` is typed `dict[str, float]`, so a convergence flag could only have been
    smuggled in as 0.0/1.0 and a warning string not at all.
    """

    stats: dict[str, float] = Field(
        description="What it cost: `cells`, `dofs`, `iterations`, `seconds`. Server-defined."
    )
    converged: bool | None = Field(
        default=None,
        description="Whether an iterative solve reached tolerance. Null where it does not apply.",
    )
    residual: float | None = Field(default=None, description="Final residual, when iterative.")
    warnings: list[str] = Field(
        default=[], description="Non-fatal things worth knowing about this solve."
    )


class ArtifactView(BaseModel):
    """One file, by reference. The `artifacts` level never inlines bytes."""

    name: str = Field(description="Bare filename, e.g. `solution.vtk`.")
    content_type: str = Field(description="MIME type.")
    size: int = Field(description="Size on disk in bytes.")
    path: str = Field(
        description=(
            "Absolute path on the machine that ran it. A local caller opens this directly; "
            "an HTTP adapter replaces it with a URL to a route it serves."
        )
    )


class FieldsView(BaseModel):
    """The `fields` level: the arrays, in full, because somebody asked for them by name."""

    kind: str = Field(description="`grid2d` or `mesh2d`; selects the shape of `data`.")
    data: dict[str, Any] = Field(description="The full field payload.")


class LeveledResult(BaseModel):
    """What `result.get` returns. Unrequested levels are absent, not null.

    `job_id` and `solver` are always present for the same reason `capability.describe` always
    carries a `name`: an answer should be identifiable without correlating it back to the
    request that produced it.
    """

    job_id: str = Field(description="The job this describes.")
    solver: str = Field(description="Capability that produced it.")
    status: StatusView | None = Field(default=None, description="Level `status`.")
    metrics: dict[str, float] | None = Field(
        default=None,
        description=(
            "Level `metrics`: the declared engineering scalars, keyed by the names "
            "`capability.describe` reports. Empty if the capability declares none."
        ),
    )
    diagnostics: DiagnosticsView | None = Field(default=None, description="Level `diagnostics`.")
    fields: FieldsView | None = Field(default=None, description="Level `fields`.")
    artifacts: list[ArtifactView] | None = Field(default=None, description="Level `artifacts`.")


class FieldQuery(BaseModel):
    """One bounded question about one field.

    Deliberately a single model rather than an operation per method: every one of these is
    "reduce this field to something small", the arguments overlap heavily, and a transport
    that has to bind nine near-identical operations will get one of them subtly wrong.
    """

    field: str = Field(description="Scalar field name, as the result carries it (`T`, `speed`).")
    op: Literal[
        "max", "min", "mean", "integral", "at_point", "over_region", "section", "sample", "hotspots"
    ] = Field(description="What to compute.")
    at: list[float] | None = Field(
        default=None, description="`[x, y]` for `at_point`."
    )
    start: list[float] | None = Field(default=None, description="`[x, y]` start for `section`.")
    end: list[float] | None = Field(default=None, description="`[x, y]` end for `section`.")
    samples: int | None = Field(
        default=None,
        description=(
            "Sample budget for `section` and `sample`. Capped server-side — an uncapped "
            "budget is the whole field with extra steps."
        ),
    )
    count: int | None = Field(default=None, description="How many `hotspots` to return.")
    minimum: bool = Field(
        default=False, description="For `hotspots`: find the coldest rather than the hottest."
    )
    region: str | None = Field(
        default=None,
        description=(
            "Region name for `over_region`. Resolved against the geometry the job recorded "
            "in its workspace provenance."
        ),
    )


class FieldQueryResult(BaseModel):
    """A query's answer. `value` is whatever the operation returns — a scalar or a short list.

    Not a union of nine response models. The operations return genuinely different shapes and
    a discriminated union of them would be nine models a transport has to know about, for a
    payload a caller already knows the shape of because it chose the operation.
    """

    job_id: str = Field(description="Job that was queried.")
    field: str = Field(description="Field that was queried.")
    op: str = Field(description="Operation that was run.")
    result: dict[str, Any] = Field(
        description=(
            "The answer. `max`/`min` give `value` and `at`; `mean`/`integral` give `value`; "
            "`section`/`sample` give short parallel arrays; `hotspots` gives a handful of "
            "points. Every one is bounded."
        )
    )


def check_levels(levels: list[str] | None) -> tuple[str, ...]:
    """Validate a level selection, or return the default when there is none."""
    if levels is None:
        return DEFAULT_LEVELS
    unknown = [level for level in levels if level not in LEVELS]
    if unknown:
        raise errors.UnknownLevel(unknown, list(LEVELS))
    return tuple(levels)


def build(
    *,
    job_id: str,
    solver: str,
    status: str,
    error: str | None,
    created_at: str,
    finished_at: str | None,
    summary: ResultSummary | None,
    artifacts: list[ArtifactView],
    data: dict[str, Any] | None,
    levels: tuple[str, ...],
) -> LeveledResult:
    """Assemble the requested levels. The caller has already decided what to load."""
    out = LeveledResult(job_id=job_id, solver=solver)
    if "status" in levels:
        out.status = StatusView(
            status=status,
            error=error,
            created_at=created_at,
            finished_at=finished_at,
            seconds=(summary.stats.get("seconds") if summary else None),
        )
    if "metrics" in levels:
        out.metrics = dict(summary.metrics) if summary else {}
    if "diagnostics" in levels and summary is not None:
        out.diagnostics = DiagnosticsView(
            stats=dict(summary.stats),
            converged=summary.converged,
            residual=summary.residual,
            warnings=list(summary.warnings),
        )
    if "fields" in levels and summary is not None and data is not None:
        out.fields = FieldsView(kind=summary.kind, data=data)
    if "artifacts" in levels:
        out.artifacts = artifacts
    return out


def region_selection(
    geometry_body: dict[str, Any], region: str, xy: np.ndarray
) -> np.ndarray:
    """Boolean mask of which sample points fall inside a named region of a `regions2d`.

    Lives here rather than in :mod:`fenixspoon.fields` because it is the one part of a query
    that needs to know what a *geometry* is, and fields.py deliberately does not.
    """
    regions = geometry_body.get("regions")
    if not regions:
        raise errors.RegionUnavailable(
            region,
            "the geometry has no named regions; `over_region` needs a `regions2d` geometry",
        )
    names = [entry.get("name") for entry in regions]
    match = next((entry for entry in regions if entry.get("name") == region), None)
    if match is None:
        raise errors.RegionUnavailable(
            region, f"this geometry has regions {sorted(n for n in names if n)}"
        )
    polygon = np.asarray(match["shape"]["points"], dtype=float)
    return fieldlib.points_in_polygon(xy, polygon)
