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
    point_ids: list[str] | None = Field(
        default=None,
        description=(
            "Stable identifiers for the vertices, one per point, in the same order. Optional "
            "and absent by default — a geometry that never names a boundary has no use for "
            "them (protocol 1.8, #85).\n\n"
            "They exist because *indices* cannot survive editing: insert a control point and "
            "every index after it shifts, moving a named boundary onto a different edge with "
            "nothing to notice. An id follows the point it was given to, which is what makes "
            "`select: {type: points}` mean the same edge after a shape change. The same idea "
            "the workspace already applies to objects, one level down."
        ),
    )

    @model_validator(mode="after")
    def _check_point_ids(self) -> "Polygon2D":
        """One id per point, all distinct — the two properties a selector relies on.

        Length is not a formality: an id list that has fallen out of step with the points is
        worse than none at all, because a selector would resolve to the wrong vertex rather
        than failing. Distinctness matters for the same reason.
        """
        if self.point_ids is None:
            return self
        if len(self.point_ids) != len(self.points):
            raise ValueError(
                f"point_ids has {len(self.point_ids)} entries for {len(self.points)} points; "
                "there must be exactly one per point, in the same order"
            )
        if len(set(self.point_ids)) != len(self.point_ids):
            raise ValueError("point_ids must be unique within one polygon")
        return self

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


class PartSelector(BaseModel):
    """A named piece of the geometry's topology: the outer boundary, the hole, a region.

    The cheapest of the three and the one that needed no invention — it is what the existing
    adapters already do implicitly. Potential flow puts the free stream on the outer boundary
    and a streamline on the obstacle; heat convects from every exposed face. Writing that down
    costs nothing and makes it addressable.
    """

    type: Literal["part"] = "part"
    of: str = Field(
        description=(
            "`outer` for the bounding rectangle, `obstacle` for a `domain2d` hole, or "
            "`region:<name>` for the boundary of a named `regions2d` region."
        )
    )


class PointsSelector(BaseModel):
    """The edges spanned by named vertices — the selector that survives editing.

    Vertex *indices* would not: insert a control point and every index after it shifts. An id
    follows the point it was given to, so a clamped root stays on the same physical edge after
    the profile changes. Requires the polygon to carry `point_ids`; a geometry without them
    cannot be selected this way, and says so rather than guessing.
    """

    type: Literal["points"] = "points"
    ids: Annotated[
        list[str],
        Field(
            min_length=2,
            description=(
                "Point ids, from the polygon's `point_ids`. At least two, and at least two of "
                "them adjacent in the outline: this selects the *edges* they span, and one "
                "vertex spans none. The geometry checks the adjacency, since ids can be two "
                "apart and satisfy a length rule while selecting nothing."
            ),
        ),
    ]


class NearSelector(BaseModel):
    """Everything within `tol` of a coordinate value — the dolfinx idiom, as data.

    `np.isclose(x[0], 0.0)` is how a FEniCSx user says "the left edge", and it is the right
    tool when the boundary is a statement about *space* rather than about the shape: a
    symmetry plane stays the symmetry plane whatever profile is put on it.
    """

    type: Literal["near"] = "near"
    axis: Literal["x", "y"] = Field(description="Which coordinate is being tested.")
    value: float = Field(description="The value it must be near.")
    tol: float = Field(default=1e-9, gt=0.0, description="Half-width of the band, in metres.")


class BoxSelector(BaseModel):
    """Everything inside an axis-aligned rectangle. `near` for a strip, this for a corner."""

    type: Literal["box"] = "box"
    bounds: tuple[float, float, float, float] = Field(
        description="Inclusive `[xmin, ymin, xmax, ymax]`."
    )

    @model_validator(mode="after")
    def _check(self) -> "BoxSelector":
        _check_bounds(self.bounds)
        return self


class AllOfSelector(BaseModel):
    """Intersection of the selectors it holds: the loaded edge *and* only its upper half.

    An intersection rather than an expression language, and the difference is the point.
    Accepting arbitrary predicates would be the UFL-over-the-wire argument again in
    miniature — a small closed set of primitives says what the cases here need and nothing a
    client could turn into code.
    """

    type: Literal["all_of"] = "all_of"
    of: Annotated[
        list["BoundarySelector"],
        Field(min_length=2, description="Selectors that must all hold."),
    ]


#: How a boundary is picked out. Three families, deliberately: `part` follows the topology,
#: `points` follows the *shape* through an edit, and `near`/`box` follow the *space*. Neither
#: of the last two can express the other honestly — a clamped root should stay on the same
#: physical edge when the profile changes, while a symmetry plane is a statement about
#: position — which is why both are here rather than one of them being the winner.
BoundarySelector = Annotated[
    PartSelector | PointsSelector | NearSelector | BoxSelector | AllOfSelector,
    Field(discriminator="type"),
]


class BoundarySpec(BaseModel):
    """A named piece of a geometry's boundary (protocol 1.8, #85).

    The geometry says *where*; a load case says what happens there. That split is what lets
    three load cases share one shape — putting the conditions here would mint a new geometry
    revision for every load change, and turn comparing two load cases into comparing two
    shapes.
    """

    name: str = Field(
        min_length=1,
        description=(
            "What a load case refers to. Unique within one geometry, and worth choosing as a "
            "convention — `root`/`tip` across a family of shapes is what makes one load case "
            "reusable on all of them."
        ),
    )
    select: BoundarySelector = Field(description="Which part of the boundary this names.")
    description: str | None = Field(
        default=None, description="What this boundary is, for a caller reading the geometry."
    )


def spanned_edges(polygon: Polygon2D, ids: list[str]) -> list[tuple[int, int]]:
    """Vertex-index pairs for the edges the named points span, in outline order.

    The single definition of what a `points` selector *means*, used by validation here and by
    the resolver in :mod:`fenixspoon.boundaries`. Sharing it is the point: if the check and
    the resolution computed adjacency separately they could disagree, and the disagreement
    would take the shape this design is trying to make impossible — a boundary that validates
    and then matches nothing.

    Two vertices are adjacent when they are consecutive in the outline, and the outline wraps,
    so the last and the first are neighbours like any other pair.
    """
    if not polygon.point_ids:
        return []
    index = {pid: position for position, pid in enumerate(polygon.point_ids)}
    chosen = sorted(index[pid] for pid in ids if pid in index)
    edges = [
        (first, second)
        for first, second in zip(chosen, chosen[1:], strict=False)
        if second == first + 1
    ]
    last = len(polygon.points) - 1
    if last in chosen and 0 in chosen:
        edges.append((last, 0))
    return edges


def _check_boundaries(boundaries: list[BoundarySpec], polygons: list[Polygon2D]) -> None:
    """Names are unique, and a `points` selector actually picks something out.

    Two checks, and the second is the one that earns its keep. Ids existing is not enough:
    `["a", "c"]` names two real vertices that are not adjacent, spans no edge, and would
    resolve to a predicate that is false everywhere. A boundary that matches nothing is a
    condition applied to nothing, on a solve that runs and answers a different problem — the
    exact failure this design exists to prevent, so it is refused rather than tested for.
    """
    names = [entry.name for entry in boundaries]
    if len(set(names)) != len(names):
        raise ValueError("boundary names must be unique within one geometry")
    known = {pid for polygon in polygons if polygon.point_ids for pid in polygon.point_ids}
    for entry in boundaries:
        for selector in _point_selectors(entry.select):
            for wanted in selector.ids:
                if wanted not in known:
                    raise ValueError(
                        f"boundary {entry.name!r} selects point id {wanted!r}, which no polygon "
                        f"in this geometry declares"
                        + ("" if known else " (no polygon carries `point_ids` at all)")
                    )
            if not any(spanned_edges(polygon, selector.ids) for polygon in polygons):
                raise ValueError(
                    f"boundary {entry.name!r} selects points {selector.ids} that span no edge; "
                    "a `points` selector names the edges *between* consecutive vertices, so at "
                    "least two of them must be adjacent in one polygon's outline"
                )


def _point_selectors(selector) -> list["PointsSelector"]:
    """Every `points` selector inside one, including within an `all_of`."""
    if isinstance(selector, PointsSelector):
        return [selector]
    if isinstance(selector, AllOfSelector):
        return [found for inner in selector.of for found in _point_selectors(inner)]
    return []


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
    boundaries: list[BoundarySpec] = Field(
        default=[],
        description=(
            "Named pieces of the boundary a load case can refer to (protocol 1.8, #85). "
            "Empty by default: every adapter shipped today puts its conditions where the "
            "outer/obstacle split implies them, and naming is for the physics where that is "
            "not enough."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> "Domain2D":
        _check_bounds(self.bounds)
        _check_inside(self.obstacle, self.bounds, "obstacle")
        _check_boundaries(self.boundaries, [self.obstacle])
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
    boundaries: list[BoundarySpec] = Field(
        default=[],
        description=(
            "Named pieces of the boundary a load case can refer to (protocol 1.8, #85). "
            "Empty by default."
        ),
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
        _check_boundaries(self.boundaries, [region.shape for region in self.regions])
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
