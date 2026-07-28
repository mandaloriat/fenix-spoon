"""Geometry schema — the pydantic source of truth for the wire protocol (docs/04-wire-protocol.md).

Geometry payloads are parametric descriptions; meshing always happens server-side
(NumPy masking for the mock solver, Gmsh for FEniCSx adapters).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Point2D = tuple[float, float]


def _properly_intersect(p1: Point2D, p2: Point2D, p3: Point2D, p4: Point2D) -> bool:
    """True if segments p1-p2 and p3-p4 cross at an interior point of both."""

    def orient(a: Point2D, b: Point2D, c: Point2D) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


class Polygon2D(BaseModel):
    """A closed *simple* polygon; the last point connects back to the first implicitly.

    Self-intersecting outlines are rejected at validation time: downstream meshers
    (Gmsh boolean operations in particular) can hang or produce garbage on them, so
    they must never reach a solver.
    """

    type: Literal["polygon2d"] = "polygon2d"
    points: Annotated[list[Point2D], Field(min_length=3)]

    @model_validator(mode="after")
    def _check_simple(self) -> "Polygon2D":
        pts = self.points
        n = len(pts)
        for i in range(n):
            if pts[i] == pts[(i + 1) % n]:
                raise ValueError(f"duplicate consecutive points at index {i}")
        for i in range(n):
            for j in range(i + 1, n):
                if j == i + 1 or (i == 0 and j == n - 1):
                    continue  # adjacent edges share a vertex, never a proper crossing
                if _properly_intersect(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                    raise ValueError(
                        f"polygon must not self-intersect (edges {i} and {j} cross)"
                    )
        return self


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
