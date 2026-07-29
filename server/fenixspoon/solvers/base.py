"""The solver adapter protocol.

A solver is a class with a ``name``, a params model, and a ``solve`` method. It knows nothing
about HTTP or jobs; it receives a validated geometry + params and a :class:`SolverContext`
providing progress reporting, cooperative cancellation, and artifact registration. See
``mock_laplace.py`` for the reference implementation.
"""

import mimetypes
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ..geometry import Geometry


class JobCancelled(Exception):
    """Raised inside a solver (via ``SolverContext.check_cancelled``) to abort a run."""


class ProgressEvent(BaseModel):
    """One tick of solver progress, streamed to WebSocket subscribers."""

    type: str = "progress"
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

    kind: str = Field(description='Result schema selector: `"grid2d"` or `"mesh2d"`.')
    data: dict[str, Any] = Field(description="The field data, shaped according to `kind`.")
    stats: dict[str, float] = {}
    """What the solve actually cost: ``cells``, ``dofs``, and whatever else the adapter
    knows. The job manager adds ``seconds``. Reported so a user can see why a job was
    slow, and so an operator can pick a sensible cap."""


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


class ArtifactSpec(BaseModel):
    """A file this capability may write alongside the inline result (issue #43).

    Declaring them lets a caller decide whether to ask for the VTK *before* submitting,
    rather than discovering after the fact that the only way to get the field out was a
    parameter it left at the default.
    """

    name: str = Field(description="Filename the solver registers, e.g. `solution.vtk`.")
    content_type: str = Field(description="MIME type it is served with.")
    description: str = Field(description="What the file contains, and what opens it.")
    when: str | None = Field(
        default=None,
        description=(
            "Name of the boolean param that has to be true for this file to appear; "
            "null if it is always written."
        ),
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
    ) -> None:
        self._progress_cb = progress_cb
        self._cancel_event = cancel_event or threading.Event()
        self._artifact_dir = artifact_dir
        self._artifacts: list[dict[str, Any]] = []

    def progress(self, event: ProgressEvent) -> None:
        self._progress_cb(event)

    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise JobCancelled()

    def artifact(self, name: str, content_type: str | None = None) -> Path:
        """Register an output file and return the path the solver should write it to.

        ``name`` must be a bare filename — path separators are rejected so artifacts can
        never escape the job directory.
        """
        if not name or "/" in name or "\\" in name or name.startswith(".") or ".." in name:
            raise ValueError(f"invalid artifact name: {name!r}")
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
        self._artifacts.append({"name": name, "content_type": content_type})
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

    The eight attributes below ``geometry_types`` are the **capability declaration** added
    for progressive discovery (roadmap M2.5, issue #43). Every one of them has a default, so
    an existing adapter keeps working untouched and reports ``"unspecified"`` where it has
    not said. They are plain class attributes in the spirit of ``Params`` rather than a
    registration framework: declaring a metric is one line in a list, and forgetting to
    declare anything costs a caller information, not a crash.
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
    #: satisfied by construction. Worth reporting anyway, because *versions* differ.
    requires: ClassVar[list[str]] = []

    #: Scalar engineering quantities this capability reports. See :class:`MetricSpec`.
    metrics: ClassVar[list[MetricSpec]] = []

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
