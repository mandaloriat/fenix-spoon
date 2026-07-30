#!/usr/bin/env python3
"""Generate the protocol reference page from the pydantic models (issue #17).

    python server/tools/generate_protocol_reference.py          # write the page
    python server/tools/generate_protocol_reference.py --check  # fail if it is stale

The point is that documentation of the wire format cannot drift from the code that
enforces it. The page is generated *into the repository* rather than at site-build time,
for two reasons: a field's description changing shows up as a diff in the pull request
that changed it, and CI can fail on a stale page without building the docs site at all.

Only the protocol models are listed, deliberately. Solver parameters are per-deployment —
which solvers exist depends on what is installed — so rendering them here would produce a
page that differs between a plain checkout and a FEniCSx one, and a "regenerate and diff"
check that fails for the wrong reason. `GET /api/v1/solvers` reports them at runtime,
which is the honest place for something that varies by deployment.
"""

import argparse
import sys
import types
from pathlib import Path
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = REPO_ROOT / "docs" / "reference-protocol.md"

HEADER = """<!--
  GENERATED FILE — do not edit.
  Regenerate with: python server/tools/generate_protocol_reference.py
  CI fails if this file is out of date with the models.
-->

# Protocol reference

Every table below is generated from the pydantic models the server validates against, so
this page cannot describe a field the code does not have. The prose companion —
what the endpoints do, what the lifecycle looks like, what the guarantees are — is in
[the wire protocol guide](04-wire-protocol.md).

Solver parameters are not here: which solvers exist depends on what a deployment has
installed, and each one publishes its own JSON Schema at `GET /api/v1/solvers`.
"""


def type_name(annotation) -> str:
    """A readable type for a table cell, not a faithful repr."""
    if annotation is None or annotation is type(None):
        return "null"
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Both spellings: `Optional[X]` gives typing.Union, `X | None` gives types.UnionType,
    # and missing the second one leaves a raw `|` in the cell that splits the table.
    if origin is Union or origin is types.UnionType:
        # `X | None` reads better as "X (optional)" once the None is stripped, but the
        # union may be a real one (str | int), so only collapse the None case.
        non_none = [a for a in args if a is not type(None)]
        rendered = " \\| ".join(type_name(a) for a in non_none)
        return f"{rendered} \\| null" if len(non_none) != len(args) else rendered
    if origin in (list, set, frozenset):
        return f"{origin.__name__}[{', '.join(type_name(a) for a in args)}]"
    if origin is tuple:
        return f"tuple[{', '.join(type_name(a) for a in args)}]"
    if origin is dict:
        return f"dict[{', '.join(type_name(a) for a in args)}]"
    if origin is Literal:
        # `Literal` alone would hide the discriminator value, which is the single most
        # useful thing about these fields.
        return " \\| ".join(repr(a) for a in args)
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    if hasattr(annotation, "__qualname__"):
        return annotation.__qualname__
    return str(annotation).replace("typing.", "")


def discriminated_members(annotated) -> tuple[set[type], str]:
    """Members of a discriminated union, and the field that tags them.

    Derived rather than hard-coded, so adding a geometry kind cannot leave the reference
    describing its discriminator as optional.
    """
    args = get_args(annotated)
    if not args:
        return set(), ""
    union, *metadata = args
    for meta in metadata:
        discriminator = getattr(meta, "discriminator", None)
        if discriminator:
            return set(get_args(union)), str(discriminator)
    return set(), ""


def render_model(
    model: type[BaseModel], title: str | None = None, wire_required: str = ""
) -> str:
    lines = [f"## `{title or model.__name__}`", ""]
    doc = (model.__doc__ or "").strip()
    if doc:
        # Only the first paragraph: the rest is usually implementation notes for whoever
        # maintains the model, not contract for whoever consumes it.
        lines += [doc.split("\n\n")[0].replace("\n", " ").strip(), ""]

    lines += ["| Field | Type | Required | Default | Description |", "|---|---|---|---|---|"]
    for name, field in model.model_fields.items():
        # `is_required()` answers a Python question — can you construct this object
        # without the argument — and for a discriminated union member that is the wrong
        # question. Pydantic needs the tag present in the JSON to pick the member at all
        # (`union_tag_not_found`), so the model's default can never apply on the wire and
        # showing it would be describing a payload the server rejects.
        tag_field = name == wire_required
        required = "yes" if field.is_required() or tag_field else "no"
        default = "" if field.is_required() or tag_field else f"`{field.default!r}`"
        if default == "`PydanticUndefined`":
            default = ""
        description = (field.description or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{name}` | `{type_name(field.annotation)}` | {required} | {default} | "
            f"{description} |"
        )
    lines.append("")
    return "\n".join(lines)


def build() -> str:
    # Imported here so `--help` works without the package installed.
    from fenixspoon.api import JobCreated, JobList, JobRequest
    from fenixspoon.core.discovery import (
        CapabilityDescription,
        CapabilitySummary,
        CostSection,
        EnvironmentInfo,
        GeometriesSection,
        LimitsInfo,
        MpiInfo,
        PackageInfo,
        ParamsSection,
        ParamSummary,
        QuotaInfo,
        RequirementsSection,
        UsageInfo,
    )
    from fenixspoon.core.results import (
        ArtifactView,
        DiagnosticsView,
        FieldQuery,
        FieldQueryResult,
        FieldsView,
        LeveledResult,
        StatusView,
    )
    from fenixspoon.geometry import Domain2D, Geometry, Polygon2D, Region2D, Regions2D
    from fenixspoon.jobs import JobStatus
    from fenixspoon.protocol import (
        ArtifactRef,
        Grid2DData,
        Mesh2DData,
        ProgressEvent,
        ResultEnvelope,
        StatusEvent,
    )
    from fenixspoon.solvers.base import (
        ArtifactSpec,
        CapabilityExample,
        CapabilityFeatures,
        MetricSpec,
        SolverInfo,
    )

    sections = [
        ("Geometry", [Polygon2D, Domain2D, Region2D, Regions2D]),
        ("Jobs", [JobRequest, JobCreated, JobStatus, JobList]),
        ("Events", [ProgressEvent, StatusEvent]),
        ("Results", [ResultEnvelope, Grid2DData, Mesh2DData, ArtifactRef]),
        (
            "Compact results",
            [
                LeveledResult,
                StatusView,
                DiagnosticsView,
                FieldsView,
                ArtifactView,
                FieldQuery,
                FieldQueryResult,
            ],
        ),
        # The exhaustive form first, then the progressive one (protocol 1.2, #43). Both are
        # here because both are contract: `/solvers` is not deprecated by `/capabilities`,
        # it answers a different question.
        ("Discovery", [SolverInfo]),
        (
            "Environment",
            [EnvironmentInfo, PackageInfo, MpiInfo, LimitsInfo, QuotaInfo, UsageInfo],
        ),
        (
            "Capabilities",
            [
                CapabilitySummary,
                CapabilityDescription,
                GeometriesSection,
                ParamsSection,
                ParamSummary,
                MetricSpec,
                ArtifactSpec,
                CostSection,
                CapabilityFeatures,
                RequirementsSection,
                CapabilityExample,
            ],
        ),
    ]

    # `Polygon2D` also has a defaulted `type`, but it is a plain nested model rather than
    # a union member, so there its default really does apply. Only the tagged members are
    # promoted.
    members, discriminator = discriminated_members(Geometry)

    parts = [HEADER]
    for heading, models in sections:
        parts.append(f"\n# {heading}\n")
        parts.extend(
            render_model(model, wire_required=discriminator if model in members else "")
            for model in models
        )
    return "\n".join(parts).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the checked-in page differs from what the models produce",
    )
    args = parser.parse_args(argv)

    generated = build()
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.is_file() else ""
        if current != generated:
            print(
                f"{OUTPUT.relative_to(REPO_ROOT)} is out of date with the protocol models.\n"
                "Run: python server/tools/generate_protocol_reference.py",
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.write_text(generated)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
