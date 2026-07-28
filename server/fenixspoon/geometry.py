"""Geometry schema — the pydantic source of truth for the wire protocol (docs/04-wire-protocol.md).

Geometry payloads are parametric descriptions; meshing always happens server-side
(NumPy masking for the mock solver, Gmsh for FEniCSx adapters).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Point2D = tuple[float, float]


class Polygon2D(BaseModel):
    """A closed polygon; the last point connects back to the first implicitly."""

    type: Literal["polygon2d"] = "polygon2d"
    points: Annotated[list[Point2D], Field(min_length=3)]


class Domain2D(BaseModel):
    """A rectangular computational domain with a polygonal obstacle (hole) inside it."""

    type: Literal["domain2d"] = "domain2d"
    bounds: tuple[float, float, float, float] = Field(
        default=(-2.0, -1.5, 4.0, 1.5), description="[xmin, ymin, xmax, ymax]"
    )
    obstacle: Polygon2D

    @model_validator(mode="after")
    def _check(self) -> "Domain2D":
        xmin, ymin, xmax, ymax = self.bounds
        if xmax <= xmin or ymax <= ymin:
            raise ValueError("bounds must satisfy xmin < xmax and ymin < ymax")
        for x, y in self.obstacle.points:
            if not (xmin < x < xmax and ymin < y < ymax):
                raise ValueError("obstacle points must lie strictly inside the domain bounds")
        return self


# Discriminated union of every geometry kind the protocol knows about.
Geometry = Annotated[Domain2D, Field(discriminator="type")]
