"""Typed models for wire-protocol payloads (docs/04-wire-protocol.md).

This module is the machine-checkable side of the protocol: the conformance fixtures in
``protocol/fixtures/`` are validated against these models in CI, and the JS SDK (roadmap
M2) mirrors them. Handlers may still build plain dicts, but anything they emit must parse
with these models — the fixture suite also validates live server output.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .geometry import Domain2D, Geometry, Polygon2D  # noqa: F401  (re-export for consumers)
from .solvers.base import ProgressEvent  # noqa: F401  (re-export for consumers)


class StatusEvent(BaseModel):
    """A job's lifecycle transition. The stream ends after a terminal one."""

    type: Literal["status"]
    status: Literal["running", "done", "failed", "cancelled"] = Field(
        description="`done`, `failed` and `cancelled` are terminal; the stream closes after."
    )
    error: str | None = Field(
        default=None, description="Why the job failed. Null unless `status` is `failed`."
    )


class Grid2DData(BaseModel):
    """Fields sampled on a regular grid. Arrays are row-major, y increasing upward."""

    bounds: tuple[float, float, float, float] = Field(
        description="Area the grid covers, as [xmin, ymin, xmax, ymax]."
    )
    shape: tuple[int, int] = Field(description="Grid size as [ny, nx], rows before columns.")
    fields: dict[str, list[float]] = Field(
        description="Field name to ny*nx values, indexed `[iy * nx + ix]`."
    )
    mask: list[int] = Field(
        description="One entry per grid point, 1 inside the obstacle. Same indexing as a field."
    )

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
    """An unstructured triangle mesh with one value per node per field."""

    bounds: tuple[float, float, float, float] = Field(
        description="Bounding box of the mesh, as [xmin, ymin, xmax, ymax]."
    )
    points: list[tuple[float, float]] = Field(description="Node coordinates as [x, y] pairs.")
    triangles: list[tuple[int, int, int]] = Field(
        description="Each triangle as three indices into `points`."
    )
    point_fields: dict[str, list[float]] = Field(
        description="Field name to one value per node, in `points` order."
    )

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
    """A downloadable file the solver produced alongside the inline result."""

    name: str = Field(description="Bare filename, e.g. `solution.vtk`.")
    content_type: str = Field(description="MIME type to serve it with.")
    size: int = Field(description="Size on disk in bytes.")
    url: str = Field(description="Server-relative download path; join with the API base URL.")


class ResultEnvelope(BaseModel):
    """What `GET /api/v1/jobs/{id}/result` returns once a job is `done`."""

    job_id: str = Field(description="The job this result belongs to.")
    kind: Literal["grid2d", "mesh2d"] = Field(
        description="Selects the schema of `data`: `Grid2DData` or `Mesh2DData`."
    )
    data: dict[str, Any] = Field(description="The field data, shaped according to `kind`.")
    stats: dict[str, float] = Field(
        default={},
        description=(
            "What the solve cost — `cells`, `dofs`, `iterations`, `seconds`. Keys are "
            "server-defined and all optional: display them, never branch on one existing."
        ),
    )
    artifacts: list[ArtifactRef] = Field(
        default=[], description="Files the solver wrote, downloadable from the artifact endpoint."
    )

    @model_validator(mode="after")
    def _check_data(self) -> "ResultEnvelope":
        model = {"grid2d": Grid2DData, "mesh2d": Mesh2DData}[self.kind]
        model.model_validate(self.data)
        return self
