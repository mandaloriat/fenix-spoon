"""The `axisymmetric2d` geometry kind and the electrostatics that consumes it (#100).

Three things are being checked here, and they are different in kind:

- **The validation that makes the kind worth having.** A radius cannot be negative, and a
  region may sit on the axis when the section reaches it. Everything else is `regions2d`'s
  rules, checked here too — not out of thoroughness but because they are the *same function*
  and this is the assertion that says so.
- **The refusal.** A meridian section sent to a plane solver is a `422`, and that is the
  whole argument for a discriminator rather than a convention: before the kind existed the
  same payload reached a plane solver, which accepted it and quietly solved a slice.
- **The physics, against an answer from outside this repository.** A coaxial section has a
  closed-form capacitance, and it is a form the `r` weight cannot be dropped from — which is
  the one property a test of an axisymmetric solver has to establish.
"""

import asyncio

import numpy as np
import pytest
from geometries import (
    CAPACITIVE_SENSOR,
    COAX,
    ROD_ON_AXIS,
    SENSOR_GAP,
    SENSOR_R1,
    SENSOR_R2,
    SOLENOID,
    rect,
)
from pydantic import TypeAdapter, ValidationError

from fenixspoon.boundaries import predicate
from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import (
    Axisymmetric2D,
    BoundarySpec,
    Geometry,
    NearSelector,
    PointsSelector,
    Polygon2D,
    Region2D,
)
from fenixspoon.jobs import JobManager
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_electrostatics import EPS0, MockElectrostaticsAxi2D

COAX_CONDITIONS = {"inner": {"voltage": 1.0}, "outer": {"voltage": 0.0}}


def make_ctx(tmp_path, conditions=None, events=None):
    return SolverContext(
        progress_cb=(events.append if events is not None else lambda e: None),
        artifact_dir=tmp_path / "artifacts",
        conditions=conditions,
    )


def solve(geometry, tmp_path, conditions=None, **params):
    return MockElectrostaticsAxi2D().solve(
        geometry,
        MockElectrostaticsAxi2D.Params(write_vtk=False, **params),
        make_ctx(tmp_path, conditions),
    )


# ------------------------------------------------------------------- the geometry kind


def test_the_kind_round_trips_through_the_union():
    """A payload picks the member by its discriminator, like the other two kinds."""
    geometry = TypeAdapter(Geometry).validate_python(COAX.model_dump(mode="json"))
    assert isinstance(geometry, Axisymmetric2D)
    assert geometry.type == "axisymmetric2d"


def test_a_negative_rmin_is_refused():
    """The constraint that is most of the reason the kind exists.

    A negative radius is not a domain a body of revolution has. It is a plane section that
    has been mislabelled — and left unchecked it would solve, because every other rule in the
    model is happy with it, and return a number nobody could tell was wrong.
    """
    with pytest.raises(ValidationError, match="rmin must be >= 0"):
        Axisymmetric2D(
            bounds=(-0.01, -0.01, 0.02, 0.01),
            regions=[Region2D(name="a", shape=rect(0.001, -0.005, 0.005, 0.005))],
        )


def test_a_region_may_sit_on_the_axis_but_only_on_the_axis():
    """The one relaxation of `regions2d`'s strictly-inside rule, and its limit.

    A solid shaft is bounded by r = 0, so its outline lies on the left edge; refusing that
    would make the commonest axisymmetric shape unrepresentable. The relaxation is the *axis*
    rather than the left edge in general: a section that starts at r = 19 mm has a truncation
    there, not a symmetry, and a region touching it is the ordinary error it always was.
    """
    assert ROD_ON_AXIS.reaches_axis
    assert ROD_ON_AXIS.regions[0].shape.points[0][0] == 0.0

    with pytest.raises(ValidationError, match="strictly inside"):
        Axisymmetric2D(
            bounds=(0.001, -0.01, 0.02, 0.01),
            regions=[Region2D(name="a", shape=rect(0.001, -0.005, 0.005, 0.005))],
        )


def test_a_section_that_does_not_reach_the_axis_is_legitimate():
    """The sensor's section starts at r = 19 mm, and nothing about that is a degenerate case.

    Stated as a test because it is the assumption a solver is most likely to make: the axis
    is *usually* in an axisymmetric domain, and a solver that attached a condition to the
    left edge on that basis would put a symmetry plane through a truncation.
    """
    assert not CAPACITIVE_SENSOR.reaches_axis
    assert CAPACITIVE_SENSOR.bounds[0] > 0.0


def test_the_region_rules_are_regions2d_s_rules():
    """Not a re-test of `regions2d` — an assertion that there is one implementation.

    Two copies of "regions must be disjoint or fully nested" would drift, and the drift would
    be invisible: a payload legal as a plane section and illegal as a meridian one, for no
    reason a caller could see.
    """
    with pytest.raises(ValidationError, match="region names must be unique"):
        Axisymmetric2D(
            bounds=(0.0, 0.0, 1.0, 1.0),
            regions=[
                Region2D(name="a", shape=rect(0.1, 0.1, 0.4, 0.4)),
                Region2D(name="a", shape=rect(0.5, 0.5, 0.9, 0.9)),
            ],
        )
    with pytest.raises(ValidationError, match="overlap partially"):
        Axisymmetric2D(
            bounds=(0.0, 0.0, 1.0, 1.0),
            regions=[
                Region2D(name="a", shape=rect(0.1, 0.1, 0.5, 0.5)),
                Region2D(name="b", shape=rect(0.3, 0.3, 0.9, 0.9)),
            ],
        )


def test_naming_the_axis_costs_nothing_new():
    """Protocol 1.8's `near` selector already names r = 0; the kind adds no mechanism.

    Worth a test rather than a sentence, because "it should just work" is the claim being
    made: `boundaries.predicate` resolves a selector against `geometry.regions`, and the new
    member has regions, so every selector family reaches it unchanged.
    """
    geometry = ROD_ON_AXIS.model_copy(
        update={
            "boundaries": [
                BoundarySpec(name="axis", select=NearSelector(axis="x", value=0.0)),
            ]
        }
    )
    on_axis = predicate(geometry, "axis")(np.array([[0.0, 0.005], [0.0, 0.0]]))
    assert list(on_axis) == [True, False]


def _rod_with_ids(*boundaries: BoundarySpec) -> Axisymmetric2D:
    return Axisymmetric2D(
        bounds=(0.0, -0.01, 0.02, 0.01),
        regions=[
            Region2D(
                name="rod",
                shape=Polygon2D(
                    points=[(0.0, -0.005), (0.005, -0.005), (0.005, 0.005), (0.0, 0.005)],
                    point_ids=["axis_low", "low", "high", "axis_high"],
                ),
            )
        ],
        boundaries=list(boundaries),
    )


def test_a_points_selector_reaches_the_new_kind_too():
    """The id-based family, and the refusal that comes with it, on an axisymmetric outline.

    `_check_boundaries` is called from the shared region-map check, so a selector that names
    two non-adjacent vertices — which would resolve to a predicate false everywhere — is
    refused here for the same reason it is on a plane section.
    """
    geometry = _rod_with_ids(
        BoundarySpec(name="outer_face", select=PointsSelector(ids=["low", "high"]))
    )
    on_face = predicate(geometry, "outer_face")(np.array([[0.005, 0.0], [0.0, 0.0]]))
    assert list(on_face) == [True, False]

    with pytest.raises(ValidationError, match="span no edge"):
        _rod_with_ids(
            BoundarySpec(name="bad", select=PointsSelector(ids=["axis_low", "high"]))
        )


# ------------------------------------------------------------------------- the refusal


def test_a_plane_solver_refuses_a_meridian_section(tmp_path):
    """The `422` that is the point of the whole exercise.

    Before the kind existed this payload was a `regions2d` with bounds starting at zero, and
    `mock.magnetostatics2d` accepted it and solved a slice through an infinite prism. The
    answer was a number, not an error, which is the failure mode a discriminator plus
    `geometry_types` converts into a refusal at submit.
    """
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    me = Principal(id="anonymous", quotas=Quotas())

    with pytest.raises(errors.GeometryKindMismatch):
        asyncio.run(core.submit("mock.magnetostatics2d", CAPACITIVE_SENSOR, {}, me))

    # And the converse: the axisymmetric solver refuses a plane region map, so the two
    # readings of the same rectangle can never be swapped by accident in either direction.
    with pytest.raises(errors.GeometryKindMismatch):
        asyncio.run(core.submit("mock.electrostatics_axi2d", SOLENOID, {}, me))


def test_the_capability_declares_the_kind_it_reads():
    assert MockElectrostaticsAxi2D.geometry_types == ["axisymmetric2d"]


# ------------------------------------------------------------------------ the physics


def test_the_coaxial_capacitance_matches_the_closed_form(tmp_path):
    """The external answer, and the one test here that could not pass by accident.

    Between concentric cylinders `C = 2*pi*eps*L/ln(b/a)`, and the z truncation costs nothing
    because the exact solution does not vary with z. So the only thing left to be wrong is
    the radial discretisation — and the `r` weight, without which this number is not close.
    """
    a, b, length = 0.001, 0.003, 0.002
    result = solve(COAX, tmp_path, COAX_CONDITIONS, resolution=96, iterations=8000)

    exact = 2.0 * np.pi * EPS0 * length / np.log(b / a)
    assert result.converged
    assert result.metrics["capacitance"] == pytest.approx(exact, rel=0.01)
    # The energy the capacitance is read off, checked independently of the division.
    assert result.metrics["energy"] == pytest.approx(0.5 * exact, rel=0.01)


def test_dropping_the_r_weight_would_not_pass_that_test(tmp_path):
    """The counter-case, so the test above cannot be satisfied by a plane solver.

    A plane slice of the same rectangle is a parallel-plate capacitor per unit depth, and its
    capacitance is a different number by nearly a factor of two here. Asserting the distance
    is what makes the closed-form check evidence *for the axisymmetric reduction* rather than
    merely for a Laplace solve that runs.
    """
    a, b, length = 0.001, 0.003, 0.002
    axisymmetric = 2.0 * np.pi * EPS0 * length / np.log(b / a)
    plane_per_metre = EPS0 * length / (b - a)
    # Per unit depth, so the honest comparison is against one metre of it.
    assert abs(plane_per_metre / axisymmetric - 1.0) > 0.5

    result = solve(COAX, tmp_path, COAX_CONDITIONS, resolution=96, iterations=8000)
    assert abs(result.metrics["capacitance"] / plane_per_metre - 1.0) > 0.5


def test_the_potential_falls_logarithmically_across_the_gap(tmp_path):
    """The field itself, not only the integral of it.

    An energy can be right for the wrong reasons; the profile cannot. `ln(b/r)/ln(b/a)` is
    what the cylindrical operator produces and `(b-r)/(b-a)` is what a plane one would.
    """
    result = solve(COAX, tmp_path, COAX_CONDITIONS, resolution=96, iterations=8000)
    nz, nr = result.data["shape"]
    potential = np.asarray(result.data["fields"]["V"]).reshape(nz, nr)
    r = np.linspace(0.001, 0.003, nr)

    middle = potential[nz // 2]
    expected = np.log(0.003 / r) / np.log(3.0)
    assert np.max(np.abs(middle - expected)) < 0.01
    # Nowhere near the straight line a plane solve would give.
    assert np.max(np.abs(middle - (0.003 - r) / 0.002)) > 0.05


def test_the_sensor_reads_above_its_parallel_plate_estimate(tmp_path):
    """The motivating case: fringe and chamfer add capacitance, and by how much is the answer.

    The published sensor sits about 15.7% above its parallel-plate value. This section is the
    same *shape* rather than the same dimensions (see `geometries.CAPACITIVE_SENSOR`), so
    what is asserted is the sign and a band — a solver that returned the parallel-plate value
    would be one that had lost the fringe entirely, and one that returned half again as much
    would be reporting something other than this capacitor.

    The band is wide at the bottom for a reason worth knowing before quoting a number from
    this adapter: the gap is 90 µm and the grid spacing at this resolution is about 16 µm, so
    the electrode edge is staircased and the fringe is under-resolved. That is the accuracy
    limit of a raster, and it is why the FEniCSx half of the pair meshes the outline.
    """
    parallel_plate = EPS0 * np.pi * (SENSOR_R2**2 - SENSOR_R1**2) / SENSOR_GAP
    result = solve(CAPACITIVE_SENSOR, tmp_path, resolution=256, iterations=20000)

    capacitance = result.metrics["capacitance"]
    assert result.converged
    assert capacitance > parallel_plate
    assert capacitance < 1.5 * parallel_plate


def test_refining_the_grid_moves_the_sensor_capacitance_towards_the_fringe(tmp_path):
    """Two resolutions, because one number cannot say whether it is converged.

    A coarse grid quantises the gap — 90 µm across 2.8 cells is a wider gap than the geometry
    describes — so it reads *below* the parallel-plate value. Refining lifts it past that and
    into the fringe. Asserting the direction is what turns the single-resolution test above
    from a threshold into a statement about the physics.
    """
    coarse = solve(CAPACITIVE_SENSOR, tmp_path, resolution=128, iterations=20000)
    fine = solve(CAPACITIVE_SENSOR, tmp_path, resolution=256, iterations=20000)
    assert fine.metrics["capacitance"] > coarse.metrics["capacitance"]


def test_a_region_on_the_axis_solves(tmp_path):
    """The shape the strictly-inside rule would have excluded, run rather than only validated.

    r = 0 gets no condition and needs none: in an r-weighted form the boundary term carries
    the same r and vanishes there. What that produces is a field with no radial gradient on
    the axis, which is what is checked — a Dirichlet condition mistakenly applied there would
    show up as a kink instead.
    """
    result = solve(ROD_ON_AXIS, tmp_path, resolution=96, iterations=8000)
    nz, nr = result.data["shape"]
    potential = np.asarray(result.data["fields"]["V"]).reshape(nz, nr)
    e_r = np.asarray(result.data["vector_fields"]["E_vec"]).reshape(nz, nr, 2)[..., 0]
    z = np.linspace(-0.01, 0.01, nz)

    # The rod is metal all the way to the axis: no column of background up its middle, which
    # is what an even-odd test against an outline lying on the grid would have left.
    on_rod = np.abs(z) < 0.004
    assert np.allclose(potential[on_rod, 0], 1.0)
    # And off the rod the axis is dielectric, where the symmetry condition has to hold on its
    # own — a Dirichlet condition mistakenly applied there would show as a radial gradient.
    assert np.max(np.abs(e_r[:, 0])) < 1e-9 * np.max(np.abs(e_r))
    assert 0.0 < potential[np.abs(z) > 0.008, 0].max() < 1.0


def test_permittivity_raises_the_capacitance(tmp_path):
    """A dielectric in the gap multiplies the capacitance by its `eps_r`, and the coaxial
    case is the one where that factor is exact rather than approximate."""
    filled = COAX.model_copy(deep=True)
    filled.background["eps_r"] = 3.0
    filled.regions[0].material["eps_r"] = 3.0

    empty = solve(COAX, tmp_path, COAX_CONDITIONS, resolution=96, iterations=8000)
    with_dielectric = solve(filled, tmp_path, COAX_CONDITIONS, resolution=96, iterations=8000)
    assert with_dielectric.metrics["capacitance"] == pytest.approx(
        3.0 * empty.metrics["capacitance"], rel=0.01
    )


def test_a_model_with_nothing_held_is_refused(tmp_path):
    """Singular, and refused rather than relaxed into a plausible zero.

    With no potential pinned every constant solves the problem, so a relaxation returns its
    initial guess — a field of zeros and a capacitance of zero, with nothing to say the
    question was ill-posed.
    """
    floating = COAX.model_copy(deep=True)
    with pytest.raises(ValueError, match="no electrode"):
        solve(floating, tmp_path, resolution=32, iterations=10)


def test_one_potential_reports_energy_and_no_capacitance(tmp_path):
    """Declared and absent beats declared and zero.

    One electrode is a well-posed problem with a field-free answer; there is simply no second
    conductor for a capacitance to be between. `2W/V²` would evaluate to zero and read as an
    answer.
    """
    single = ROD_ON_AXIS.model_copy(deep=True)
    single.regions[1].material["voltage"] = 1.0
    result = solve(single, tmp_path, resolution=64, iterations=2000)

    assert "capacitance" not in result.metrics
    assert result.metrics["energy"] == pytest.approx(0.0, abs=1e-24)
    assert any("no capacitance" in warning for warning in result.warnings)


def test_both_result_kinds_carry_the_section(tmp_path):
    """`mesh2d` is a triangulation of the same grid, so the fields must agree."""
    grid = solve(COAX, tmp_path, COAX_CONDITIONS, resolution=48, iterations=2000)
    mesh = solve(
        COAX, tmp_path, COAX_CONDITIONS, resolution=48, iterations=2000, output="mesh2d"
    )
    assert mesh.kind == "mesh2d"
    assert max(mesh.data["point_fields"]["V"]) == pytest.approx(
        max(grid.data["fields"]["V"])
    )
    assert mesh.metrics["capacitance"] == pytest.approx(grid.metrics["capacitance"])


def test_the_declared_metric_is_filled_from_the_payload(tmp_path):
    """`e_max` is the one metric of the three that *is* a reduction, and the runtime fills
    it — the adapter reports energy and capacitance because neither is in the payload."""
    from fenixspoon.solvers.base import fill_declared_metrics

    result = solve(COAX, tmp_path, COAX_CONDITIONS, resolution=48, iterations=2000)
    assert "e_max" not in result.metrics
    fill_declared_metrics(MockElectrostaticsAxi2D, result)
    assert result.metrics["e_max"] == pytest.approx(max(result.data["fields"]["E"]))


def test_the_adapter_declares_the_axisymmetric_reduction_not_the_prism(tmp_path):
    """Both statements are named `two_dimensional`, and they say opposite things.

    An adapter for a body of revolution that shipped the infinite-prism sentence would be
    telling a caller its farads were farads per metre.
    """
    del tmp_path
    statement = next(
        a.statement
        for a in MockElectrostaticsAxi2D.assumptions
        if a.name == "two_dimensional"
    )
    assert "body of revolution" in statement
    assert "per unit depth" in statement  # it says results are *not*
    assert "infinitely long" not in statement
