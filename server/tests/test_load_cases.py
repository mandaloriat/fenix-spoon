"""What happens on a named boundary (#85, second half).

#85 split along the seam its design discussion found: *where* a boundary is belongs to the
shape, and *what happens there* does not. `test_boundaries.py` is the where. This is the what
— the `load_case` object, the conditions reaching a solve, and the four refusals that keep an
unread condition from becoming a quietly different problem.

Two tests carry the design and the rest is machinery around them:

- :func:`test_one_load_case_serves_two_shapes` is the argument for a load case being its own
  object rather than a block inside a design. If it did not hold, there would be no reason not
  to embed the conditions in the design and delete a type.
- :func:`test_an_id_selected_clamp_follows_the_shape_through_an_edit` is the argument for
  stable point ids, now measured on a *solve* rather than on a predicate: the same load case,
  the same boundary name, a geometry edit in between, and the clamp still on the same physical
  edge. Its counterpart in `test_boundaries.py` proves the predicate moves correctly; this
  proves the numbers do.
"""

import asyncio

import numpy as np
import pytest

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.jobs import JobManager

#: Resolution and the hole coordinates are chosen together, and that is worth stating because
#: it looks arbitrary. `mock.elasticity2d` meshes a **raster**: it keeps the grid nodes that
#: are not strictly inside the obstacle, so the mesh conforms to the hole only where the hole
#: lands on grid lines. At 41 points over a unit square the spacing is 0.025 and the corners
#: below are exact multiples of it, which puts 21 mesh nodes precisely on each hole edge — so
#: a `points` selector resolves with **zero** tolerance rather than with a heuristic band.
#:
#: An unaligned hole would put no node on the outline at all, and the solve would fail saying
#: so. That is the mock's discretisation showing through, not a gap in #85: Gmsh meshes the
#: polygon itself, so `dolfinx.elasticity2d` has no such constraint. It is declared as an
#: assumption on the mock rather than papered over with a tolerance that would quietly load
#: a band of nodes near the boundary instead of the boundary.
RESOLUTION = 41
_STEP = 1.0 / (RESOLUTION - 1)


def plate(hole_lo: float = 0.25, hole_hi: float = 0.75) -> dict:
    """A unit square with a large square hole, clamped at the left, hole edges named by id."""
    assert abs(hole_lo / _STEP - round(hole_lo / _STEP)) < 1e-12, "hole must land on grid lines"
    return {
        "type": "domain2d",
        "bounds": [0.0, 0.0, 1.0, 1.0],
        "obstacle": {
            "type": "polygon2d",
            "points": [
                [hole_lo, hole_lo],
                [hole_hi, hole_lo],
                [hole_hi, hole_hi],
                [hole_lo, hole_hi],
            ],
            "point_ids": ["p0", "p1", "p2", "p3"],
        },
        "boundaries": [
            {"name": "root", "select": {"type": "near", "axis": "x", "value": 0.0, "tol": 1e-9}},
            {"name": "tip", "select": {"type": "near", "axis": "x", "value": 1.0, "tol": 1e-9}},
            # The edge between p2 and p3: the top of the hole. The one boundary here that
            # follows the *shape* rather than the space, and the only one an edit can move.
            {"name": "hole_top", "select": {"type": "points", "ids": ["p2", "p3"]}},
        ],
    }


CANTILEVER = {"root": {"fixed": 1.0}, "tip": {"traction_y": -1.0e6}}

FAST = {"resolution": RESOLUTION, "iterations": 20000, "write_vtk": False}


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "workspace"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def solve(core, me, **kwargs):
    """Submit and wait, in one event loop.

    Both halves must share a loop: the in-process backend finishes a job through a callback
    scheduled on the loop that submitted it, so waiting inside a second `asyncio.run` watches
    a status that can never move.
    """

    async def run():
        job = (
            await core.submit_design(kwargs["design"], me)
            if "design" in kwargs
            else await core.submit(
                kwargs.get("solver", "mock.elasticity2d"),
                _validated(kwargs["geometry"]),
                kwargs.get("params", FAST),
                me,
                conditions=kwargs.get("conditions"),
            )
        )
        deadline = asyncio.get_running_loop().time() + 90
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return core.job(job.id, me, with_result=True)

    return asyncio.run(run())


def _validated(raw):
    from pydantic import TypeAdapter

    from fenixspoon.geometry import Geometry

    return TypeAdapter(Geometry).validate_python(raw)


def tip_deflection(job) -> float:
    """Downward displacement at the loaded end, from the vector field the result carries."""
    data = job.result.data
    points = np.asarray(data["points"], dtype=float)
    displacement = np.asarray(data["point_vector_fields"]["u"], dtype=float)
    at_tip = points[:, 0] > points[:, 0].max() - 1e-9
    return float(displacement[at_tip, 1].min())


def make(core, me, geometry, conditions, solver="mock.elasticity2d", params=None):
    """Geometry, load case and design as three objects, returning the design reference."""
    geometry_ref = core.create_object("geometry", geometry, me).ref
    load_case = core.create_object("load_case", {"conditions": conditions}, me).ref
    design = core.create_object(
        "design",
        {
            "solver": solver,
            "geometry": geometry_ref,
            "params": params or FAST,
            "load_cases": [load_case],
        },
        me,
    )
    return geometry_ref, load_case, design.ref


# ------------------------------------------------------------------ the two that matter


def test_one_load_case_serves_two_shapes(core, me):
    """#85's acceptance criterion: one load case, two geometries, two designs, two solves.

    This is the whole argument for `load_case` being its own object type. If a load case were
    a block inside a design, "clamped at the root, pulled down at the tip" would have to be
    written once per shape, and changing it would mean editing every design that shares it.
    """
    load_case = core.create_object("load_case", {"conditions": CANTILEVER}, me).ref

    designs = []
    for hole_lo in (0.20, 0.30):
        geometry_ref = core.create_object("geometry", plate(hole_lo, hole_lo + 0.5), me).ref
        designs.append(
            core.create_object(
                "design",
                {
                    "solver": "mock.elasticity2d",
                    "geometry": geometry_ref,
                    "params": FAST,
                    "load_cases": [load_case],
                },
                me,
            ).ref
        )

    deflections = []
    for design in designs:
        job = solve(core, me, design=design)
        assert job.status == "done", job.error
        # The one load case is recorded as an input of both solves, so the relation is
        # queryable from either end — `jobs_for_object` on the load case finds them both.
        assert f"{load_case}@1" in job.inputs["load_cases"]
        deflections.append(tip_deflection(job))

    assert all(value < 0 for value in deflections), "the tip should deflect downward"
    # Two different shapes, so two different answers — otherwise the geometry reference is
    # not reaching the solve and this test would pass on a stub.
    assert deflections[0] != deflections[1]
    assert len(core.jobs_for_object(load_case, me)) == 2


def test_an_id_selected_clamp_follows_the_shape_through_an_edit(core, me):
    """The ids decision, measured on a solve rather than on a predicate.

    A boundary selected by point id is patched *around* — a vertex is inserted before the ones
    it names — and must still be the same physical edge afterwards. Against index-based
    selection this fails: inserting at index 1 shifts `p2` and `p3` down the list, and
    "the top of the hole" silently becomes one of its sides.

    Traction rather than a clamp, because a clamp that moved would still produce a plausible
    number, and the compliance under a known load is a single scalar that says where the load
    landed.

    The test carries **its own counterexample**. Asserting only that the answer survives the
    edit would pass against a selector that ignored the geometry entirely, so the last third
    re-runs the identical solve with the selector an index-based implementation would have
    produced — `["p1", "p2"]`, positions 2 and 3 of the patched outline — and requires it to
    disagree. That is the difference between "the ids work" and "the ids are load-bearing".
    """
    load = {"root": {"fixed": 1.0}, "hole_top": {"traction_y": 5.0e5}}
    geometry_ref, _, _ = make(core, me, plate(), load)

    before = solve(core, me, geometry=core.object(geometry_ref, me).body, conditions=load)
    assert before.status == "done", before.error

    # Insert a vertex on the *bottom* edge of the hole, between p0 and p1. Every index after it
    # shifts by one; the ids do not. Two operations in one patch, because a geometry whose
    # `points` and `point_ids` disagree in length does not validate — which is the model
    # refusing a half-applied edit rather than storing one.
    patched = core.patch_object(
        geometry_ref,
        [
            {"op": "add", "path": "/obstacle/points/1", "value": [0.5, 0.25]},
            {"op": "add", "path": "/obstacle/point_ids/1", "value": "p0b"},
        ],
        me,
    )
    assert patched.body["obstacle"]["point_ids"] == ["p0", "p0b", "p1", "p2", "p3"]

    after = solve(core, me, geometry=patched.body, conditions=load)
    assert after.status == "done", after.error

    # Bit-identical, not merely close. The inserted vertex lies on an edge the outline already
    # had, so the meshed body and the selected edge are the same body and the same edge — an
    # id-selected boundary is not approximately preserved through this edit, it is preserved.
    assert before.result.metrics["compliance"] > 0
    assert after.result.metrics["compliance"] == pytest.approx(
        before.result.metrics["compliance"], rel=1e-9
    )

    # And the counterexample: positions 2 and 3 of the patched outline are `p1` and `p2`, the
    # hole's right-hand side. This is what "the boundary named by indices [2, 3]" now means.
    by_index = patched.body
    by_index["boundaries"] = [
        entry if entry["name"] != "hole_top"
        else {"name": "hole_top", "select": {"type": "points", "ids": ["p1", "p2"]}}
        for entry in by_index["boundaries"]
    ]
    drifted = solve(core, me, geometry=by_index, conditions=load)
    assert drifted.status == "done", drifted.error
    assert drifted.result.metrics["compliance"] != pytest.approx(
        before.result.metrics["compliance"], rel=0.5
    ), "the index-based selector must land somewhere else, or this test proves nothing"


# ------------------------------------------------------------------ the refusals


def test_a_capability_that_reads_no_conditions_refuses_a_load_case(core, me):
    """The refusal the whole design turns on.

    `mock.laplace2d` would solve exactly the problem it solves without a load case: converged,
    plausible, and answering a question nobody asked. There is no symptom, so it has to be an
    error rather than a warning.
    """
    with pytest.raises(errors.CapabilityTakesNoConditions) as caught:
        solve(core, me, solver="mock.laplace2d", geometry=plate(),
              params={"resolution": 40}, conditions={"root": {"fixed": 1.0}})
    assert "mock.laplace2d" in str(caught.value)


def test_an_unknown_boundary_names_both_sides(core, me):
    """#85 asks for the message to name what was asked for *and* what the geometry declares.

    The usual cause is a load case written against a different shape, and seeing the two lists
    together diagnoses that instantly where "no such boundary" sends someone to read a file.
    """
    with pytest.raises(errors.UnknownBoundary) as caught:
        solve(core, me, geometry=plate(), conditions={"rooot": {"fixed": 1.0}})
    message = str(caught.value)
    assert "rooot" in message
    assert "root" in message and "tip" in message
    assert caught.value.declared == ["root", "tip", "hole_top"]


def test_a_geometry_that_names_no_boundaries_says_so(core, me):
    """The empty case gets its own sentence, because `it names []` is not advice."""
    bare = {k: v for k, v in plate().items() if k != "boundaries"}
    with pytest.raises(errors.UnknownBoundary) as caught:
        solve(core, me, geometry=bare, conditions={"root": {"fixed": 1.0}})
    assert "no boundaries at all" in str(caught.value)


def test_an_unread_condition_key_is_refused_where_a_material_key_would_be_ignored(core, me):
    """The asymmetry #85 asked for, stated as a test.

    An unread *material* key leaves a property at its default and the answer is merely
    computed with it. An unread *condition* key leaves a clamp or a load out of the assembly.
    """
    with pytest.raises(errors.UnknownConditionKey) as caught:
        solve(core, me, geometry=plate(), conditions={"root": {"clampd": 1.0}})
    assert "clampd" in str(caught.value)
    assert set(caught.value.declared) == {"fixed", "traction_x", "traction_y"}

    # The contrast, in the same test so the pair cannot drift: a region's material takes
    # whatever it is given, and always has.
    from fenixspoon.geometry import Region2D

    Region2D(name="steel", shape={"type": "polygon2d", "points": [(0, 0), (1, 0), (1, 1)]},
             material={"not_a_real_property": 3.0})


def test_a_load_case_cannot_be_sent_alongside_the_edge_shorthand(core, me):
    """Two ways of saying where the conditions go, and never both at once.

    Fails the solve rather than the submission, because which params are shorthand for what is
    knowledge only the adapter has — see `boundaries.governed_by_load_case`.
    """
    job = solve(core, me, geometry=plate(), conditions=CANTILEVER,
                params={**FAST, "load_edge": "ymax"})
    assert job.status == "failed"
    assert "load_edge" in job.error


def test_two_load_cases_claiming_one_boundary_are_refused(core, me):
    """Merged by name, never by list order.

    Last-wins would make which clamp applied a function of the order of a JSON array —
    reproducible, and impossible to notice.
    """
    geometry_ref = core.create_object("geometry", plate(), me).ref
    first = core.create_object("load_case", {"conditions": {"root": {"fixed": 1.0}}}, me).ref
    second = core.create_object(
        "load_case", {"conditions": {"root": {"traction_x": 1.0e5}}}, me
    ).ref
    design = core.create_object(
        "design",
        {"solver": "mock.elasticity2d", "geometry": geometry_ref, "params": FAST,
         "load_cases": [first, second]},
        me,
    ).ref

    with pytest.raises(errors.ConflictingLoadCases) as caught:
        core.resolve_design(design, me)
    assert caught.value.boundary == "root"

    # Disjoint boundaries are the case this is *not* refusing, and the reason a design takes
    # a list at all: restraints authored once, a load authored per study.
    third = core.create_object("load_case", {"conditions": {"tip": {"traction_y": -1.0}}}, me).ref
    ok = core.create_object(
        "design",
        {"solver": "mock.elasticity2d", "geometry": geometry_ref, "params": FAST,
         "load_cases": [first, third]},
        me,
    ).ref
    resolved = core.resolve_design(ok, me)
    assert set(resolved.conditions) == {"root", "tip"}


def test_a_boundary_listed_with_no_conditions_is_refused(core, me):
    """`{"root": {}}` reads like the root has been dealt with and applies nothing."""
    with pytest.raises(errors.InvalidObject):
        core.create_object("load_case", {"conditions": {"root": {}}}, me)


# ------------------------------------------------------------------ cache and discovery


def test_two_load_cases_on_one_shape_are_two_solves(core, me):
    """The cache key covers the load case, or the second answer is the first one's numbers.

    This is #48's `resolutoin` guard in the shape the cache could produce by itself: two
    designs that differ only in what is clamped, collapsing onto one job and reporting a
    perfect agreement between runs that were never the same run.
    """
    geometry = _validated(plate())
    solver = core.capability("mock.elasticity2d")
    pulled_down = core.cache_key_for(solver, geometry, solver.Params(), conditions=CANTILEVER)
    pulled_up = core.cache_key_for(
        solver, geometry, solver.Params(),
        conditions={"root": {"fixed": 1.0}, "tip": {"traction_y": 1.0e6}},
    )
    unloaded = core.cache_key_for(solver, geometry, solver.Params())

    assert pulled_down != pulled_up != unloaded
    assert pulled_down == core.cache_key_for(
        solver, geometry, solver.Params(), conditions=dict(CANTILEVER)
    )


def test_a_solve_without_a_load_case_keeps_the_key_it_had(core, me):
    """An absent load case must hash identically to an empty one.

    Not a detail: a stored cache written before #85 has keys computed without the field, and
    adding `"conditions": {}` to the payload would change every one of them — an entire cache
    missing for a change to inputs that never happened.
    """
    from fenixspoon import cache

    parts = dict(
        solver="mock.laplace2d", solver_version="1", geometry={"type": "domain2d"},
        params={"resolution": 64}, environment={"numpy": "2.0"},
    )
    assert cache.cache_key(**parts) == cache.cache_key(**parts, conditions={})
    assert cache.cache_key(**parts) != cache.cache_key(**parts, conditions=CANTILEVER)


def test_the_conditions_section_distinguishes_none_from_undeclared(core, me):
    """An empty list here is a rule the server enforces, not a gap in a declaration.

    Every other `capability.describe` section reports something about the *answer*, where an
    empty list means "not said". This one reports an *input*, and `[]` means "send me no load
    case, I will refuse it" — which a caller has to be able to act on.
    """
    elastic = core.capability_describe("mock.elasticity2d", ["conditions"])
    assert {spec.key for spec in elastic.conditions} == {"fixed", "traction_x", "traction_y"}
    assert all(spec.unit and spec.description for spec in elastic.conditions)

    flow = core.capability_describe("mock.laplace2d", ["conditions"])
    assert flow.conditions == []

    # Absent when not asked for, like every other section — a caller that misspells the name
    # and gets a payload without conditions must not conclude the capability has none.
    narrow = core.capability_describe("mock.elasticity2d", ["metrics"])
    assert narrow.conditions is None


def test_the_shipped_adapters_that_declare_conditions_are_the_ones_that_read_them():
    """A declaration nobody reads is worse than none: it advertises an input that is ignored.

    Asserted against the **installed** capabilities rather than a hard-coded list, so an
    adapter that declares `conditions` without consuming `ctx.conditions` fails the build.
    """
    import inspect

    from fenixspoon.solvers.registry import registered_solvers

    for solver_cls in registered_solvers():
        source = inspect.getsource(solver_cls)
        reads = "ctx.conditions" in source or "governed_by_load_case" in source
        assert bool(solver_cls.conditions) == reads, (
            f"{solver_cls.name} declares {len(solver_cls.conditions)} condition keys and "
            f"{'does' if reads else 'does not'} read them"
        )


def test_potential_flow_and_heat_are_untouched(core, me):
    """#85's fourth acceptance point. Their conditions are `part` selectors nobody wrote down.

    The check that matters is not that they still solve — everything still solves — but that
    they still solve *the same*, so the whole geometry-and-boundaries addition is genuinely
    additive for a capability that ignores it.
    """
    # Heat takes `regions2d`, potential flow takes `domain2d`, so both geometry kinds are
    # covered — the `boundaries` list was added to each of them and each has to stay additive.
    sink = {
        "type": "regions2d",
        "bounds": [0.0, 0.0, 1.0, 1.0],
        "regions": [
            {
                "name": "block",
                "shape": {
                    "type": "polygon2d",
                    "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
                },
                "material": {"k": 200.0},
            }
        ],
        "background": {"k": 1.0},
        "boundaries": [
            {"name": "wall", "select": {"type": "part", "of": "outer"}},
            {"name": "block_face", "select": {"type": "part", "of": "region:block"}},
        ],
    }

    for solver, named in (("mock.laplace2d", plate()), ("mock.heat2d", sink)):
        plain = {k: v for k, v in named.items() if k != "boundaries"}
        params = {"resolution": 40}
        without = solve(core, me, solver=solver, geometry=plain, params=params)
        with_names = solve(core, me, solver=solver, geometry=named, params=params)
        assert without.status == with_names.status == "done", without.error or with_names.error
        # Identical numbers, not merely a successful solve. Naming a boundary must not change
        # the answer for a capability that reads no conditions — otherwise "additive" would be
        # a claim about the schema rather than about the physics.
        assert without.result.metrics == pytest.approx(with_names.result.metrics)
        # The cache keys *differ*, and that is right rather than a flaw worth fixing. The key
        # hashes the validated geometry, and adding a `boundaries` list genuinely changes it —
        # so naming a boundary costs a re-solve on a capability that will not read it. The
        # asymmetry is the one the cache is built around: a missed hit is a solve, and a hit
        # that ignored a field would be a wrong answer. Asserted so that anyone tempted to
        # exclude `boundaries` from the hash has to come here and argue for it.
        assert without.cache_key != with_names.cache_key
