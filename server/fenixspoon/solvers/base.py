"""The solver adapter protocol.

A solver is a class with a ``name``, a params model, and a ``solve`` method. It knows nothing
about HTTP or jobs; it receives a validated geometry + params and a :class:`SolverContext`
providing progress reporting, cooperative cancellation, and artifact registration. See
``mock_laplace.py`` for the reference implementation.
"""

import logging
import mimetypes
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from .. import fields
from ..frames import MAX_FRAMES
from ..geometry import Geometry
from ..modes import MAX_MODES
from ..series import Series1DData, check_series

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Raised inside a solver (via ``SolverContext.check_cancelled``) to abort a run."""


class ProgressEvent(BaseModel):
    """One tick of solver progress, streamed to WebSocket subscribers."""

    type: str = Field(
        default="progress",
        description=(
            "Discriminates this from a `status` event on the same stream. Fixed by default "
            "rather than by annotation, which is why it needs saying: a subscriber branches "
            "on it to tell a tick of work from a change of state."
        ),
    )
    iteration: int = Field(description="How far the solve has got, in solver-defined units.")
    total: int | None = Field(
        default=None, description="Expected final `iteration`, when the solver can predict it."
    )
    residual: float | None = Field(
        default=None, description="Convergence measure for iterative solvers; null otherwise."
    )
    message: str | None = Field(
        default=None, description="Human-readable stage, e.g. `meshing with Gmsh`."
    )


class SolverResult(BaseModel):
    """Result envelope. ``kind`` selects the schema of ``data`` (see docs/04-wire-protocol.md)."""

    kind: str = Field(
        description='Result schema selector: `"grid2d"`, `"mesh2d"` or `"series1d"`.'
    )
    data: dict[str, Any] = Field(description="The field data, shaped according to `kind`.")
    stats: dict[str, float] = {}
    """What the solve actually cost: ``cells``, ``dofs``, and whatever else the adapter
    knows. The job manager adds ``seconds``. Reported so a user can see why a job was
    slow, and so an operator can pick a sensible cap.

    **Cost, not answer.** The engineering quantities go in ``metrics``; keeping them apart
    is what lets an operator size a machine and an engineer make a decision from the same
    result without either having to work out which keys are which."""

    metrics: dict[str, float] = {}
    """The engineering answer: the scalars this capability declared in :attr:`Solver.metrics`
    (roadmap M2.5, issue #46).

    An adapter only fills in what the generic path cannot. Any declared metric that is a
    stated reduction of a result field — ``t_max`` is the ``max`` of ``T`` — is computed
    after the solve returns, from the declaration, so no adapter writes that code twice.
    What an adapter *must* supply is the derived ones: ``t_rise`` needs the ``t_ambient``
    parameter, ``cp_min`` needs ``u_inf``, and neither is a reduction of anything."""

    converged: bool | None = None
    """Whether the solve reached its tolerance. ``None`` where the question does not apply —
    a direct LU factorisation does not iterate toward anything.

    This had nowhere to live before: ``stats`` is ``dict[str, float]``, so a flag could only
    be smuggled in as 0.0/1.0 and hoped about."""

    residual: float | None = None
    """Final residual for an iterative solve; ``None`` for a direct one."""

    warnings: list[str] = []
    """Things the caller should know that did not fail the job — a solve stopped at its
    iteration cap, a mesh coarser than requested. Previously these could only be said in a
    progress event, which is to say only to a client that happened to be watching."""

    series: list[Series1DData] = []
    """Curves this solve produced alongside its field (protocol 1.4, issue #69).

    A surface distribution, a section, a convergence history: an ordered list of (x, y) pairs
    with names and units. Separate from ``data`` because one solve legitimately answers both
    questions at once — the airfoil adapters return the flow field *and* the surface `C_p`,
    and making a caller submit twice for the second one would be inventing a round trip."""

    @model_validator(mode="after")
    def _check_series(self) -> "SolverResult":
        # Enforced at construction, inside the adapter, rather than only on the wire: the
        # collection-level ceiling is the thing standing between "a curve" and "the field
        # arrays under another key", and an adapter should learn it wrote too much where it
        # wrote it. A `series1d` adapter puts its curves in `data`, which is an untyped dict
        # here, so that shape is bounded by the envelope rather than by this model.
        check_series(self.series)
        return self


class MetricSpec(BaseModel):
    """A scalar engineering quantity this capability reports (roadmap M2.5, issue #43).

    Metrics are the *answer* to an engineering question — peak flux density, temperature
    rise, circulation — as distinct from ``stats``, which is what the solve *cost*. Keeping
    them apart is the point: an operator reads ``stats`` to size a machine, an engineer
    reads metrics to make a decision, and a caller that has to guess which is which cannot
    do either.

    **Declared here, computed in #46.** This issue makes metrics discoverable, so a caller
    learns what a solve will report *before* running one; the level that returns their
    values is separate work. What keeps the declaration from being aspirational is
    ``field``: where a metric is a stated reduction of a declared result field,
    ``test_declared_metrics_reduce_a_field_the_solver_emits`` runs a real solve and fails if
    that field is not in the payload.
    """

    name: str = Field(description="Identifier a caller asks for, e.g. `speed_max`.")
    unit: str = Field(
        description='Unit as a display string — `"T"`, `"K"`, `"m/s"`; `"1"` if dimensionless.'
    )
    description: str = Field(description="What the number means, in one line.")
    field: str | None = Field(
        default=None,
        description=(
            "Result field this metric reduces, when it is a reduction of one. Null when the "
            "solver computes it some other way (an integral over a region, a ratio)."
        ),
    )
    reduction: Literal["max", "min", "mean", "integral"] | None = Field(
        default=None,
        description="How `field` becomes a scalar. Null exactly when `field` is null.",
    )
    # Added by #68. Lift, moment and centre of pressure are integrals over the *body surface*,
    # not over the domain, so before this they could only be declared with `field` null —
    # honest, but indistinguishable from an arbitrary derived ratio and outside the reach of
    # `test_declared_metrics_reduce_a_field_the_solver_emits`. Boundary integrals recur rather
    # than being a one-off: lift and moment here, gap force in magnetostatics, reaction forces
    # in elasticity, wall heat flux in conduction.
    boundary: str | None = Field(
        default=None,
        description=(
            "Boundary this metric integrates over, when it is a boundary integral rather "
            "than a reduction of a field — `body` for the surface of a `domain2d` obstacle. "
            "Mutually exclusive with `field`: a quantity is one or the other."
        ),
    )
    # Added by #86, and found by #84 rather than reasoned about in advance. A transient's
    # payload is *one instant*, so `t_final_max` is declarable as a reduction and the peak
    # over the whole run is not. Without this, the two are distinguishable only by their
    # names — a convention two adapter authors would spell differently and both look right.
    over: Literal["payload", "run"] = Field(
        default="payload",
        description=(
            "What the number is taken over. `payload` — the result that came back, which "
            "for a steady solve is the whole answer and for a transient is the final "
            "instant. `run` — the whole solve: a peak over its history, a time to reach a "
            "level, or a property of the configuration. A `run` metric can only be supplied "
            "by the adapter, because it is not in the payload to be reduced."
        ),
    )

    @model_validator(mode="after")
    def _check_recipe(self) -> "MetricSpec":
        """A metric names one route to its value, never two and never half of one.

        Enforced in the constructor rather than by a test over ``registered_solvers()``, for
        the same reason :meth:`SolverResult._check_series` is: the declaration system exists
        for adapters this repository does not contain, and a test that iterates the installed
        set binds four of them and nothing else. It also guards shared machinery —
        :func:`fill_declared_metrics` branches on ``field``/``reduction`` and knows nothing
        about ``boundary``, so a spec setting both would quietly compute the reduction and
        shadow the boundary integral, which is exactly the ambiguity naming the boundary was
        supposed to remove.
        """
        if (self.field is None) != (self.reduction is None):
            raise ValueError(
                f"metric {self.name!r} declares field={self.field!r} with "
                f"reduction={self.reduction!r}: a field with no reduction cannot be evaluated, "
                f"and a reduction with no field has nothing to reduce"
            )
        if self.field is not None and self.boundary is not None:
            raise ValueError(
                f"metric {self.name!r} declares both a field ({self.field!r}) and a boundary "
                f"({self.boundary!r}); a quantity is a reduction of a field or an integral "
                f"over a boundary, not both"
            )
        # `field` means "the runtime reduces this from the payload", and a `run` metric is by
        # definition not in the payload. Allowing both would produce the exact failure #86
        # exists to prevent: a peak over a transient's history quietly reported as the value
        # at the final instant, which agrees on every monotonic case anyone would test.
        if self.over == "run" and self.field is not None:
            raise ValueError(
                f"metric {self.name!r} is declared over the run but reduces the field "
                f"{self.field!r}; the payload holds one instant, so the runtime cannot "
                f"compute it — drop the field and have the adapter supply the value"
            )
        return self


class ConditionSpec(BaseModel):
    """A boundary-condition key this capability reads from a load case (issue #85).

    A load case is an open map of scalars, exactly like ``Region2D.material``, because a
    typed enum of condition kinds would put physics into the protocol and every new physics
    would become a protocol change. That openness has one cost #85 named explicitly: **a
    material key typo is silently ignored today, and a boundary-condition typo must not
    be.** So an adapter declares the keys it reads, the way it already declares its metrics
    and its assumptions, and a key nothing declares is refused at submit.

    That makes the declaration load-bearing rather than documentation: it is the *only*
    thing standing between `traction_y: -1e6` and `tracton_y: -1e6`, and the second one
    would otherwise produce an unloaded structure that solves perfectly happily and reports
    zero stress.

    ``kind`` is the discretization classification — imposed value, imposed flux, or a mix —
    rather than a physics vocabulary. It is what lets a form builder group restraints apart
    from loads without knowing what elasticity is.
    """

    name: str = Field(description="Key as it appears in a load case, e.g. `traction_x`.")
    unit: str = Field(description='Unit as a display string — `"Pa"`, `"K"`; `"1"` for a flag.')
    description: str = Field(description="What setting it does, in one line.")
    kind: Literal["dirichlet", "neumann", "robin"] = Field(
        description=(
            "How it enters the problem: `dirichlet` imposes the unknown itself, `neumann` "
            "imposes its flux, `robin` relates the two. A classification of the condition, "
            "not of the physics — which is why it can be a closed set here when the keys "
            "themselves deliberately are not."
        )
    )


class Assumption(BaseModel):
    """A modelling assumption in force, and where it stops applying (issue #70).

    Every other section of a capability description says what the capability **does**. This
    one says what its model **assumes** — which is the information a caller needs most before
    trusting a number, and which until now existed only as prose inside `description`, if at
    all. Prose cannot be filtered, cannot be checked, and cannot be shown next to the number
    it qualifies.

    Two fields carry most of the weight.

    ``excludes`` is the one that pays for itself immediately: it lets a caller ask *"can this
    capability tell me about drag?"* and get a definite **no** rather than a plausible zero.
    That is the same argument #43 made for :class:`CapabilityFeatures`, where all three flags
    default to false so that "can I sweep this?" has an answer a caller can act on — this is
    the physics equivalent.

    ``quantity`` / ``limit`` / ``comparator`` make an assumption *machine-checkable* where it
    has a numeric edge and the quantity is computable from the params: Mach against 0.3, `B`
    against a saturation threshold. That is what makes this more than documentation. The
    per-run half — "this run has M = 0.42, above the 0.3 limit" — is a diagnostic and belongs
    with the warnings in #46; declaring the limit here is what lets such a warning name the
    assumption it violated instead of restating it.
    """

    name: str = Field(description="Short identifier a caller can filter on, e.g. `inviscid`.")
    statement: str = Field(
        description="What is assumed, and where it stops holding, in one or two sentences."
    )
    quantity: str | None = Field(
        default=None,
        description=(
            "Physical quantity the limit applies to, when the assumption has a numeric edge "
            "— `mach`, `reynolds`, `b_max`. Null for a structural assumption like `2-D`."
        ),
    )
    limit: float | None = Field(
        default=None,
        description="Value of `quantity` at which the assumption fails. Null when there is no "
        "single number, which is most of them.",
    )
    comparator: Literal["<", "<=", ">", ">="] | None = Field(
        default=None,
        description=(
            "Which side of `limit` is valid: `<` means the assumption holds while `quantity` "
            "is below it. Null exactly when `limit` is null."
        ),
    )
    excludes: list[str] = Field(
        default=[],
        description=(
            "Quantities this assumption puts out of reach entirely — `drag` for an inviscid "
            "model. A caller asking for one of these should be told no, not given a zero."
        ),
    )
    when: str | None = Field(
        default=None,
        description=(
            "Name of the boolean param that puts this assumption in force. Null — the usual "
            "case — means it always applies. Read `when_value` for which setting arms it."
        ),
    )
    when_value: bool = Field(
        default=True,
        description=(
            "The value of `when` that puts this assumption in force. `false` is how an "
            "assumption in force when a feature is *disabled* is declared. Ignored when "
            "`when` is null."
        ),
    )
    """Which setting of ``when`` arms this assumption.

    A second field rather than a sigil inside ``when``. The first version of this model spelled
    it ``when="!kutta"``, which read fine and was the wrong depth twice over: this class had
    already decided three fields down that machine-readable conditions get *structure*
    (``quantity``/``limit``/``comparator``), and :class:`ArtifactSpec` has carried a ``when``
    of its own since #43 whose grammar is a bare parameter name. Two sibling models exposing
    one field name with two grammars is a consumer reading either and mis-parsing the other.
    With the value in a field of its own, ``when`` means the same thing in both — and
    ``ArtifactSpec``'s implicit "must be true" is just this field's default."""

    def applies(self, params: dict[str, Any] | None = None) -> bool:
        """Whether this assumption is in force for a given parameter set.

        Unconditional assumptions are always in force, which is what makes the *static*
        declaration useful on its own: a caller reading `capability.describe` with no params
        in hand still learns that the model is 2-D and inviscid. ``when`` exists for the one
        genuinely conditional case — an assumption a parameter switches off, like the absent
        circulation of a potential-flow solve run without the Kutta condition — and a caller
        that has not decided its params yet should read such an entry as "possible", which is
        why it is declared rather than hidden.
        """
        if self.when is None:
            return True
        return bool((params or {}).get(self.when)) is self.when_value


class ArtifactSpec(BaseModel):
    """A file this capability may write alongside the inline result (issue #43).

    Declaring them lets a caller decide whether to ask for the VTK *before* submitting,
    rather than discovering after the fact that the only way to get the field out was a
    parameter it left at the default.
    """

    name: str = Field(
        description=(
            "Filename the solver registers, e.g. `solution.vtk`. May contain `{index}` for a "
            "file written once per stored instant — `frame_{index}.vtk` — which is how a "
            "transient declares an output whose count depends on its parameters (#86)."
        )
    )
    content_type: str = Field(description="MIME type it is served with.")
    description: str = Field(description="What the file contains, and what opens it.")
    when: str | None = Field(
        default=None,
        description=(
            "Name of the param whose **truthiness** gates this file; null if it is always "
            "written. Usually a boolean (`write_vtk`), but not necessarily — the frame "
            "artifacts are gated on `save_every`, an integer where 0 means none."
        ),
    )

    def matches(self, written: str) -> bool:
        """True if `written` is a file this spec declares.

        A literal comparison for the ordinary case, and a digit match where the name carries
        `{index}`. Written as a method rather than left to each caller because the test that
        keeps the declaration honest — written files are a subset of declared ones — is the
        one place this must not be re-implemented approximately.
        """
        if "{index}" not in self.name:
            return written == self.name
        head, _, tail = self.name.partition("{index}")
        middle = written[len(head) : len(written) - len(tail)] if written else ""
        return (
            written.startswith(head)
            and written.endswith(tail)
            and len(written) > len(head) + len(tail)
            and middle.isdigit()
        )


class CapabilityFeatures(BaseModel):
    """What a capability supports beyond a single solve (issue #43).

    All three default to false, and false is the honest answer for every adapter shipped
    today. That is worth saying explicitly rather than by omission: a caller asking
    "can I sweep this?" gets a definite no it can act on, and the day the study
    abstraction (#48) lands, exactly one flag moves.
    """

    sweep: bool = Field(
        default=False, description="Can be driven by a parameter study without a bespoke driver."
    )
    gradient: bool = Field(
        default=False, description="Reports derivatives of a metric with respect to its params."
    )
    mpi: bool = Field(
        default=False,
        description=(
            "Produces a correct result when the process is launched under `mpirun`. False "
            "for the FEniCSx adapters too: they mesh on `COMM_WORLD` but serialise "
            "partition-local arrays, so a multi-rank run would return one rank's slice."
        ),
    )


class CapabilityExample(BaseModel):
    """A known-good parameter set, so a caller need not invent one (issue #43).

    Params only, deliberately: a geometry large enough to be realistic is exactly the
    payload progressive discovery exists to avoid sending. Geometry kinds are described in
    the `geometries` section, and runnable geometries live in `protocol/fixtures/`.
    """

    title: str = Field(description="Short label, e.g. `fast preview`.")
    description: str = Field(description="When to reach for this parameter set.")
    params: dict[str, Any] = Field(description="Params as submitted, valid against `Params`.")


class SolverInfo(BaseModel):
    """What ``GET /api/v1/solvers`` returns per solver."""

    name: str = Field(description="Identifier to pass as `solver` when submitting a job.")
    title: str = Field(description="Short human-readable name for a picker.")
    description: str = Field(description="What this solver computes, and how.")
    geometry_types: list[str] = Field(
        description="Geometry `type` values this solver accepts, e.g. `[\"domain2d\"]`."
    )
    params_schema: dict[str, Any] = Field(
        description="JSON Schema for this solver's `params`; build a form from it."
    )


class SolverContext:
    """Runtime services handed to :meth:`Solver.solve`.

    ``solve`` runs in a worker thread; every method here is safe to call from it.
    Cancellation is cooperative: long loops should call :meth:`check_cancelled` at a
    sensible cadence (it is just an ``Event.is_set`` check, so per-iteration is fine).
    """

    def __init__(
        self,
        progress_cb: Callable[[ProgressEvent], None],
        cancel_event: threading.Event | None = None,
        artifact_dir: Path | None = None,
        conditions: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._progress_cb = progress_cb
        self._cancel_event = cancel_event or threading.Event()
        self._artifact_dir = artifact_dir
        self._artifacts: list[dict[str, Any]] = []
        self.conditions: dict[str, dict[str, float]] = dict(conditions or {})
        """What the load case says happens on each named boundary (issue #85).

        Keyed by a boundary name the geometry declares, so an adapter turns one into a
        predicate with :func:`fenixspoon.boundaries.predicate` and hands *that* to whatever
        it uses to select entities. Empty — the default, and the only thing every adapter
        shipped before #85 ever saw — means the caller supplied no load case and the
        adapter's own parameters govern.

        Here rather than in ``params`` because a load case is a workspace object of its own
        and a design references it the way it references a geometry: putting it in the
        params model would make it part of each adapter's schema, so two adapters for one
        physics would spell it two ways, and a caller could not reuse one set of restraints
        across a family of shapes without rewriting it per capability.

        Already validated by the time it arrives: every boundary named here is one the
        geometry declares, and every key is one this capability declared in
        :attr:`Solver.conditions`. An adapter reads it without re-checking."""

    def progress(self, event: ProgressEvent) -> None:
        self._progress_cb(event)

    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise JobCancelled()

    def artifact(
        self,
        name: str,
        content_type: str | None = None,
        t: float | None = None,
        mode: int | None = None,
    ) -> Path:
        """Register an output file and return the path the solver should write it to.

        ``name`` must be a bare filename — path separators are rejected so artifacts can
        never escape the job directory.

        ``t`` marks the file as **one instant of a time-dependent solve** (#86). The
        artifacts carrying one become the result's `frames`, in time order. Putting the
        instant on the file rather than in a parallel list is what makes an index that names
        a file the result does not serve unrepresentable rather than merely tested for.

        ``mode`` marks it as **one mode shape of an eigensolve** (#101), and everything the
        paragraph above says applies unchanged — it is the same index over a different
        ordering, which is why it is a second field rather than a reuse of the first. The two
        are mutually exclusive: a file holds an instant or a mode, and one that claimed both
        would be ordered two ways.
        """
        if not name or "/" in name or "\\" in name or name.startswith(".") or ".." in name:
            raise ValueError(f"invalid artifact name: {name!r}")
        if t is not None and mode is not None:
            raise ValueError(
                f"artifact {name!r} was given both an instant and a mode number; a file "
                "holds one or the other"
            )
        if self._artifact_dir is None:
            raise RuntimeError("this context has no artifact directory configured")
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        if content_type is None:
            content_type = (
                mimetypes.guess_type(name)[0]
                or {"vtk": "model/vnd.vtk", "vtu": "model/vnd.vtu"}.get(
                    name.rsplit(".", 1)[-1].lower(), "application/octet-stream"
                )
            )
        entry: dict[str, Any] = {"name": name, "content_type": content_type}
        if t is not None:
            # Enforced at registration rather than at serialisation, because only some
            # transports build a `ResultEnvelope` — the compact levels and the local API do
            # not, and a cap that half the callers can walk past is not a cap. Raised here it
            # also fails the *solve*, with a message naming the parameter to change, instead
            # of producing a result that cannot be represented.
            framed = sum(1 for item in self._artifacts if item.get("t") is not None)
            if framed >= MAX_FRAMES:
                raise ValueError(
                    f"a result may index at most {MAX_FRAMES} frames; save fewer instants "
                    "rather than moving the history into the envelope"
                )
            entry["t"] = float(t)
        if mode is not None:
            # The same cap, at the same place and for the same reason as the frame one above.
            indexed = sum(1 for item in self._artifacts if item.get("mode") is not None)
            if indexed >= MAX_MODES:
                raise ValueError(
                    f"a result may index at most {MAX_MODES} mode shapes; ask for fewer "
                    "modes rather than moving the modal basis into the envelope"
                )
            entry["mode"] = int(mode)
        self._artifacts.append(entry)
        return self._artifact_dir / name

    @property
    def artifacts(self) -> list[dict[str, Any]]:
        """Registered artifacts, enriched with on-disk size where the file exists."""
        out = []
        for entry in self._artifacts:
            path = (self._artifact_dir / entry["name"]) if self._artifact_dir else None
            if path is not None and path.is_file():
                out.append({**entry, "size": path.stat().st_size})
        return out


class Solver(ABC):
    """Base class for solver adapters.

    Subclasses set ``name``, ``title``, ``description`` and ``Params`` (a pydantic model), and
    implement :meth:`solve`. ``solve`` runs in a worker thread — it must be self-contained,
    call ``ctx.progress`` at a sensible cadence (every few hundred ms of work, not every
    iteration), and call ``ctx.check_cancelled`` inside long loops.

    The nine attributes below ``geometry_types`` are the **capability declaration** added
    for progressive discovery (roadmap M2.5, issue #43, extended by #70). Every one of them
    has a default, so an existing adapter keeps working untouched and reports
    ``"unspecified"`` where it has not said. They are plain class attributes in the spirit of
    ``Params`` rather than a registration framework: declaring a metric is one line in a list,
    and forgetting to declare anything costs a caller information, not a crash.
    """

    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    geometry_types: ClassVar[list[str]] = ["domain2d"]

    #: Physics this capability solves, as a coarse tag a caller can filter on:
    #: ``potential-flow``, ``magnetostatics``, ``heat-conduction``. Free-form on purpose —
    #: a closed enum would mean a new physics could not be added without changing this file,
    #: which is the coupling `writing an adapter <../start-write-a-solver.md>`_ avoids.
    physics: ClassVar[str] = "unspecified"

    #: Which implementation family provides it: ``mock`` for a NumPy stand-in that runs
    #: anywhere, ``fenicsx`` for a real FEniCSx solve. The word in the design spec is
    #: *availability*, and it answers the question a caller actually asks — "is the real
    #: thing installed here, or only the development stand-in?"
    availability: ClassVar[str] = "unspecified"

    #: Importable modules this adapter needs. Informational: a solver only reaches the
    #: registry if its imports succeeded, so a listed capability's requirements are
    #: satisfied by construction. Worth reporting anyway, because *versions* differ —
    #: and the versions go into the cache key (#47), so this list is load-bearing there.
    requires: ClassVar[list[str]] = []

    #: **Bump this whenever the numbers change** (roadmap M2.5, issue #47).
    #:
    #: It is the only statement that a cached answer from an older build is no longer the
    #: answer this adapter would give. Nothing can infer it: an adapter can be rewritten from
    #: Jacobi to multigrid, or have a boundary condition corrected, without a single
    #: signature changing — and the cache would go on serving the old field forever.
    #:
    #: A free-form string rather than a number, so a date or a git tag works. It is opaque to
    #: the cache: any change to it is a new key.
    version: ClassVar[str] = "1"

    #: Whether this adapter returns the same answer for the same inputs. **Default False**,
    #: so caching is opt-in (#47).
    #:
    #: Serving a cached result for a solver that does not reproduce is a wrong answer
    #: delivered quickly, which is the worst outcome on offer; a missed hit is merely a
    #: solve. Thread counts, MPI rank decomposition and anything seeded from wall-clock
    #: state all break reproducibility, and none of them is visible from outside the
    #: adapter — so the adapter has to say.
    deterministic: ClassVar[bool] = False

    #: Scalar engineering quantities this capability reports. See :class:`MetricSpec`.
    metrics: ClassVar[list[MetricSpec]] = []

    #: Modelling assumptions in force, and what they put out of reach. See
    #: :class:`Assumption`. Empty is the honest default for a third-party adapter that has
    #: not said — and, unlike the other declarations, an empty list here is worth noticing:
    #: every physical model assumes *something*, so "no assumptions declared" means
    #: undeclared rather than unconditionally valid. Which is why
    #: `test_every_shipped_adapter_declares_its_assumptions` refuses it for the adapters in
    #: this repository.
    assumptions: ClassVar[list[Assumption]] = []

    #: Boundary-condition keys it reads from a load case. See :class:`ConditionSpec`.
    #:
    #: Empty means this capability takes no load case at all, and that is enforced rather
    #: than merely reported: submitting one to a capability that declares nothing is
    #: refused. Silently ignoring it would answer a different problem than the caller
    #: described — the failure #85 exists to make impossible.
    conditions: ClassVar[list[ConditionSpec]] = []

    #: Files it may write. See :class:`ArtifactSpec`.
    artifacts: ClassVar[list[ArtifactSpec]] = []

    #: Sweep / gradient / MPI support. See :class:`CapabilityFeatures`.
    features: ClassVar[CapabilityFeatures] = CapabilityFeatures()

    #: Known-good parameter sets. See :class:`CapabilityExample`.
    examples: ClassVar[list[CapabilityExample]] = []

    class Params(BaseModel):
        pass

    @classmethod
    def info(cls) -> SolverInfo:
        return SolverInfo(
            name=cls.name,
            title=cls.title,
            description=cls.description,
            geometry_types=cls.geometry_types,
            params_schema=cls.Params.model_json_schema(),
        )

    @classmethod
    def estimates_cost(cls) -> bool:
        """Whether this adapter overrides :meth:`estimate_cells`.

        Asked by the `cost` discovery section, so a caller can tell "this server will size
        my request before running it" from "the wall-clock timeout is the only backstop".
        Compares the underlying functions because ``cls.estimate_cells`` is a bound
        classmethod, and two bindings of the same function are not the same object.
        """
        return cls.estimate_cells.__func__ is not Solver.estimate_cells.__func__

    @classmethod
    def estimate_cells(cls, geometry: Geometry, params: "Solver.Params") -> int | None:
        """Roughly how many cells this job will use, for the server-side cap.

        Called *before* the job is accepted, so it must be cheap — no meshing. Return
        ``None`` when the adapter genuinely cannot say; the job is then admitted and the
        wall-clock timeout remains the backstop. A deliberate over-estimate is safer
        than an under-estimate: the point is refusing work that would exhaust the box,
        and a job rejected with a clear message beats one killed halfway through.
        """
        return None

    @abstractmethod
    def solve(
        self, geometry: Geometry, params: "Solver.Params", ctx: SolverContext
    ) -> SolverResult: ...


def fill_declared_metrics(solver_cls: type[Solver], result: SolverResult) -> None:
    """Compute the declared metrics the adapter did not supply (roadmap M2.5, issue #46).

    Issue #43 let a capability declare that `t_max` is the `max` of the field `T`. This is
    where that declaration becomes a number — once, generically, so that four adapters do not
    each write the same `float(T.max())`. An adapter that already put the metric in
    ``result.metrics`` wins: the derived ones (`t_rise`, `cp_min`) depend on parameters and
    only the adapter can compute them.

    A metric that cannot be computed is **omitted and logged, never guessed**. A declaration
    naming a field the adapter did not emit is a bug in the declaration — there is a test
    that catches it — but a solve at runtime must not fail because of one, and a metric
    reported as 0.0 because the field was missing would be far worse than an absent one.
    """
    for spec in solver_cls.metrics:
        if spec.name in result.metrics or spec.field is None or spec.reduction is None:
            continue
        try:
            result.metrics[spec.name] = fields.reduce_field(
                result.data, result.kind, spec.field, spec.reduction
            )
        except (fields.FieldError, KeyError, ValueError) as exc:
            logger.warning(
                "solver %s declares metric %r over field %r, which this result cannot "
                "supply: %s",
                solver_cls.name,
                spec.name,
                spec.field,
                exc,
            )
