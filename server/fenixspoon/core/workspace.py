"""The workspace: typed, versioned, referenceable objects (roadmap M2.5, issue #44).

Every iteration today resends the whole geometry. A caller that moves one control point of an
airfoil and re-solves sends the entire outline again, and again, and again — which is fine over
a LAN to a browser and ruinous for a caller whose context window is the scarce resource. This
module is the other half of the fix: objects live in the workspace under stable ids, and an
iteration names them.

Seven object types, and they are deliberately not equal:

| Type | Validated against | Why |
|---|---|---|
| `geometry` | the `Geometry` union | a real schema exists and is already enforced at submit |
| `material` | name + property map | mirrors what `regions2d` regions already carry |
| `design` | :class:`DesignBody` | the load-bearing one — what `job.submit` resolves |
| `boundary_condition` | *nothing* | see below |
| `load_case` | :class:`~fenixspoon.core.conditions.LoadCaseBody` | #85 defined it |
| `study` | `MeshConvergenceBody` or `SweepBody`, on `kind` | #48 defined it, #21 unioned it |
| `optimization` | :class:`~fenixspoon.core.optimize.OptimizationBody` | #22 defined it |

**`boundary_condition` is stored as opaque JSON on purpose.** Issue #44 asked for that to be
said out loud rather than papered over with an invented schema, so: no shipped solver has a
boundary condition that is separable from its params in a way this type would capture.
`mock.heat2d` expresses convective cooling as `h` and `t_ambient`;
`dolfinx.magnetostatics2d` puts A = 0 on the outer boundary and offers no choice about it. A
generic schema written today would be a guess generalising from zero examples. The type
exists so that ids and references are consistent when a capability finally needs them; until
then the body is whatever the caller puts there and the workspace stores it faithfully.

`study` and `load_case` *were* the same, and each left the thin list the moment a concrete
use case arrived to generalise from — #48 for the first, #85 and the elasticity pair for the
second. That is the rule this table is keeping: a body gets a schema when something needs to
read it, not when someone can imagine reading it. Note that `load_case` is the one that
turned out to carry the physics; `boundary_condition` is now the type with no user, and the
honest reading of #85 is that a load case is what a boundary condition was reaching for.

## Versioning and patching

Objects are never mutated. `object.patch` reads the head, applies the patch, and writes the next
revision; the old one stays readable forever. A reference can pin one — `geometry:g-1@3` — which
is what lets a result name exactly the inputs it was computed from.

The patch format is **JSON Patch (RFC 6902)**, and the deciding case is the one issue #44 named:
moving one control point of a polygon.

    [{"op": "replace", "path": "/obstacle/points/2", "value": [0.35, 0.12]}]

JSON Merge Patch cannot express that. Merge patch replaces arrays wholesale, so the same edit
means resending every point — which is precisely the cost this milestone exists to remove, and
geometry is *mostly* arrays of points. A domain-specific patch (`move_point`, `set_param`) would
be friendlier to generate, but it is a vocabulary that has to grow a verb for every edit anyone
ever wants, and RFC 6902 already covers all of them with implementations in every language a
client might be written in.

## Lifetime

**Objects are not swept by `FENIXSPOON_JOB_TTL`.** They are kept until deleted, and there is no
object TTL. Issue #44 flagged the seven-day job sweep as wrong for a design an agent has been
iterating on for a week, and the asymmetry is the answer rather than a longer number: a job is a
computation, and losing it costs a re-run; an object is something a person authored, and losing
it is data loss.

That inverts the dangling-reference question. A result never outlives its inputs — the inputs
outlive it. A design can therefore name a job id the retention sweep has already removed, and
resolving it yields the same "not found" any purged job gives. This is the better direction: the
workspace keeps enough to *recompute*, which a swept result never could.
"""

import json
from datetime import UTC, datetime
from typing import Any

import jsonpatch
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from ..geometry import Geometry
from ..objects import OBJECT_TYPES, ObjectFileStore, ObjectRecord, parse_ref
from . import errors
from .conditions import Conditions, LoadCaseBody, merge_load_cases
from .optimize import OptimizationBody
from .studies import STUDY_BODY

#: Built once. The union is fixed at import time, and constructing a `TypeAdapter` per
#: validation shows up on a workspace listing that validates every head it returns.
_GEOMETRY = TypeAdapter(Geometry)


class MaterialBody(BaseModel):
    """A named set of material properties.

    Mirrors what a `regions2d` region already carries inline, so a material extracted into
    the workspace can be dropped straight back into a region. Property keys are open —
    `mu_r`, `k`, `current_density` are what the shipped solvers read, and a solver that
    wants `rho` should not need this model changed.
    """

    name: str = Field(description="Human label, e.g. `aluminium 6061`.")
    properties: dict[str, float] = Field(
        description="Property name to value, in the units the solver documents."
    )


class DesignBody(BaseModel):
    """What a solve is run *on*: a capability, a geometry, and the parameters.

    The one object type `job.submit` resolves, and the reason the others exist. Materials,
    boundary conditions and load cases are referenced rather than embedded so that editing a
    material does not require rewriting every design that uses it.
    """

    solver: str = Field(description="Capability name, as `capability.list` reports it.")
    geometry: str = Field(
        description=(
            "Reference to a `geometry` object, optionally pinned: `geometry:g-1` follows the "
            "head, `geometry:g-1@3` is frozen at that revision."
        )
    )
    params: dict[str, Any] = Field(
        default={}, description="Solver parameters, validated against the capability's schema."
    )
    materials: list[str] = Field(
        default=[], description="References to `material` objects this design uses."
    )
    boundary_conditions: list[str] = Field(
        default=[], description="References to `boundary_condition` objects. Thin, see above."
    )
    load_cases: list[str] = Field(
        default=[], description="References to `load_case` objects. Thin, see above."
    )


class ObjectSummary(BaseModel):
    """One line of `workspace.list` — no body, because a geometry body is the payload
    progressive iteration exists to stop moving."""

    ref: str = Field(description="Stable identifier, e.g. `geometry:g-1`.")
    type: str = Field(description="One of the workspace object types.")
    revision: int = Field(description="Head revision number; revisions start at 1.")
    label: str | None = Field(default=None, description="Whatever the caller called it.")
    created_at: str = Field(description="When this revision was written (RFC 3339, UTC).")


class ObjectView(BaseModel):
    """One object revision, body included. What `object.create/get/patch` return."""

    ref: str = Field(description="Stable identifier without a revision.")
    pinned: str = Field(
        description="This exact revision, `geometry:g-1@3` — quote it to freeze an input."
    )
    type: str = Field(description="Object type.")
    revision: int = Field(description="Which revision this is.")
    label: str | None = Field(default=None, description="Caller-supplied label.")
    created_at: str = Field(description="When it was written (RFC 3339, UTC).")
    body: dict[str, Any] = Field(description="The object itself, shaped by its type.")

    @classmethod
    def of(cls, record: ObjectRecord) -> "ObjectView":
        return cls(
            ref=record.ref,
            pinned=record.pinned,
            type=record.type,
            revision=record.revision,
            label=record.label,
            created_at=record.created_at.isoformat(),
            body=record.body,
        )


class WorkspaceInfo(BaseModel):
    """What `workspace.open` returns: where it is, and what is in it."""

    path: str = Field(description="Absolute path holding the object files.")
    objects: dict[str, int] = Field(
        description="Object count per type, for the types that have any."
    )
    types: list[str] = Field(description="Every type this workspace can hold.")


#: Types whose body the workspace validates. The rest are stored as given — see the module
#: docstring for why inventing a schema for them would be worse than admitting there is none.
#:
#: Adapters rather than model classes, because two of these are unions now: a geometry always
#: was, and a study body became one when #21 added the second kind. The sentinel this map used
#: to carry for the geometry special case went with it — one calling convention beats a map
#: whose values mean two different things.
VALIDATED: dict[str, TypeAdapter[Any]] = {
    "geometry": _GEOMETRY,
    "material": TypeAdapter(MaterialBody),
    "design": TypeAdapter(DesignBody),
    # #48 gave `study` a body and #85 gave `load_case` one, so both left the thin list.
    # `boundary_condition` is still thin, and still for the reason above rather than for
    # want of attention.
    "study": STUDY_BODY,
    # #22 gave `optimization` a body on the day it was added; unlike `study` and
    # `load_case` it never spent time on the thin list, because it arrived with the
    # operation that reads it.
    "optimization": TypeAdapter(OptimizationBody),
    "load_case": TypeAdapter(LoadCaseBody),
}


class Workspace:
    """Typed object operations over :class:`~fenixspoon.objects.ObjectFileStore`.

    The store knows about files and revisions; this knows what an object *means* — which
    bodies are validated, how a patch is applied, and how a design resolves to the geometry
    and params a job needs.
    """

    def __init__(self, store: ObjectFileStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ workspace

    def info(self) -> WorkspaceInfo:
        counts: dict[str, int] = {}
        for record in self.store.list_heads():
            counts[record.type] = counts.get(record.type, 0) + 1
        return WorkspaceInfo(
            path=str(self.store.root), objects=counts, types=list(OBJECT_TYPES)
        )

    def list_objects(
        self, owner: str, object_type: str | None = None
    ) -> list[ObjectSummary]:
        if object_type is not None and object_type not in OBJECT_TYPES:
            raise errors.UnknownObjectType(object_type, list(OBJECT_TYPES))
        return [
            ObjectSummary(
                ref=record.ref,
                type=record.type,
                revision=record.revision,
                label=record.label,
                created_at=record.created_at.isoformat(),
            )
            for record in self.store.list_heads(owner=owner, object_type=object_type)
        ]

    # --------------------------------------------------------------------- objects

    def create(
        self, object_type: str, body: dict[str, Any], owner: str, label: str | None = None
    ) -> ObjectView:
        if object_type not in OBJECT_TYPES:
            raise errors.UnknownObjectType(object_type, list(OBJECT_TYPES))
        self._validate(object_type, body)
        return ObjectView.of(self.store.create(object_type, owner, body, label))

    def get(self, ref: str, owner: str) -> ObjectView:
        return ObjectView.of(self._record(ref, owner))

    def get_typed(self, ref: str, owner: str, expected: str) -> ObjectView:
        """One object, refusing a reference that resolves to the wrong kind.

        `get` deliberately does not type-check — `object.get` is how a caller inspects
        whatever it has a reference to. A caller that *knows* what it wants (a study, a
        design) needs the check, and getting `WrongObjectType` beats reading a body that
        happens to parse.
        """
        return ObjectView.of(self._record(ref, owner, expected=expected))

    def revisions(self, ref: str, owner: str) -> list[int]:
        """Every revision of an object. Reading one first, so an unknown ref is an error
        rather than an empty list that reads like "this object has no history"."""
        self._record(ref, owner)
        return self.store.revisions(ref)

    def patch(
        self, ref: str, patch: list[dict[str, Any]], owner: str, label: str | None = None
    ) -> ObjectView:
        """Apply an RFC 6902 patch to the head and write the result as the next revision.

        A pinned ``ref`` is refused rather than quietly branching. `geometry:g-1@2` names a
        revision that already exists and is immutable, so "patch that" has no honest meaning:
        either the caller wanted the head and should say so, or it wants a branch, and this
        design has no branches.
        """
        base, _, revision = parse_ref(ref)
        if revision is not None:
            raise errors.CannotPatchRevision(ref)
        current = self._record(base, owner)

        try:
            patched = jsonpatch.apply_patch(current.body, patch)
        except jsonpatch.JsonPatchException as exc:
            # Covers a malformed operation and one that does not apply — "no such path" is
            # the common case, and it is the caller's mistake either way.
            raise errors.InvalidPatch(str(exc)) from exc
        except (TypeError, KeyError) as exc:
            raise errors.InvalidPatch(f"malformed JSON Patch: {exc}") from exc

        if patched == current.body:
            # Refused rather than written. A revision that changed nothing still moves the
            # head, so every pinned reference taken afterwards points at a different number
            # for the same content — churn that makes provenance (#47) harder to read for
            # no gain. A caller that meant to change something needs to know it did not.
            raise errors.PatchChangedNothing(base)

        self._validate(current.type, patched)
        record = ObjectRecord(
            ref=current.ref,
            type=current.type,
            revision=current.revision + 1,
            owner=owner,
            body=patched,
            label=label if label is not None else current.label,
            created_at=datetime.now(UTC),
        )
        return ObjectView.of(self.store.revise(record))

    # --------------------------------------------------------------------- designs

    def resolve_design(self, ref: str, owner: str) -> tuple[Geometry, "ResolvedDesign"]:
        """Turn a design reference into the geometry and params a solve needs.

        Returns the pinned references alongside, because *which revision* was used is the
        part a result has to record. A design naming `geometry:g-1` unpinned resolves to the
        head at submit time and freezes it here — otherwise two solves of "the same design"
        could mean two different shapes with nothing saying so.
        """
        design = self._record(ref, owner, expected="design")
        body = DesignBody.model_validate(design.body)

        geometry_record = self._record(body.geometry, owner, expected="geometry")
        try:
            geometry = _GEOMETRY.validate_python(geometry_record.body)
        except ValidationError as exc:  # pragma: no cover - create() already validated it
            raise errors.InvalidObject("geometry", json.loads(exc.json())) from exc

        # Every reference the design names is resolved and type-checked, not just the two
        # that feed the solver. A design listing a geometry under `materials` would
        # otherwise resolve happily and write provenance that is wrong in a way nothing
        # would ever surface — and the thin types are the ones most likely to be miswired,
        # because nothing validates their bodies either.
        def resolve(refs: list[str], expected: str) -> list[ObjectRecord]:
            return [self._record(item, owner, expected=expected) for item in refs]

        # The load cases are the one set of references whose *bodies* are read here rather
        # than only pinned: they say what happens on each named boundary, and that has to
        # reach the solver. Merged now, at the moment the revisions are frozen, so the
        # conditions a job runs with and the revisions its provenance names are one reading
        # of the workspace rather than two that could differ.
        cases = resolve(body.load_cases, "load_case")
        conditions = merge_load_cases(
            [(record.pinned, LoadCaseBody.model_validate(record.body)) for record in cases]
        )

        return geometry, ResolvedDesign(
            solver=body.solver,
            params=dict(body.params),
            conditions=conditions,
            design=design.pinned,
            geometry=geometry_record.pinned,
            materials=[record.pinned for record in resolve(body.materials, "material")],
            boundary_conditions=[
                record.pinned
                for record in resolve(body.boundary_conditions, "boundary_condition")
            ],
            load_cases=[record.pinned for record in cases],
        )

    # ---------------------------------------------------------------------- internals

    def _record(self, ref: str, owner: str, expected: str | None = None) -> ObjectRecord:
        try:
            parse_ref(ref)
        except ValueError as exc:
            raise errors.MalformedReference(ref, str(exc)) from exc
        record = self.store.get(ref, owner=owner)
        if record is None:
            # Missing and somebody else's are the same answer, for the same reason a job is:
            # confirming that an id exists is itself a disclosure.
            raise errors.ObjectNotFound(ref)
        if expected is not None and record.type != expected:
            raise errors.WrongObjectType(record.ref, expected, record.type)
        return record

    @staticmethod
    def _validate(object_type: str, body: dict[str, Any]) -> None:
        adapter = VALIDATED.get(object_type)
        if adapter is None:
            return  # thin type: stored as given, on purpose
        try:
            adapter.validate_python(body)
        except ValidationError as exc:
            raise errors.InvalidObject(object_type, json.loads(exc.json())) from exc


class ResolvedDesign(BaseModel):
    """What a design resolved to, and the exact revisions it resolved through.

    The `design → job → result` relation issue #44 asks to be queryable, recorded at the one
    moment it is knowable. #47 extends this with the environment and a content hash; the
    references are the part that belongs here.
    """

    solver: str = Field(description="Capability the job will run.")
    params: dict[str, Any] = Field(description="Parameters from the design, before validation.")
    conditions: Conditions = Field(
        default={},
        description=(
            "The design's load cases merged into one mapping of boundary name to the "
            "scalars in force there (#85). Empty when the design names none, which is "
            "every design written before the elasticity pair. Not a reference like the "
            "fields below — it is the *content* those references resolved to, because it "
            "is what the solve actually runs with."
        ),
    )
    design: str = Field(description="Design revision used, pinned: `design:d-1@2`.")
    geometry: str = Field(description="Geometry revision used, pinned.")
    materials: list[str] = Field(default=[], description="Material revisions used, pinned.")
    boundary_conditions: list[str] = Field(
        default=[], description="Boundary-condition revisions the design named, pinned."
    )
    load_cases: list[str] = Field(
        default=[], description="Load-case revisions the design named, pinned."
    )
