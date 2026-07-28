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
from typing import Any, ClassVar

from pydantic import BaseModel

from ..geometry import Geometry


class JobCancelled(Exception):
    """Raised inside a solver (via ``SolverContext.check_cancelled``) to abort a run."""


class ProgressEvent(BaseModel):
    """One tick of solver progress, streamed to WebSocket subscribers."""

    type: str = "progress"
    iteration: int
    total: int | None = None
    residual: float | None = None
    message: str | None = None


class SolverResult(BaseModel):
    """Result envelope. ``kind`` selects the schema of ``data`` (see docs/04-wire-protocol.md)."""

    kind: str  # "grid2d" | "mesh2d"
    data: dict[str, Any]


class SolverInfo(BaseModel):
    """What ``GET /api/v1/solvers`` returns per solver."""

    name: str
    title: str
    description: str
    geometry_types: list[str]
    params_schema: dict[str, Any]


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
        self, geometry: Geometry, params: "Solver.Params", ctx: SolverContext
    ) -> SolverResult: ...
