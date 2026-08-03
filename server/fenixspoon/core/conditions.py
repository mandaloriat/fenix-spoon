"""The load case: what happens on a named boundary (#85, second half).

:mod:`fenixspoon.geometry` and :mod:`fenixspoon.boundaries` answer **where** — the first
half of #85, merged as protocol 1.8. This answers **what**, and the split is the whole
design: the same cantilever is loaded three ways and remains the same cantilever, so a
change of load must not mint a new geometry revision.

## Why a workspace object rather than a block in the design

An engineer reuses one set of restraints and loads across a family of shapes, and that
reuse is the argument. As an object it gets everything the workspace already provides —
revisions, pinning, `object.patch`, and a place in a job's `inputs` so a result records
which load case produced it. As a block inside a design it would be copied per design and
the copies would drift.

## Values are an open map of scalars

Exactly like ``Region2D.material``, and for the same reason: a typed enum of condition
kinds would put physics into the protocol, so every new physics would become a protocol
change — the coupling this codebase has consistently refused.

The one improvement over materials is that the openness is **checked at the edge rather
than nowhere**. A material key typo is silently ignored today; a boundary-condition typo is
refused here, against the keys the capability declared in
:attr:`~fenixspoon.solvers.base.Solver.conditions`. That check is not politeness — a
mistyped traction produces an unloaded structure that solves happily and reports zero
stress, which is a wrong answer wearing a right answer's clothes.

## What is checked, and where

Both refusals happen at **submit**, before a job exists, because both are the caller's
mistake and neither is discoverable afterwards:

- a boundary the geometry does not declare (:class:`~fenixspoon.core.errors.UnknownBoundary`)
- a key the capability does not read (:class:`~fenixspoon.core.errors.UnknownConditionKey`)

They are checked together in :func:`check_conditions`, which is called from
`FenixSpoonCore.submit` — the single door every transport goes through, so an inline load
case over JSON-RPC and a design-referenced one over HTTP are refused identically.
"""

from pydantic import BaseModel, Field

from ..geometry import Geometry
from ..solvers.base import Solver
from . import errors

#: A load case, resolved: boundary name to the scalars in force there. The shape an adapter
#: receives on :attr:`~fenixspoon.solvers.base.SolverContext.conditions`, and the shape that
#: goes into the cache key — plain data, so the digest is over what was *meant* rather than
#: over which objects happened to spell it.
Conditions = dict[str, dict[str, float]]


class LoadCaseBody(BaseModel):
    """A `load_case` workspace object: what happens on each named boundary.

    Named boundaries are the geometry's (protocol 1.8), so one load case applies to any
    shape that declares the same names — which is the reuse this object type exists for.
    Nothing here validates the names, because a load case is authored without a geometry in
    hand and the pairing only becomes real at submit; see :func:`check_conditions`.
    """

    conditions: Conditions = Field(
        description=(
            "Boundary name to the condition keys in force there, e.g. "
            "`{\"root\": {\"fixed\": 1}, \"tip\": {\"traction_y\": -1.0e6}}`. Keys are open "
            "and per-capability — read `capability.describe(sections=[\"conditions\"])` for "
            "the ones a given capability accepts, and their units."
        )
    )


def merge_load_cases(cases: list[tuple[str, LoadCaseBody]]) -> Conditions:
    """One design's load cases as a single mapping, refusing a disagreement.

    Merged per *key* rather than per boundary, so a design can restrain `root` in one load
    case and load `tip` in another — the reuse that motivated a separate object type in the
    first place. Two cases setting the same key on the same boundary is refused rather than
    resolved by list order: that is two intents in one design, and order is not an intent.

    ``cases`` carries each body's pinned reference alongside it so the refusal can name the
    two objects that disagree, which is the difference between a message a caller can act on
    and one that sends them reading every load case they own.
    """
    merged: Conditions = {}
    source: dict[tuple[str, str], str] = {}
    for ref, body in cases:
        for boundary, values in body.conditions.items():
            for key, value in values.items():
                previous = source.get((boundary, key))
                if previous is not None:
                    raise errors.ConflictingConditions(boundary, key, [previous, ref])
                source[(boundary, key)] = ref
                merged.setdefault(boundary, {})[key] = float(value)
    return merged


def check_conditions(
    solver_cls: type[Solver], geometry: Geometry, conditions: Conditions
) -> None:
    """Refuse a load case this geometry and this capability cannot honour.

    Boundaries first, then keys, because a caller that named the wrong boundary has usually
    also written the wrong keys for it and should hear about the cause rather than the
    symptom. Both messages name what is on offer — the geometry's declared boundaries, the
    capability's declared keys — so the fix is visible from the error alone.
    """
    if not conditions:
        return

    declared = [entry.name for entry in getattr(geometry, "boundaries", [])]
    for boundary in conditions:
        if boundary not in declared:
            raise errors.UnknownBoundary(boundary, declared)

    accepted = [spec.name for spec in solver_cls.conditions]
    for boundary, values in conditions.items():
        for key in values:
            if key not in accepted:
                raise errors.UnknownConditionKey(solver_cls.name, boundary, key, accepted)
