"""Typed models for wire-protocol payloads (docs/04-wire-protocol.md).

This module is the machine-checkable side of the protocol: the conformance fixtures in
``protocol/fixtures/`` are validated against these models in CI, and the JS SDK (roadmap
M2) mirrors them. Handlers may still build plain dicts, but anything they emit must parse
with these models — the fixture suite also validates live server output.
"""

from typing import Any, Literal

from pydantic import BaseModel, model_validator

from .geometry import Domain2D, Geometry, Polygon2D  # noqa: F401  (re-export for consumers)
from .solvers.base import ProgressEvent  # noqa: F401  (re-export for consumers)


class StatusEvent(BaseModel):
    type: Literal["status"]
    status: Literal["running", "done", "failed", "cancelled"]
    error: str | None = None


class Grid2DData(BaseModel):
    bounds: tuple[float, float, float, float]
    shape: tuple[int, int]  # [ny, nx]
    fields: dict[str, list[float]]
    mask: list[int]

    @model_validator(mode="after")
    def _check(self) -> "Grid2DData":
        ny, nx = self.shape
        n = ny * nx
        if len(self.mask) != n:
            raise ValueError(f"mask has {len(self.mask)} entries, expected ny*nx={n}")
        for name, values in self.fields.items():
            if len(values) != n:
                raise ValueError(f"field {name!r} has {len(values)} entries, expected {n}")
        return self


class Mesh2DData(BaseModel):
    bounds: tuple[float, float, float, float]
    points: list[tuple[float, float]]
    triangles: list[tuple[int, int, int]]
    point_fields: dict[str, list[float]]

    @model_validator(mode="after")
    def _check(self) -> "Mesh2DData":
        n = len(self.points)
        for tri in self.triangles:
            if any(i < 0 or i >= n for i in tri):
                raise ValueError(f"triangle {tri} references a node outside 0..{n - 1}")
        for name, values in self.point_fields.items():
            if len(values) != n:
                raise ValueError(f"point field {name!r} has {len(values)} entries, expected {n}")
        return self


class ArtifactRef(BaseModel):
    name: str
    content_type: str
    size: int
    url: str


class ResultEnvelope(BaseModel):
    job_id: str
    kind: Literal["grid2d", "mesh2d"]
    data: dict[str, Any]
    stats: dict[str, float] = {}
    """What the solve cost. Keys are server-defined and all optional — clients display
    them, they never branch on one being present."""
    artifacts: list[ArtifactRef] = []

    @model_validator(mode="after")
    def _check_data(self) -> "ResultEnvelope":
        model = {"grid2d": Grid2DData, "mesh2d": Mesh2DData}[self.kind]
        model.model_validate(self.data)
        return self
