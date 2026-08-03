"""Naming a piece of a geometry's boundary (#85, first half).

The geometry says **where**; a load case will say what happens there. This file is the
"where": stable point ids, the three selector families, and the resolution both halves of an
adapter pair will share.

The test that justifies the whole design is
:func:`test_an_id_selected_boundary_survives_an_edit_that_would_break_an_index` — everything
else here is machinery in service of that one property.
"""

import numpy as np
import pytest
from pydantic import ValidationError

from fenixspoon.boundaries import predicate
from fenixspoon.geometry import (  # noqa: I001
    AllOfSelector,
    BoundarySpec,
    BoxSelector,
    Domain2D,
    NearSelector,
    PartSelector,
    PointsSelector,
    Polygon2D,
    Region2D,
    Regions2D,
    spanned_edges,
)

SQUARE = Polygon2D(
    points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
    point_ids=["a", "b", "c", "d"],
)


def domain(*boundaries: BoundarySpec, obstacle: Polygon2D = SQUARE) -> Domain2D:
    return Domain2D(bounds=(-2.0, -2.0, 3.0, 3.0), obstacle=obstacle, boundaries=list(boundaries))


def hits(geometry, name: str, *points: tuple[float, float]) -> list[bool]:
    coords = np.array([[x for x, _ in points], [y for _, y in points]], dtype=float)
    return [bool(value) for value in predicate(geometry, name)(coords)]


# --------------------------------------------------------------- the point of the exercise


def test_an_id_selected_boundary_survives_an_edit_that_would_break_an_index():
    """The reason ids exist, stated as the failure they prevent.

    A caller names the bottom edge, then inserts a control point *before* it — the edit an
    editor makes constantly. With indices the named boundary would silently slide onto a
    different edge, and a clamp would end up somewhere nobody chose. With ids it stays where
    it was put.

    Asserted by resolving the same name on both revisions and requiring the same answer at
    the same physical points, which is the property a caller actually depends on.
    """
    before = domain(BoundarySpec(name="root", select=PointsSelector(ids=["c", "d"])))

    # Insert a vertex between `a` and `b`: every index at or after 1 shifts by one.
    inserted = Polygon2D(
        points=[(0.0, 0.0), (0.5, -0.2), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
        point_ids=["a", "new", "b", "c", "d"],
    )
    after = domain(
        BoundarySpec(name="root", select=PointsSelector(ids=["c", "d"])), obstacle=inserted
    )

    on_the_top_edge = (0.5, 1.0)
    on_the_bottom_edge = (0.5, 0.0)
    assert hits(before, "root", on_the_top_edge, on_the_bottom_edge) == [True, False]
    assert hits(after, "root", on_the_top_edge, on_the_bottom_edge) == [True, False]


def test_the_same_boundary_by_position_does_not_follow_the_shape():
    """The other half of why both families exist: a predicate follows the *space*.

    Move the shape and a `near` boundary stays where it was — which is right for a symmetry
    plane and wrong for a clamped root. Neither selector can express the other honestly, so
    this asserts the difference rather than treating one as the winner.
    """
    moved = Polygon2D(
        points=[(0.0, 0.5), (1.0, 0.5), (1.0, 1.5), (0.0, 1.5)],
        point_ids=["a", "b", "c", "d"],
    )
    by_position = BoundarySpec(name="plane", select=NearSelector(axis="y", value=0.0, tol=1e-9))
    by_shape = BoundarySpec(name="bottom", select=PointsSelector(ids=["a", "b"]))

    before = domain(by_position, by_shape)
    after = domain(by_position, by_shape, obstacle=moved)

    # The plane is where it always was; the bottom edge went with the shape.
    assert hits(before, "plane", (0.5, 0.0)) == [True]
    assert hits(after, "plane", (0.5, 0.0)) == [True]
    assert hits(before, "bottom", (0.5, 0.0)) == [True]
    assert hits(after, "bottom", (0.5, 0.0)) == [False]
    assert hits(after, "bottom", (0.5, 0.5)) == [True]


# ------------------------------------------------------------------ the selector families


def test_points_select_the_edges_between_them_not_only_the_vertices():
    """A condition applies to an edge. Selecting three vertices and stopping there would
    clamp three points of a mesh and leave the metal between them free."""
    geometry = domain(BoundarySpec(name="two_edges", select=PointsSelector(ids=["a", "b", "c"])))
    midpoint_of_first = (0.5, 0.0)
    midpoint_of_second = (1.0, 0.5)
    midpoint_of_unselected = (0.5, 1.0)
    assert hits(geometry, "two_edges", midpoint_of_first, midpoint_of_second) == [True, True]
    assert hits(geometry, "two_edges", midpoint_of_unselected) == [False]


def test_the_closing_edge_counts_as_adjacent():
    """The outline wraps, so the last vertex and the first are neighbours like any other pair.
    Missing this would make one edge of every polygon unselectable."""
    geometry = domain(BoundarySpec(name="closing", select=PointsSelector(ids=["d", "a"])))
    assert hits(geometry, "closing", (0.0, 0.5)) == [True]
    assert hits(geometry, "closing", (0.5, 0.0)) == [False]


def test_part_selectors_name_the_topology_the_adapters_already_assume():
    """`outer` and `obstacle` are what potential flow and magnetostatics do implicitly.
    Writing them down costs nothing and makes them addressable by a load case."""
    geometry = domain(
        BoundarySpec(name="far", select=PartSelector(of="outer")),
        BoundarySpec(name="body", select=PartSelector(of="obstacle")),
    )
    assert hits(geometry, "far", (-2.0, 0.0), (0.5, 0.0)) == [True, False]
    assert hits(geometry, "body", (0.5, 0.0), (-2.0, 0.0)) == [True, False]


def test_a_region_boundary_is_selectable_by_name():
    geometry = Regions2D(
        bounds=(-2.0, -2.0, 3.0, 3.0),
        regions=[Region2D(name="core", shape=SQUARE, material={"k": 1.0})],
        boundaries=[BoundarySpec(name="core_edge", select=PartSelector(of="region:core"))],
    )
    assert hits(geometry, "core_edge", (0.5, 0.0), (-1.5, -1.5)) == [True, False]

    missing = Regions2D(
        bounds=(-2.0, -2.0, 3.0, 3.0),
        regions=[Region2D(name="core", shape=SQUARE, material={})],
        boundaries=[BoundarySpec(name="x", select=PartSelector(of="region:absent"))],
    )
    with pytest.raises(KeyError, match="no region named"):
        predicate(missing, "x")


def test_all_of_is_an_intersection_and_not_an_expression_language():
    """The loaded edge *and* only its lower half — enough for the cases here, and nothing a
    client could turn into code. Accepting arbitrary predicates would be the
    UFL-over-the-wire argument again in miniature."""
    geometry = domain(
        BoundarySpec(
            name="lower_left",
            select=AllOfSelector(
                of=[PartSelector(of="outer"), BoxSelector(bounds=(-2.1, -2.1, -1.9, 0.0))]
            ),
        )
    )
    assert hits(geometry, "lower_left", (-2.0, -1.0), (-2.0, 1.0), (0.0, -2.0)) == [
        True,
        False,
        False,
    ]


# ------------------------------------------------------------------------- the invariants


def test_a_boundary_cannot_select_a_point_the_geometry_never_declared():
    """A selector matching nothing is the failure mode this design exists to prevent: a
    condition applied to nothing, on a solve that runs and answers a different problem."""
    with pytest.raises(ValidationError, match="which no polygon in this geometry declares"):
        domain(BoundarySpec(name="root", select=PointsSelector(ids=["a", "nope"])))


def test_a_geometry_with_no_point_ids_says_so_rather_than_matching_nothing():
    bare = Polygon2D(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
    with pytest.raises(ValidationError, match="no polygon carries `point_ids` at all"):
        domain(BoundarySpec(name="root", select=PointsSelector(ids=["a", "b"])), obstacle=bare)


def test_points_that_span_no_edge_are_refused_rather_than_matching_nothing():
    """The hole a review found: existing ids are not enough, they have to be *adjacent*.

    `a` and `c` are two real vertices of the square with a vertex between them. They satisfy
    every rule about ids existing, span no edge, and would have resolved to a predicate false
    everywhere — a boundary that validates and then silently matches nothing, which is the one
    outcome this whole design is arranged to prevent.

    Refused rather than tested for, and refused using the *same* function the resolver uses to
    find segments, so the check and the resolution cannot drift apart.
    """
    with pytest.raises(ValidationError, match="span no edge"):
        domain(BoundarySpec(name="root", select=PointsSelector(ids=["a", "c"])))

    # One vertex spans nothing either, and the model says so before the geometry has to.
    with pytest.raises(ValidationError, match="at least 2 items"):
        PointsSelector(ids=["a"])

    # The rule reaches inside an `all_of`, where it would otherwise be easy to hide.
    with pytest.raises(ValidationError, match="span no edge"):
        domain(
            BoundarySpec(
                name="corner",
                select=AllOfSelector(
                    of=[PointsSelector(ids=["a", "c"]), PartSelector(of="outer")]
                ),
            )
        )


def test_the_validation_and_the_resolver_agree_on_what_adjacent_means():
    """Both go through `spanned_edges`, so "it validated" and "it selects something" are the
    same statement rather than two that happen to coincide."""
    geometry = domain(BoundarySpec(name="root", select=PointsSelector(ids=["a", "b"])))
    assert spanned_edges(SQUARE, ["a", "b"]) == [(0, 1)]
    assert spanned_edges(SQUARE, ["a", "c"]) == []
    assert hits(geometry, "root", (0.5, 0.0)) == [True]


def test_point_ids_must_be_one_per_point_and_distinct():
    """An id list out of step with the points is worse than none: a selector would resolve to
    the wrong vertex rather than failing."""
    with pytest.raises(ValidationError, match="exactly one per point"):
        Polygon2D(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], point_ids=["a", "b"])
    with pytest.raises(ValidationError, match="must be unique"):
        Polygon2D(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], point_ids=["a", "b", "b"])


def test_boundary_names_are_unique_within_one_geometry():
    with pytest.raises(ValidationError, match="boundary names must be unique"):
        domain(
            BoundarySpec(name="root", select=PartSelector(of="outer")),
            BoundarySpec(name="root", select=PartSelector(of="obstacle")),
        )


def test_naming_nothing_is_the_default_and_changes_nothing():
    """Every adapter shipped today puts its conditions where the outer/obstacle split implies
    them. An empty list has to stay the default, or #85 would be a breaking change wearing an
    additive one's clothes."""
    assert Domain2D(bounds=(-2.0, -2.0, 3.0, 3.0), obstacle=SQUARE).boundaries == []
    assert Polygon2D(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]).point_ids is None


def test_an_unknown_boundary_name_lists_what_the_geometry_does_declare():
    """The message a caller acts on: what was asked for, and what is on offer."""
    geometry = domain(BoundarySpec(name="root", select=PartSelector(of="outer")))
    with pytest.raises(KeyError, match=r"declares no boundary named 'tip'.*\['root'\]"):
        predicate(geometry, "tip")
