"""Turning a named boundary into something a solver can use (#85).

The geometry says *where* a boundary is; this resolves that into the one shape both halves of
an adapter pair can consume — **a predicate over coordinates**, `f(x) -> bool` for an array
of points shaped `(2, N)`.

That signature is not arbitrary. It is exactly what `dolfinx.mesh.locate_entities_boundary`
takes, so a FEniCSx adapter passes it straight through; and it is a plain NumPy mask for a
mock working on a raster. One resolution, two consumers, no chance of the pair disagreeing
about which edge was meant — which is the same reasoning that put the elasticity stress
post-processing in one place.

## Why a predicate rather than a list of entities

A list would have to be a list *of something*: vertex indices on the raster, facet tags on
the mesh, and those are different things the two adapters would each derive their own way. A
predicate is the greatest common denominator, and it is the level at which "the left edge"
means the same to both.

The cost is that a `points` selector — the one that follows the shape rather than the space —
becomes a predicate over the *segments those points span*, evaluated as "within tolerance of
one of them". Exact for the vertices, and correct in between, which is what a boundary
condition needs.
"""

import numpy as np

from .geometry import (
    AllOfSelector,
    BoundarySpec,
    BoxSelector,
    NearSelector,
    PartSelector,
    PointsSelector,
    Polygon2D,
    spanned_edges,
)

#: Fraction of the domain's shorter side used as the default tolerance when matching a point
#: against a segment. Relative rather than absolute because a geometry may be a metre or a
#: millimetre across, and an absolute default would be either useless or wrong on one of them.
SEGMENT_TOLERANCE = 1e-6


def named(geometry, name: str) -> BoundarySpec:
    """The boundary a load case is referring to, or a message naming what does exist.

    The alternative — returning something empty — is the failure this whole design is trying
    to avoid: a condition applied to nothing, on a solve that runs and answers a different
    problem.
    """
    for entry in getattr(geometry, "boundaries", []):
        if entry.name == name:
            return entry
    declared = [entry.name for entry in getattr(geometry, "boundaries", [])]
    raise KeyError(
        f"this geometry declares no boundary named {name!r}"
        + (f"; it has {declared}" if declared else " (it names none at all)")
    )


def predicate(geometry, name: str):
    """A `f(x) -> bool` mask for the named boundary, over points shaped `(2, N)`."""
    return _resolve(named(geometry, name).select, geometry)


def governed_by_load_case(ctx, params, shorthand: tuple[str, ...]) -> bool:
    """Whether the load case governs this solve, refusing a request that asks for both (#85).

    An adapter that predates load cases keeps its parameter shorthand — elasticity's
    `fixed_edge` / `load_edge` are the case #85 names — and the two ways of saying where the
    conditions go must not both be in force at once. A caller that sends a load case *and*
    sets `load_edge` has two intents in one request, and applying either silently is the
    failure mode this design keeps refusing: a solve that runs, converges, and answers a
    different problem.

    Only an **explicitly set** shorthand conflicts. Every one of them has a default, so a
    request that never mentions them still arrives with values, and reading those as an intent
    would make a load case unusable without also passing sentinels. ``model_fields_set``
    survives ``model_validate``, so what the caller actually wrote is knowable here.

    Raises :class:`ValueError`, which fails the job with the message rather than returning a
    number. Not a submit-time refusal like the other three, and the reason is a boundary this
    module respects: the core validates a load case against the *geometry* and the capability's
    *declaration*, both of which it can read, while which params are shorthand for what is
    knowledge only the adapter has. Pushing that into the core would mean the core knowing an
    adapter's parameters by name.
    """
    if not getattr(ctx, "conditions", None):
        return False
    stated = [name for name in shorthand if name in params.model_fields_set]
    if stated:
        raise ValueError(
            f"this job carries a load case and also sets {stated}: the load case puts the "
            "conditions on boundaries the geometry names, and those parameters put them on "
            "edges of the bounding rectangle. Send one or the other."
        )
    return True


def _resolve(selector, geometry):
    if isinstance(selector, NearSelector):
        axis = 0 if selector.axis == "x" else 1
        return lambda x: np.abs(np.asarray(x)[axis] - selector.value) <= selector.tol
    if isinstance(selector, BoxSelector):
        xmin, ymin, xmax, ymax = selector.bounds
        return lambda x: (
            (np.asarray(x)[0] >= xmin)
            & (np.asarray(x)[0] <= xmax)
            & (np.asarray(x)[1] >= ymin)
            & (np.asarray(x)[1] <= ymax)
        )
    if isinstance(selector, AllOfSelector):
        inner = [_resolve(item, geometry) for item in selector.of]
        return lambda x: np.logical_and.reduce([test(x) for test in inner])
    if isinstance(selector, PointsSelector):
        return _segments_predicate(selector.ids, geometry)
    if isinstance(selector, PartSelector):
        return _part_predicate(selector.of, geometry)
    raise TypeError(f"unresolvable selector: {selector!r}")  # pragma: no cover - union is closed


def _polygons(geometry) -> list[Polygon2D]:
    if hasattr(geometry, "obstacle"):
        return [geometry.obstacle]
    return [region.shape for region in geometry.regions]


def _segments_predicate(ids: list[str], geometry):
    """Points within tolerance of the edges spanned by the named vertices.

    "Spanned" means consecutive in the outline: ids `p3, p4, p5` select the two edges between
    them, not the three vertices alone. A boundary condition applies to an *edge*, so
    selecting vertices and stopping there would put a clamp on three points of a mesh and
    leave the metal between them free.
    """
    segments: list[tuple[np.ndarray, np.ndarray]] = []
    for polygon in _polygons(geometry):
        points = np.asarray(polygon.points, dtype=float)
        # `spanned_edges` rather than adjacency worked out here: the geometry validates a
        # selector by asking the same function whether it spans anything, and two
        # implementations of "adjacent" could disagree in exactly the direction that produces
        # a boundary which validates and then matches nothing.
        segments.extend(
            (points[first], points[second]) for first, second in spanned_edges(polygon, ids)
        )

    span = _extent(geometry)

    def test(x):
        coords = np.asarray(x, dtype=float)
        hit = np.zeros(coords.shape[1], dtype=bool)
        for start, end in segments:
            hit |= _distance_to_segment(coords, start, end) <= SEGMENT_TOLERANCE * span
        return hit

    return test


def _part_predicate(part: str, geometry):
    """`outer`, `obstacle`, or `region:<name>` — the topology the adapters already assume."""
    span = _extent(geometry)
    if part == "outer":
        xmin, ymin, xmax, ymax = geometry.bounds
        tol = SEGMENT_TOLERANCE * span
        return lambda x: (
            (np.abs(np.asarray(x)[0] - xmin) <= tol)
            | (np.abs(np.asarray(x)[0] - xmax) <= tol)
            | (np.abs(np.asarray(x)[1] - ymin) <= tol)
            | (np.abs(np.asarray(x)[1] - ymax) <= tol)
        )
    if part == "obstacle":
        if not hasattr(geometry, "obstacle"):
            raise KeyError("this geometry has no obstacle; `part: obstacle` needs a domain2d")
        return _outline_predicate(geometry.obstacle, span)
    if part.startswith("region:"):
        wanted = part.split(":", 1)[1]
        for region in getattr(geometry, "regions", []):
            if region.name == wanted:
                return _outline_predicate(region.shape, span)
        raise KeyError(f"no region named {wanted!r} in this geometry")
    raise KeyError(f"unknown part {part!r}; expected `outer`, `obstacle` or `region:<name>`")


def _outline_predicate(polygon: Polygon2D, span: float):
    points = np.asarray(polygon.points, dtype=float)
    edges = [(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]

    def test(x):
        coords = np.asarray(x, dtype=float)
        hit = np.zeros(coords.shape[1], dtype=bool)
        for start, end in edges:
            hit |= _distance_to_segment(coords, start, end) <= SEGMENT_TOLERANCE * span
        return hit

    return test


def _extent(geometry) -> float:
    xmin, ymin, xmax, ymax = geometry.bounds
    return float(min(xmax - xmin, ymax - ymin))


def _distance_to_segment(coords: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    """Perpendicular distance from each point to a segment, clamped to its ends."""
    direction = end - start
    length_squared = float(direction @ direction)
    offset = coords - start[:, None]
    if length_squared == 0.0:  # pragma: no cover - polygons reject duplicate points
        return np.hypot(offset[0], offset[1])
    along = np.clip((direction[0] * offset[0] + direction[1] * offset[1]) / length_squared, 0, 1)
    closest = start[:, None] + direction[:, None] * along
    return np.hypot(coords[0] - closest[0], coords[1] - closest[1])
