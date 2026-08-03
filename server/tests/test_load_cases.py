"""What happens on a named boundary (#85, second half).

The geometry half — stable point ids, the three selector families, the resolver — is in
`test_boundaries.py`. This is the other half: a `load_case` object saying *what* happens
there, the refusals that keep it from being applied to nothing, and the two elasticity
adapters actually solving with it.

Three tests carry the weight, and they are the three things #85 listed under acceptance:

- :func:`test_one_load_case_solves_two_different_shapes` — the reuse that justified making
  this an object rather than a block in the design.
- :func:`test_a_load_case_on_named_boundaries_reproduces_the_edge_shorthand` — the new route
  and the old one agree on a case both can express, which is what makes "the parameters stay
  as the shorthand" a true statement rather than a hopeful one.
- :func:`test_a_load_case_naming_an_unknown_boundary_is_refused_with_both_halves` — the
  refusal, which is the whole reason the boundary is *named* rather than guessed.

Everything else is either a physical claim the load case makes possible for the first time
(a roller, a clamped hole, part of an edge) or an invariant that keeps the machinery honest.
"""

import asyncio
import tempfile
from pathlib import Path

import numpy as np
import pytest

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.conditions import LoadCaseBody, merge_load_cases
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import (
    AllOfSelector,
    BoundarySpec,
    BoxSelector,
    NearSelector,
    PartSelector,
    Polygon2D,
    Region2D,
    Regions2D,
)
from fenixspoon.jobs import JobManager
from fenixspoon.solvers import fill_declared_metrics
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_elasticity import MockElasticity2D

E = 2.1e11
NU = 0.3
LENGTH = 1.0
DEPTH = 0.1
TRACTION = 1.0e6


def rectangle(xmin, ymin, xmax, ymax) -> Polygon2D:
    return Polygon2D(points=[(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])


def named_beam(*boundaries: BoundarySpec) -> Regions2D:
    """A uniform steel beam whose boundaries have names.

    One material everywhere, so every closed form below applies without a caveat about which
    region a stress came from. `regions2d` rather than `domain2d` because a `domain2d`
    obstacle is a *hole*, and a beam with a hole in it has no closed form.
    """
    return Regions2D(
        bounds=(0.0, -DEPTH / 2, LENGTH, DEPTH / 2),
        regions=[
            Region2D(
                name="body",
                shape=rectangle(0.45, -0.04, 0.55, 0.04),
                material={"youngs_modulus": E, "poisson_ratio": NU},
            )
        ],
        background={"youngs_modulus": E, "poisson_ratio": NU},
        boundaries=list(boundaries),
    )


def near(name: str, axis: str, value: float) -> BoundarySpec:
    """A boundary at one coordinate — the dolfinx idiom, as data.

    The tolerance is generous relative to a mesh spacing and tiny relative to the beam, which
    is the range where "on this edge" is unambiguous.
    """
    return BoundarySpec(name=name, select=NearSelector(axis=axis, value=value, tol=1e-9))


#: The cantilever of `test_elasticity.py`, said the other way round: the root and the tip are
#: named rather than implied by `fixed_edge` / `load_edge`.
CANTILEVER = named_beam(near("root", "x", 0.0), near("tip", "x", LENGTH))

FAST = {"resolution": 64, "iterations": 3000, "write_vtk": False}

#: A square plate bonded to a stiff insert, with the insert's outline named. The plate is a
#: unit square and the insert lands on tenths of it, so at `resolution=101` every node of the
#: insert outline is a grid node — which is what lets a `part` selector find it exactly
#: rather than to within half a cell.
BONDED_PLATE = Regions2D(
    bounds=(0.0, 0.0, 1.0, 1.0),
    regions=[
        Region2D(
            name="insert",
            shape=rectangle(0.4, 0.4, 0.6, 0.6),
            material={"youngs_modulus": E, "poisson_ratio": NU},
        )
    ],
    background={"youngs_modulus": E, "poisson_ratio": NU},
    boundaries=[
        BoundarySpec(name="insert", select=PartSelector(of="region:insert")),
        near("right", "x", 1.0),
    ],
)


def solve(geometry, conditions=None, **params):
    """Solve directly with a load case, and fill the declared metrics as a caller receives them."""
    ctx = SolverContext(
        progress_cb=lambda event: None,
        artifact_dir=Path(tempfile.mkdtemp()),
        conditions=conditions,
    )
    result = MockElasticity2D().solve(
        geometry, MockElasticity2D.Params(**{**FAST, **params}), ctx
    )
    fill_declared_metrics(MockElasticity2D, result)
    return result


def tip_displacement(result) -> float:
    """The largest displacement magnitude, which for a cantilever is at the tip."""
    return max(result.data["point_fields"]["u_mag"])


# ------------------------------------------------------------- the load case as an object


def test_a_load_case_body_is_validated_rather_than_stored_as_opaque_json(core, me):
    """`load_case` left the thin list, and this is what that bought.

    Before #85 the body was whatever the caller put there — which was honest while nothing
    read it, and became a liability the moment something did. A body whose conditions are a
    string instead of a map would otherwise be stored happily and fail inside an adapter.
    """
    good = core.create_object("load_case", {"conditions": {"root": {"fixed": 1}}}, me)
    assert good.body == {"conditions": {"root": {"fixed": 1}}}

    with pytest.raises(errors.InvalidObject):
        core.create_object("load_case", {"conditions": {"root": "clamped"}}, me)
    with pytest.raises(errors.InvalidObject):
        core.create_object("load_case", {"restraints": {"root": {"fixed": 1}}}, me)


def test_merging_two_load_cases_keeps_them_apart_until_they_disagree():
    """The reuse case and the refusal, in one place.

    A design naming a restraint set and a load set separately is the composition an engineer
    actually wants — clamp the same way, load three different ways. Two cases setting the
    *same* key on the same boundary is not composition, and resolving it by list order would
    make the answer depend on something that carries no intent.
    """
    restraints = LoadCaseBody(conditions={"root": {"fixed": 1}})
    pull = LoadCaseBody(conditions={"tip": {"traction_x": TRACTION}})
    assert merge_load_cases([("load_case:lc-1@1", restraints), ("load_case:lc-2@1", pull)]) == {
        "root": {"fixed": 1.0},
        "tip": {"traction_x": TRACTION},
    }

    # Different keys on one boundary still merge: "clamped in x" and "loaded in y" are
    # compatible statements about the same edge.
    also = LoadCaseBody(conditions={"root": {"traction_y": 1.0}})
    assert merge_load_cases([("a", restraints), ("b", also)])["root"] == {
        "fixed": 1.0,
        "traction_y": 1.0,
    }

    with pytest.raises(errors.ConflictingConditions) as caught:
        merge_load_cases([("load_case:lc-1@1", restraints), ("load_case:lc-3@1", restraints)])
    assert caught.value.boundary == "root" and caught.value.key == "fixed"
    assert caught.value.sources == ["load_case:lc-1@1", "load_case:lc-3@1"]


# ------------------------------------------------------------------------- the refusals


def test_a_load_case_naming_an_unknown_boundary_is_refused_with_both_halves(core, me):
    """#85's named refusal, and the one the whole design turns on.

    A condition applied to nothing does not fail — it produces a solve that runs, converges,
    and answers a different problem. A cantilever that was never clamped comes back as a
    rigid-body motion; a plate that was never loaded comes back at zero stress. Both look
    like results, which is why this has to be a 422 at submit rather than a warning later.

    The message carries both halves because that is what makes it actionable in one round
    trip: what was asked for, and what the geometry does declare.
    """
    with pytest.raises(errors.UnknownBoundary) as caught:
        submit(core, me, CANTILEVER, {"foot": {"fixed": 1}})
    assert caught.value.boundary == "foot"
    assert caught.value.declared == ["root", "tip"]
    assert "foot" in caught.value.detail and "root" in caught.value.detail


def test_a_mistyped_condition_key_is_refused_rather_than_ignored(core, me):
    """The check that pays for `ConditionSpec` existing at all.

    Load-case values are an open map of scalars on purpose — a typed enum of condition kinds
    would put physics into the protocol. The price of that openness is that `tracton_y` is a
    perfectly valid key, and without this it would produce an unloaded structure reporting
    zero stress. `Region2D.material` still has that hole; a load case does not.
    """
    with pytest.raises(errors.UnknownConditionKey) as caught:
        submit(core, me, CANTILEVER, {"tip": {"tracton_y": -TRACTION}})
    assert caught.value.key == "tracton_y"
    assert "traction_y" in caught.value.accepted
    assert "traction_y" in caught.value.detail


def test_a_capability_that_reads_no_conditions_refuses_a_load_case(core, me):
    """An empty `conditions` declaration is enforced, not merely reported.

    `mock.heat2d` expresses its convective boundary as `h` and `t_ambient` params and reads
    no load case at all. Accepting one and ignoring it would be the same silent failure as an
    unknown key, one level up — so the refusal names the capability rather than the key.
    """
    with pytest.raises(errors.UnknownConditionKey) as caught:
        submit(
            core,
            me,
            BONDED_PLATE,
            {"insert": {"fixed": 1}},
            solver="mock.heat2d",
            params={"resolution": 32, "iterations": 20, "write_vtk": False},
        )
    assert caught.value.accepted == []
    assert "no boundary-condition keys at all" in caught.value.detail


def test_two_loaded_boundaries_cannot_claim_the_same_stretch(core, me):
    """Found by review of #85, and it is the pair disagreeing that made it visible.

    Selectors are not required to be disjoint — `near` and `part` will happily both find a
    corner — so a caller reaches this by accident rather than by trying. The FEniCSx adapter
    already refused an overlap; the mock silently *summed*, so the shared stretch carried a
    load that was the sum of two the caller never asked to add, with nothing saying so.

    Refused rather than summed, because superposition is a real thing to want and this is not
    it: the intent "`traction_x` on this stretch" has one value, so two is a contradiction
    rather than a total. Both adapters now refuse in the same words, from one constant.
    """
    overlapping = named_beam(
        near("root", "x", 0.0),
        near("tip", "x", LENGTH),
        # The whole outer boundary, which includes the tip. Legal, and a natural thing to
        # write for "pressurise the outside" — right up until the tip is also loaded.
        BoundarySpec(name="skin", select=PartSelector(of="outer")),
    )
    with pytest.raises(ValueError, match="cannot carry two tractions"):
        solve(
            overlapping,
            {
                "root": {"fixed": 1},
                "tip": {"traction_y": -TRACTION},
                "skin": {"traction_x": TRACTION},
            },
        )

    # Restraints are exempt, and deliberately: they do not accumulate. Clamping a node
    # through two overlapping boundaries clamps it once, so refusing that would reject a
    # caller who named the same edge twice for readability. `foot` is a box over the root
    # end, so it overlaps `root` node for node — and the answer is the plain cantilever.
    doubled = named_beam(
        near("root", "x", 0.0),
        near("tip", "x", LENGTH),
        BoundarySpec(name="foot", select=BoxSelector(bounds=(-0.01, -0.06, 0.01, 0.06))),
    )
    load = {"tip": {"traction_y": -TRACTION}}
    both = solve(doubled, {"root": {"fixed": 1}, "foot": {"fixed_y": 1}, **load})
    once = solve(doubled, {"root": {"fixed": 1}, **load})
    assert tip_displacement(both) == pytest.approx(tip_displacement(once), rel=1e-12)


def test_a_load_case_that_is_not_one_is_refused_by_shape_before_anything_else(core, me):
    """The payload has to *be* a load case before "does this geometry declare `root`" is a
    question, and two doors reach the check with an unvalidated map: the in-process Python
    API, and the worker unpacking a queue message.

    Without this, `{"root": 5}` reaches an adapter as an integer and raises a `TypeError`
    from somewhere deep in NumPy — a server-shaped failure for a caller-shaped mistake.
    Raised in review of #85; `InvalidObject` because the thing that failed is a load-case
    body whether it arrived inline or as an object, and it already carries pydantic's
    structured list to every transport.
    """
    for malformed in ({"root": 5}, {"root": {"fixed": "clamped"}}, {"root": ["fixed"]}):
        with pytest.raises(errors.InvalidObject) as caught:
            submit(core, me, CANTILEVER, malformed)
        assert caught.value.object_type == "load_case"

    # A string that *is* a number still parses, because pydantic coerces it — and what the
    # solve and the cache key see is the scalar, not the string.
    coerced = submit(core, me, CANTILEVER, {"root": {"fixed": "1"}, "tip": {"traction_y": -1e6}})
    assert coerced.status == "done", coerced.error


def test_a_boundary_that_selects_no_node_of_the_mesh_fails_the_solve():
    """The same failure one level down, where a name check cannot reach it.

    The boundary exists, the selector is well formed, and the mesh is simply too coarse to
    have a node on it. Refused inside the adapter rather than tolerated, because a condition
    that silently contributes nothing is exactly what everything above is arranged to
    prevent — and a caller can act on it: raise the resolution, or widen the selector.
    """
    sliver = named_beam(
        near("root", "x", 0.0),
        near("tip", "x", LENGTH),
        BoundarySpec(
            name="nowhere", select=BoxSelector(bounds=(0.4999, 0.0499, 0.5001, 0.0501))
        ),
    )
    with pytest.raises(ValueError, match="selects no node"):
        solve(sliver, {"root": {"fixed": 1}, "nowhere": {"traction_y": -TRACTION}})


# ---------------------------------------------------------------- the physics it enables


def test_a_load_case_on_named_boundaries_reproduces_the_edge_shorthand():
    """The two routes agree where both can speak, which is what keeps the shorthand honest.

    #85 kept `fixed_edge` / `load_edge` rather than deleting them, on the grounds that a
    caller with a bare rectangle should not have to author named boundaries to clamp one end.
    That is only a defensible position if the two produce the *same* answer on a case both
    express — otherwise the shorthand is a second, subtly different physics.

    Bit-for-bit is the right expectation here, not an approximation: the same nodes are
    clamped, the same segments are loaded with the same consistent nodal forces, and the same
    CG runs on the result. A difference would mean the two paths disagree about the mesh.
    """
    shorthand = solve(
        CANTILEVER, fixed_edge="xmin", load_edge="xmax", traction=(0.0, -TRACTION)
    )
    named = solve(CANTILEVER, {"root": {"fixed": 1}, "tip": {"traction_y": -TRACTION}})

    assert tip_displacement(named) == pytest.approx(tip_displacement(shorthand), rel=1e-12)
    assert named.metrics["sigma_vm_max"] == pytest.approx(
        shorthand.metrics["sigma_vm_max"], rel=1e-12
    )
    assert named.metrics["compliance"] == pytest.approx(
        shorthand.metrics["compliance"], rel=1e-12
    )


def test_a_roller_lets_the_plate_contract_where_a_clamp_would_not():
    """Why `fixed_x` is a separate key from `fixed`, stated as the error it removes.

    Uniform tension is the one state constant-strain triangles reproduce exactly — but only
    if the restraints permit it. Clamping the left edge in *both* directions forbids the
    Poisson contraction there, and the field is no longer uniform: `test_elasticity.py` had
    to measure `sigma_vm_max` away from the clamp for exactly this reason.

    Holding only the normal component is what a symmetry plane actually is, and with the
    remaining rigid-body translation pinned at a single node the discrete answer becomes
    exactly uniform. So this asserts the closed form over the *whole* field, including at the
    constraint — which the full clamp cannot do, as the second half checks.
    """
    corner = BoundarySpec(
        name="anchor", select=BoxSelector(bounds=(-1e-9, -DEPTH / 2 - 1e-9, 1e-9, -0.049))
    )
    plate = named_beam(near("left", "x", 0.0), near("right", "x", LENGTH), corner)

    rolled = solve(
        plate,
        {
            "left": {"fixed_x": 1},
            "anchor": {"fixed_y": 1},
            "right": {"traction_x": TRACTION},
        },
    )
    stress = np.asarray(rolled.data["point_fields"]["sigma_vm"], dtype=float)
    assert stress.max() == pytest.approx(TRACTION, rel=1e-6)
    assert stress.min() == pytest.approx(TRACTION, rel=1e-6)

    clamped = solve(plate, {"left": {"fixed": 1}, "right": {"traction_x": TRACTION}})
    # The same load, the same mesh, a restraint that forbids the contraction: the peak sits
    # well above the far field, at the corners of the clamp. Saint-Venant, not error.
    assert clamped.metrics["sigma_vm_max"] > 1.05 * TRACTION


def test_an_interior_boundary_can_carry_a_condition_at_last():
    """One of the two limits `edge_aligned_boundary_conditions` used to exclude.

    Before #85 a restraint had to be a side of the bounding rectangle, so a plate bonded to a
    rigid insert — or hung from a pin — could not be expressed at all. Here the insert's
    outline is named through the topological selector the geometry already had, and it is the
    thing holding the plate up.

    Asserted where it is decisive: the displacement is zero on the insert and not on the
    loaded edge. That is the definition of "the condition landed where it was put".

    An **interior** boundary rather than the rim of a `domain2d` hole, and the difference is
    the mock rather than the design. A `domain2d` obstacle is masked out of a Cartesian grid,
    so the surviving nodes sit a fraction of a cell *outside* the polygon and none of them
    lies on it — the staircasing this adapter's docstring warns about. The FEniCSx twin
    meshes the hole properly and is held to the hole case in `test_elasticity.py`; here the
    region outline is grid-aligned by construction, so the selector has exact nodes to find.
    """
    result = solve(
        BONDED_PLATE,
        {"insert": {"fixed": 1}, "right": {"traction_x": TRACTION}},
        resolution=101,
    )

    points = np.asarray(result.data["points"], dtype=float)
    magnitude = np.asarray(result.data["point_fields"]["u_mag"], dtype=float)
    on_the_insert = (
        (np.abs(points[:, 0] - 0.4) < 1e-9) | (np.abs(points[:, 0] - 0.6) < 1e-9)
    ) & (points[:, 1] >= 0.4 - 1e-9) & (points[:, 1] <= 0.6 + 1e-9)
    assert on_the_insert.sum() > 4, "the selector must have real nodes to find"
    assert magnitude[on_the_insert].max() == pytest.approx(0.0, abs=1e-18)
    assert magnitude[points[:, 0] > 0.99].min() > 0.0


def test_part_of_an_edge_can_be_loaded_and_carries_part_of_the_load():
    """The other excluded limit, and the one that needs a quantitative check.

    `all_of` intersects the tip with a box over its lower half, which is the composition #85
    chose instead of an expression language. A transverse tip deflection depends on the
    *resultant* of the end load to Euler-Bernoulli order, so halving the loaded length at the
    same traction should halve the deflection.

    The tolerance is loose because the split is quantised: only segments with **both**
    endpoints inside the box are loaded, so a structured grid whose nodes do not straddle
    y = 0 neatly loads slightly less than half the edge. That is a property of the mesh, not
    a modelling choice, and tightening the tolerance would only make this test a thermometer
    for the grid spacing.
    """
    partial = named_beam(
        near("root", "x", 0.0),
        BoundarySpec(
            name="lower_tip",
            select=AllOfSelector(
                of=[
                    NearSelector(axis="x", value=LENGTH, tol=1e-9),
                    BoxSelector(bounds=(LENGTH - 0.1, -DEPTH / 2 - 0.1, LENGTH + 0.1, 0.0)),
                ]
            ),
        ),
    )
    half = solve(partial, {"root": {"fixed": 1}, "lower_tip": {"traction_y": -TRACTION}})
    whole = solve(CANTILEVER, {"root": {"fixed": 1}, "tip": {"traction_y": -TRACTION}})

    ratio = tip_displacement(half) / tip_displacement(whole)
    assert 0.3 < ratio < 0.7, f"a half-loaded tip should carry about half the load, got {ratio}"


# ------------------------------------------------------- the design and the cache (#44, #47)


def test_one_load_case_solves_two_different_shapes(core, me):
    """#85's acceptance criterion, and the argument for a separate object type.

    One load case, two geometries that both declare `root` and `tip`, two designs, two
    solves. If the conditions lived inside the design this would be two copies that drift;
    inside the geometry it would be worse, because changing the load would mint a new shape.

    The two beams differ in depth, so agreeing would be the suspicious outcome: the same
    restraints and the same traction on a stiffer section must deflect less, and asserting
    *that* is what shows the load case reached both solves rather than neither.
    """
    load_case = core.create_object(
        "load_case",
        {"conditions": {"root": {"fixed": 1}, "tip": {"traction_y": -TRACTION}}},
        me,
        label="end shear",
    ).ref

    deflections = []
    for depth in (DEPTH, 2 * DEPTH):
        geometry = Regions2D(
            bounds=(0.0, -depth / 2, LENGTH, depth / 2),
            regions=[
                Region2D(
                    name="body",
                    shape=rectangle(0.45, -depth / 2 + 0.01, 0.55, depth / 2 - 0.01),
                    material={"youngs_modulus": E, "poisson_ratio": NU},
                )
            ],
            background={"youngs_modulus": E, "poisson_ratio": NU},
            boundaries=[near("root", "x", 0.0), near("tip", "x", LENGTH)],
        )
        geometry_ref = core.create_object(
            "geometry", geometry.model_dump(mode="json"), me
        ).ref
        design = core.create_object(
            "design",
            {
                "solver": "mock.elasticity2d",
                "geometry": geometry_ref,
                "params": FAST,
                "load_cases": [load_case],
            },
            me,
        ).ref
        job = solve_design(core, design, me)
        assert job.status == "done", job.error
        deflections.append(core.result_levels(job.id, me).metrics["u_max"])

    # Bending stiffness goes as the cube of the depth while the total end force only doubles
    # with the loaded edge, so Euler-Bernoulli puts the deeper beam at a quarter of the
    # deflection. The measured ratio is 3.7 rather than 4 — a deeper beam is not slender, so
    # shear deformation is a larger share of the answer — which is why this is a band rather
    # than a number. A load case that reached only one of the two solves would land far
    # outside it: equal deflections, or one of them zero.
    assert 3.0 < deflections[0] / deflections[1] < 4.5


def test_two_load_cases_on_one_shape_are_two_cache_entries(core, me):
    """The load case is part of the solve's identity, not metadata beside it.

    This is the one place where getting #85 wrong would be actively dangerous rather than
    merely wrong: the content-addressed cache (#47) hashes the geometry, the params and the
    environment, and if the conditions were left out then pulling a beam and shearing it
    would be the same key. The second caller would be served the first one's field —
    instantly, with `cached: true`, and about a different problem.
    """
    geometry_ref = core.create_object(
        "geometry", CANTILEVER.model_dump(mode="json"), me
    ).ref

    jobs = []
    for traction in ({"traction_y": -TRACTION}, {"traction_x": TRACTION}):
        load_case = core.create_object(
            "load_case", {"conditions": {"root": {"fixed": 1}, "tip": traction}}, me
        ).ref
        design = core.create_object(
            "design",
            {
                "solver": "mock.elasticity2d",
                "geometry": geometry_ref,
                "params": FAST,
                "load_cases": [load_case],
            },
            me,
        ).ref
        jobs.append(solve_design(core, design, me))

    shear, tension = jobs
    assert shear.id != tension.id
    assert shear.reused == 0 and tension.reused == 0
    # Bending a slender beam is far softer than stretching it, so the two answers are not
    # merely different — they are different by orders of magnitude.
    assert (
        core.result_levels(shear.id, me).metrics["u_max"]
        > 100 * core.result_levels(tension.id, me).metrics["u_max"]
    )

    # And the same design solved twice *is* one entry: the conditions enter the key, they do
    # not defeat it.
    again = solve_design(core, jobs[0].inputs["design"].split("@")[0], me)
    assert again.id == shear.id


def test_a_design_records_the_load_case_revisions_it_resolved(core, me):
    """The `design -> job -> result` relation has to include the load case, or the question
    "what exactly was solved" has an answer with a hole in it.

    The pinned reference goes into `inputs` like every other object; the *merged conditions*
    deliberately do not, because `inputs` is what `jobs_for_object` searches for references
    and a nested map of scalars is not one.
    """
    geometry_ref = core.create_object(
        "geometry", CANTILEVER.model_dump(mode="json"), me
    ).ref
    load_case = core.create_object(
        "load_case",
        {"conditions": {"root": {"fixed": 1}, "tip": {"traction_y": -TRACTION}}},
        me,
    ).ref
    design = core.create_object(
        "design",
        {
            "solver": "mock.elasticity2d",
            "geometry": geometry_ref,
            "params": FAST,
            "load_cases": [load_case],
        },
        me,
    ).ref

    _, resolved = core.workspace.resolve_design(design, me.id)
    assert resolved.load_cases == [f"{load_case}@1"]
    assert resolved.conditions == {
        "root": {"fixed": 1.0},
        "tip": {"traction_y": -TRACTION},
    }

    job = solve_design(core, design, me)
    assert job.inputs["load_cases"] == [f"{load_case}@1"]
    assert "conditions" not in job.inputs
    assert [found.id for found in core.jobs_for_object(load_case, me)] == [job.id]


# ------------------------------------------------------------------------- the transports


def test_an_inline_load_case_and_a_design_reach_the_solver_identically(core, me):
    """A load case is a workspace object, and an inline one is still a first-class input.

    The same argument that gave `job.submit` an inline geometry beside a design reference:
    the workspace is the reproducible route, and a caller solving one thing once should not
    have to author three objects first. Both arrive at `FenixSpoonCore.submit`, so both are
    checked by the same code and both enter the same cache key — which this asserts by
    requiring the inline submission to *hit* the design's entry.
    """
    conditions = {"root": {"fixed": 1.0}, "tip": {"traction_y": -TRACTION}}
    geometry_ref = core.create_object(
        "geometry", CANTILEVER.model_dump(mode="json"), me
    ).ref
    load_case = core.create_object("load_case", {"conditions": conditions}, me).ref
    design = core.create_object(
        "design",
        {
            "solver": "mock.elasticity2d",
            "geometry": geometry_ref,
            "params": FAST,
            "load_cases": [load_case],
        },
        me,
    ).ref

    from_design = solve_design(core, design, me)
    inline = submit(core, me, CANTILEVER, conditions)
    assert inline.id == from_design.id, "the inline form must address the same solve"
    assert inline.reused == 1


def test_the_capability_declares_the_keys_a_load_case_may_use(core, me):
    """A caller authoring a load case has to be able to ask what goes in it.

    Declared alongside the metrics and the assumptions, and returned as its own section so a
    form can be built from it without pulling the params schema too. The empty list is a real
    answer here rather than an omission — it is what makes "this capability takes no load
    case" discoverable before a submission rather than through a 422.
    """
    described = core.capability_describe("mock.elasticity2d", ["conditions"]).model_dump(
        exclude_none=True
    )
    assert set(described) == {"name", "conditions"}
    by_name = {entry["name"]: entry for entry in described["conditions"]}
    assert set(by_name) == {"fixed", "fixed_x", "fixed_y", "traction_x", "traction_y"}
    assert by_name["traction_y"]["unit"] == "Pa"
    assert by_name["traction_y"]["kind"] == "neumann"
    assert by_name["fixed"]["kind"] == "dirichlet"

    assert core.capability_describe("mock.heat2d", ["conditions"]).conditions == []


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "workspace"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def submit(core, me, geometry, conditions, solver="mock.elasticity2d", params=None):
    """One inline submission, waited out on the loop that started it."""

    async def run():
        job = await core.submit(
            solver, geometry, params or FAST, me, conditions=conditions
        )
        deadline = asyncio.get_running_loop().time() + 60
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        finished = core.job(job.id, me)
        finished.reused = job.reused
        return finished

    return asyncio.run(run())


def solve_design(core, design_ref, me):
    """Submit a design and wait for it, in one event loop (see `test_workspace.py`)."""

    async def run():
        job = await core.submit_design(design_ref, me)
        deadline = asyncio.get_running_loop().time() + 60
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return core.job(job.id, me)

    return asyncio.run(run())
