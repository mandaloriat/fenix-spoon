"""The solver adapter protocol.

A solver is a class with a ``name``, a params model, and a ``solve`` method. It knows nothing
about HTTP or jobs; it receives a validated geometry + params and a progress callback, and
returns a :class:`SolverResult`. See ``mock_laplace.py`` for the reference implementation.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel

from ..geometry import Domain2D


class ProgressEvent(BaseModel):
    """One tick of solver progress, streamed to WebSocket subscribers."""

    type: str = "progress"
    iteration: int
    total: int | None = None
    residual: float | None = None
    message: str | None = None


class SolverResult(BaseModel):
    """Result envelope. ``kind`` selects the schema of ``data`` (see docs/04-wire-protocol.md)."""

    kind: str  # "grid2d" | "mesh2d" (reserved)
    data: dict[str, Any]
    artifacts: list[dict[str, Any]] = []


class SolverInfo(BaseModel):
    """What ``GET /api/v1/solvers`` returns per solver."""

    name: str
    title: str
    description: str
    geometry_types: list[str]
    params_schema: dict[str, Any]


ProgressCallback = Callable[[ProgressEvent], None]


class Solver(ABC):
    """Base class for solver adapters.

    Subclasses set ``name``, ``title``, ``description`` and ``Params`` (a pydantic model), and
    implement :meth:`solve`. ``solve`` runs in a worker thread — it must be self-contained and
    call ``progress`` at a sensible cadence (every few hundred ms of work, not every iteration).
    """

    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    geometry_types: ClassVar[list[str]] = ["domain2d"]

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

    @abstractmethod
    def solve(
        self, geometry: Domain2D, params: "Solver.Params", progress: ProgressCallback
    ) -> SolverResult: ...
