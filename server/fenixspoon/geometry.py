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
    points: Annotated[
        list[Point2D],
        Field(
            min_length=3,
            description=(
                "Outline vertices in order, as [x, y] pairs. The closing edge is implicit; "
                "do not repeat the first point."
            ),
        ),
    ]

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


def _check_bounds(bounds: tuple[float, float, float, float]) -> None:
    xmin, ymin, xmax, ymax = bounds
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("bounds must satisfy xmin < xmax and ymin < ymax")


def _check_inside(polygon: Polygon2D, bounds: tuple[float, float, float, float], what: str) -> None:
    xmin, ymin, xmax, ymax = bounds
    for x, y in polygon.points:
        if not (xmin < x < xmax and ymin < y < ymax):
            raise ValueError(f"{what} points must lie strictly inside the domain bounds")


class Domain2D(BaseModel):
    """A rectangular computational domain with a polygonal obstacle (hole) inside it."""

    type: Literal["domain2d"] = "domain2d"
    bounds: tuple[float, float, float, float] = Field(
        default=(-2.0, -1.5, 4.0, 1.5),
        description="Outer rectangle as [xmin, ymin, xmax, ymax], in metres.",
    )
    obstacle: Polygon2D = Field(
        description=(
            "The hole cut out of the domain. Its points must lie strictly inside `bounds`."
        )
    )

    @model_validator(mode="after")
    def _check(self) -> "Domain2D":
        _check_bounds(self.bounds)
        _check_inside(self.obstacle, self.bounds, "obstacle")
        return self


class Region2D(BaseModel):
    """A named material region: a polygon plus solver-interpreted material properties.

    ``material`` is deliberately an open dict of scalars rather than a typed physics
    model — the protocol stays physics-agnostic and each solver documents the keys it
    reads (e.g. ``mu_r`` and ``current_density`` for magnetostatics). Unknown keys are
    ignored by solvers, so a payload can carry properties several solvers care about.
    """

    name: str = Field(
        min_length=1, description="Unique within one geometry; used to label results."
    )
    shape: Polygon2D = Field(description="The region outline, strictly inside `bounds`.")
    material: dict[str, float] = Field(
        default={},
        description=(
            "Solver-interpreted scalar properties, e.g. `mu_r` and `current_density` for "
            "magnetostatics. Keys a solver does not recognise are ignored."
        ),
    )


class Regions2D(BaseModel):
    """A rectangular domain filled with material regions over a background material.

    Unlike :class:`Domain2D` (one hole cut out of the domain), every region here is
    *filled*: the mesh covers the whole rectangle and the physics varies by region.
    This is what problems like a solenoid cross-section need — iron core, copper coil
    carrying a current density, air everywhere else.

    Regions may be nested (an iron core inside a coil): **later entries in the list win**
    where they overlap, like painter's order. Regions whose outlines properly cross are
    rejected, since that describes an ambiguous material assignment rather than nesting.
    """

    type: Literal["regions2d"] = "regions2d"
    bounds: tuple[float, float, float, float] = Field(
        default=(-0.1, -0.1, 0.1, 0.1),
        description="Outer rectangle as [xmin, ymin, xmax, ymax], in metres.",
    )
    regions: Annotated[
        list[Region2D],
        Field(
            min_length=1,
            description=(
                "Material regions in painter's order: where two nest, the later one wins. "
                "Partially overlapping outlines are rejected."
            ),
        ),
    ]
    background: dict[str, float] = Field(
        default={}, description="Material outside every region (typically air)"
    )

    @model_validator(mode="after")
    def _check(self) -> "Regions2D":
        _check_bounds(self.bounds)
        names = [r.name for r in self.regions]
        if len(set(names)) != len(names):
            raise ValueError("region names must be unique")
        for region in self.regions:
            _check_inside(region.shape, self.bounds, f"region {region.name!r}")
        for i, a in enumerate(self.regions):
            for b in self.regions[i + 1 :]:
                if _outlines_cross(a.shape.points, b.shape.points):
                    raise ValueError(
                        f"regions {a.name!r} and {b.name!r} overlap partially; regions must be "
                        "disjoint or fully nested"
                    )
        return self


def _outlines_cross(a: list[Point2D], b: list[Point2D]) -> bool:
    """True if two polygon outlines properly cross (partial overlap, not nesting)."""
    for i in range(len(a)):
        for j in range(len(b)):
            if _properly_intersect(
                a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)]
            ):
                return True
    return False


# Discriminated union of every geometry kind the protocol knows about.
Geometry = Annotated[Domain2D | Regions2D, Field(discriminator="type")]
