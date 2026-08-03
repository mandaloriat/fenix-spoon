"""Linear elasticity, checked against closed forms rather than against itself (#81).

The two adapters are the first here with a **vector** unknown, and the first whose boundary
conditions have to be *placed* rather than implied by the geometry. Both of those are things
a test can get wrong quietly, so every assertion below is either an analytical result or a
statement about how the error behaves under refinement.

What each adapter is good for is not the same, and the tests say so:

- the **mock** is exact where the strain is constant, converges to `PL^3/3EI` from below as
  the mesh refines (constant-strain triangles are stiff in bending), and cannot resolve a
  curved hole at all — it masks a Cartesian grid, so the hole is staircased and the peak
  stress is sensitive to how the steps happen to fall;
- the **FEniCSx** adapter meshes the hole properly and uses a quadratic space, so it is the
  one held to the classical `K_t = 3`.
"""

import numpy as np
import pytest
from geometries import BEAM, PLATE_WITH_HOLE, UNIFORM_BEAM

from fenixspoon.geometry import BoundarySpec, BoxSelector, NearSelector
from fenixspoon.solvers import fill_declared_metrics, get_solver
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_elasticity import MockElasticity2D

E = 2.1e11
NU = 0.3
LENGTH = 1.0
DEPTH = 0.1


def run(solver_cls, geometry, **params):
    """Solve directly and fill the declared metrics, which is what a caller would receive."""
    import tempfile
    from pathlib import Path

    ctx = SolverContext(
        progress_cb=lambda event: None, artifact_dir=Path(tempfile.mkdtemp())
    )
    result = solver_cls().solve(
        geometry, solver_cls.Params(write_vtk=False, **params), ctx
    )
    fill_declared_metrics(solver_cls, result)
    return result


def tip_deflection(traction_y: float) -> float:
    """Euler-Bernoulli tip deflection of a cantilever under an end shear, per unit depth."""
    load = abs(traction_y) * DEPTH
    second_moment = DEPTH**3 / 12.0
    return load * LENGTH**3 / (3.0 * E * second_moment)


# --------------------------------------------------------------------- the mock adapter


def test_uniform_tension_is_exact_away_from_the_clamp():
    """Constant strain is the one state a constant-strain triangle reproduces exactly.

    Not everywhere, though, and the exception is physics rather than discretization:
    clamping an edge in both directions forbids the Poisson contraction there, so the stress
    rises at the clamped corners. Saint-Venant says that disturbance decays within about a
    depth, which is why the *median* is the right statistic for the far field and the peak is
    not.
    """
    stress = 1.0e6
    result = run(
        MockElasticity2D, UNIFORM_BEAM, resolution=48, traction=(stress, 0.0),
        fixed_edge="xmin", load_edge="xmax",
    )

    far_field = np.median(result.data["point_fields"]["sigma_vm"])
    assert far_field == pytest.approx(stress, rel=1e-6)
    assert result.metrics["u_max"] == pytest.approx(stress * LENGTH / E, rel=1e-2)
    # The clamp concentration is real and modest; a run that lost it entirely would mean the
    # boundary condition stopped being applied.
    assert 1.0 < result.metrics["sigma_vm_max"] / far_field < 1.3


def test_a_cantilever_converges_to_the_beam_formula_from_below():
    """CST elements are stiff in bending, so the interesting assertion is the *trend*.

    A single tolerance would pass on a solver that was wrong by a constant factor. Requiring
    the error to shrink under refinement, and to approach from below, is a statement about
    the element that a wrong stiffness matrix could not satisfy.
    """
    traction = -1.0e6
    exact = tip_deflection(traction)
    coarse, fine = (
        run(
            MockElasticity2D, UNIFORM_BEAM, resolution=resolution, traction=(0.0, traction),
            fixed_edge="xmin", load_edge="xmax", iterations=40000,
        ).metrics["u_max"]
        for resolution in (48, 96)
    )

    assert coarse < fine < exact * 1.02, "the cantilever stopped approaching from below"
    assert abs(fine - exact) < abs(coarse - exact), "refining the mesh did not help"
    assert fine == pytest.approx(exact, rel=0.08)


def test_a_hole_concentrates_the_stress():
    """A masked grid cannot resolve a circle, and this test does not pretend otherwise.

    The peak here lands between roughly 2.3 and 3.3 depending on where the staircase steps
    fall relative to the hole — refinement moves it around rather than converging, which is
    exactly why `test_the_fenicsx_adapter_finds_the_classical_stress_concentration` exists
    and is marked `fenics`. What this asserts is the part the mock *can* say: a hole raises
    the local stress far above the far field, and the far field itself is undisturbed.
    """
    stress = 1.0e6
    result = run(
        MockElasticity2D, PLATE_WITH_HOLE, resolution=96, traction=(stress, 0.0),
        fixed_edge="xmin", load_edge="xmax", iterations=60000,
    )

    assert np.median(result.data["point_fields"]["sigma_vm"]) == pytest.approx(stress, rel=0.02)
    assert 2.3 < result.metrics["sigma_vm_max"] / stress < 3.3


def test_plane_strain_is_stiffer_than_plane_stress():
    """The `plane` parameter is not decorative: picking the wrong one is a quiet error.

    A plane-strain body is restrained out of plane, so it deflects less under the same load.
    If the two cases ever returned the same numbers, the parameter would be doing nothing and
    the `plane_idealisation` assumption would be a false declaration.
    """
    shared = dict(
        resolution=48, traction=(0.0, -1.0e6), fixed_edge="xmin", load_edge="xmax",
        iterations=40000,
    )
    stress = run(MockElasticity2D, UNIFORM_BEAM, plane="stress", **shared)
    strain = run(MockElasticity2D, UNIFORM_BEAM, plane="strain", **shared)

    assert strain.metrics["u_max"] < stress.metrics["u_max"]
    assert strain.metrics["u_max"] == pytest.approx(
        stress.metrics["u_max"] * (1.0 - NU**2), rel=0.02
    )


def test_a_stiffer_region_stiffens_the_plate():
    """Per-region material reaches the solve, checked by a comparison rather than a lookup.

    `BEAM` is aluminium with a steel insert at mid-span and `UNIFORM_BEAM` is steel
    throughout, so the mixed one must deflect more. Asserting on the returned modulus field
    would only prove the rasteriser ran.
    """
    shared = dict(
        resolution=48, traction=(0.0, -1.0e6), fixed_edge="xmin", load_edge="xmax",
        iterations=40000,
    )
    mixed = run(MockElasticity2D, BEAM, **shared)
    steel = run(MockElasticity2D, UNIFORM_BEAM, **shared)

    assert mixed.metrics["u_max"] > steel.metrics["u_max"]


def test_compliance_is_the_work_the_load_does():
    """`compliance` is the one metric no declaration can compute, so it needs its own check.

    For a linear body the external work is `f·u`, and the strain energy is half of it. The
    test that catches a factor-of-two error is the comparison with an independent estimate:
    the load times the tip deflection, which for an end shear is the same quantity.
    """
    traction = -1.0e6
    result = run(
        MockElasticity2D, UNIFORM_BEAM, resolution=96, traction=(0.0, traction),
        fixed_edge="xmin", load_edge="xmax", iterations=40000,
    )
    expected = abs(traction) * DEPTH * result.metrics["u_max"]
    assert result.metrics["compliance"] == pytest.approx(expected, rel=0.2)


def test_the_same_edge_cannot_be_clamped_and_loaded():
    """A body clamped where it is loaded has no answer worth returning, and pydantic says so
    at validation time rather than letting the solve produce zeros."""
    with pytest.raises(ValueError, match="different edges"):
        MockElasticity2D.Params(fixed_edge="xmin", load_edge="xmin")


def test_the_declared_assumptions_say_where_the_load_can_go():
    """#81's open question, declared rather than left in the source.

    A caller has no other way to discover that a load cannot be put on a hole boundary or on
    part of an edge — and the whole point of the `assumptions` section is that such a limit
    is discoverable before a solve, not after a surprising result.
    """
    names = {assumption.name for assumption in MockElasticity2D.assumptions}
    assert "edge_aligned_boundary_conditions" in names
    edge_rule = next(
        assumption
        for assumption in MockElasticity2D.assumptions
        if assumption.name == "edge_aligned_boundary_conditions"
    )
    assert {"partial_edge_load", "hole_boundary_load", "point_load"} <= set(edge_rule.excludes)


# ------------------------------------------------------------------ the FEniCSx adapter


def fenicsx():
    solver = get_solver("dolfinx.elasticity2d")
    if solver is None:
        pytest.skip("requires dolfinx (FEniCSx)")
    return solver


@pytest.mark.fenics
def test_the_fenicsx_cantilever_matches_the_beam_formula():
    """Quadratic elements on a conforming mesh: this is the adapter whose number is quotable.

    The tolerance is 5% rather than 1% because Euler-Bernoulli is itself an approximation —
    it omits shear deflection, which for a beam of aspect ratio ten is a percent or two, and
    the finite element is the more accurate of the two. Tightening this further would be
    asserting that the beam theory is right.
    """
    traction = -1.0e6
    result = run(
        fenicsx(), UNIFORM_BEAM, mesh_size=0.02, degree=2, traction=(0.0, traction),
        fixed_edge="xmin", load_edge="xmax",
    )
    assert result.metrics["u_max"] == pytest.approx(tip_deflection(traction), rel=0.05)


@pytest.mark.fenics
def test_the_fenicsx_adapter_finds_the_classical_stress_concentration():
    """Kirsch: a circular hole in a wide plate triples the stress. The check the mock cannot
    make, because this adapter meshes the hole instead of masking a grid over it."""
    stress = 1.0e6
    result = run(
        fenicsx(), PLATE_WITH_HOLE, mesh_size=0.03, degree=2, traction=(stress, 0.0),
        fixed_edge="xmin", load_edge="xmax",
    )
    assert result.metrics["sigma_vm_max"] / stress == pytest.approx(3.0, rel=0.15)


@pytest.mark.fenics
def test_the_pair_agrees_on_a_case_neither_of_them_defines():
    """The cross-validation the shared declaration exists for.

    Same geometry, same load, same material, two entirely different discretizations — and a
    caller who swapped one for the other must get the same engineering answer. The tolerance
    is loose because the mock is a first-order element on a structured grid; what would fail
    is a sign error, a factor of two, or a different idea of what `traction` means.
    """
    shared = dict(traction=(0.0, -1.0e6), fixed_edge="xmin", load_edge="xmax")
    mock = run(MockElasticity2D, UNIFORM_BEAM, resolution=128, iterations=40000, **shared)
    fenics = run(fenicsx(), UNIFORM_BEAM, mesh_size=0.02, degree=2, **shared)

    assert mock.metrics["u_max"] == pytest.approx(fenics.metrics["u_max"], rel=0.1)
    assert set(mock.metrics) == set(fenics.metrics)


# ------------------------------------------------------------- load cases (#85, second half)


def _named_beam(**boundaries):
    """`UNIFORM_BEAM` with named ends, so a load case can put the conditions on them."""
    return UNIFORM_BEAM.model_copy(
        update={
            "boundaries": [
                BoundarySpec(name=name, select=select) for name, select in boundaries.items()
            ]
        }
    )


ENDS = {
    "root": NearSelector(axis="x", value=0.0, tol=1e-9),
    "tip": NearSelector(axis="x", value=1.0, tol=1e-9),
}


def test_a_load_case_reproduces_the_shorthand_it_replaces():
    """The two ways of placing a condition must describe one problem.

    `fixed_edge="xmin"` and a load case clamping a boundary that *is* `xmin` are the same
    cantilever, so they must return the same number — not merely a plausible one. This is the
    assertion that catches a load case wired to the right edge with the wrong sign, applied
    to vertices instead of edges, or scaled by a tributary length computed differently from
    the shorthand's.
    """
    shared = dict(resolution=128, iterations=40000)
    by_params = run(
        MockElasticity2D, UNIFORM_BEAM,
        fixed_edge="xmin", load_edge="xmax", traction=(0.0, -1.0e6), **shared,
    )

    ctx = SolverContext(
        progress_cb=lambda event: None,
        conditions={"root": {"fixed": 1.0}, "tip": {"traction_y": -1.0e6}},
    )
    by_load_case = MockElasticity2D().solve(
        _named_beam(**ENDS), MockElasticity2D.Params(write_vtk=False, **shared), ctx
    )
    fill_declared_metrics(MockElasticity2D, by_load_case)

    assert by_load_case.metrics["u_max"] == pytest.approx(by_params.metrics["u_max"], rel=1e-6)
    assert by_load_case.metrics["compliance"] == pytest.approx(
        by_params.metrics["compliance"], rel=1e-6
    )


def test_the_mock_refuses_a_boundary_its_raster_does_not_resolve():
    """The `raster_conforming_boundary` assumption, as behaviour rather than as prose.

    A clamp that selected no node would leave the body free and the solve would either fail
    far downstream or return a rigid-body displacement — so it is refused here, naming the
    boundary and what to do about it.
    """
    off_grid = _named_beam(sliver=NearSelector(axis="x", value=0.4999999, tol=1e-12))
    ctx = SolverContext(
        progress_cb=lambda event: None, conditions={"sliver": {"fixed": 1.0}}
    )
    with pytest.raises(ValueError, match="no mesh node lies on boundary 'sliver'"):
        MockElasticity2D().solve(
            off_grid, MockElasticity2D.Params(write_vtk=False, resolution=64), ctx
        )


@pytest.mark.fenics
def test_the_fenicsx_adapter_takes_a_load_case_on_an_unaligned_boundary():
    """What the mock's assumption says this half can do and the other cannot.

    Gmsh meshes the outline, so a boundary at an arbitrary position has facets on it. The
    clamp sits at x = 0.0 as before, and the *load* is a strip the raster could not resolve —
    which is the whole difference the two declarations describe.
    """
    beam = _named_beam(
        root=NearSelector(axis="x", value=0.0, tol=1e-9),
        strip=BoxSelector(bounds=(0.937, -0.0501, 1.0001, 0.0501)),
    )
    ctx = SolverContext(
        progress_cb=lambda event: None,
        conditions={"root": {"fixed": 1.0}, "strip": {"traction_y": -1.0e6}},
    )
    result = fenicsx()().solve(
        beam, fenicsx().Params(write_vtk=False, mesh_size=0.02, degree=2), ctx
    )
    fill_declared_metrics(fenicsx(), result)

    # A downward load near the free end of a clamped beam: it deflects downward, and by
    # roughly what a shorter cantilever would. The point is that the load landed on the strip
    # at all — the mock refuses this geometry outright.
    assert result.metrics["u_max"] > 0
    assert result.metrics["compliance"] > 0


@pytest.mark.fenics
def test_the_pair_agrees_through_a_load_case_too():
    """The cross-validation of #81, re-run over the path #85 added.

    Two discretizations, one load case, one answer. If the FEniCSx facet tags and the mock's
    boundary-edge walk disagreed about which edge `tip` names, or about how a traction becomes
    nodal forces, this is where it would show.
    """
    beam = _named_beam(**ENDS)
    conditions = {"root": {"fixed": 1.0}, "tip": {"traction_y": -1.0e6}}

    def solve_with(solver_cls, **params):
        ctx = SolverContext(progress_cb=lambda event: None, conditions=conditions)
        result = solver_cls().solve(
            beam, solver_cls.Params(write_vtk=False, **params), ctx
        )
        fill_declared_metrics(solver_cls, result)
        return result

    mock = solve_with(MockElasticity2D, resolution=128, iterations=40000)
    fenics = solve_with(fenicsx(), mesh_size=0.02, degree=2)

    assert mock.metrics["u_max"] == pytest.approx(fenics.metrics["u_max"], rel=0.1)
