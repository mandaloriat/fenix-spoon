"""Circulation, lift and surface pressure (#68).

#68 was filed because `mock.laplace2d` and `dolfinx.potential_flow2d` reported a lift of
essentially zero at every incidence and said nothing about why — the circulation was whatever
the body's streamfunction constant happened to make it, and that constant was a heuristic. So
these tests have to establish two different things, and they are separated below because they
are convincing for different reasons.

**The exact half.** Potential flow around a cylinder with imposed circulation has a closed-form
streamfunction, a closed-form surface `C_p`, and `L' = rho U Gamma`; and the Kutta condition on
a cylinder is the Joukowski airfoil's condition in the plane it lives in, so it too has a
closed-form answer, `Gamma = 4 pi U R sin(alpha + beta)`. Those are checked against the
arithmetic directly, with no solver in the way, which is what makes them checks of *this code*
rather than checks of a discretisation. This is the verification #68 asked for and the reason it
argued the work belonged upstream rather than in an application.

**The approximate half.** Then the real solver, against what thin-airfoil theory says a section
must do: no lift at zero incidence when symmetric, a lift-curve slope near 2*pi, a zero-lift
angle near -2 degrees for NACA 2412, a nose-down quarter-chord moment when cambered. Those are
bands rather than equalities, and the band widths here are *measured*, not hoped for — the
figures in each docstring are what this implementation actually produces.
"""

import numpy as np
import pytest
from geometries import naca4

from fenixspoon.geometry import Domain2D, Polygon2D
from fenixspoon.solvers import aerodynamics as aero
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_laplace import MockLaplace2D

# ------------------------------------------------------------------ geometry fixtures


def cylinder(radius: float, n: int = 240):
    angle = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.column_stack([radius * np.cos(angle), radius * np.sin(angle)])


def horizontal_chord(points: np.ndarray) -> aero.Profile:
    """A :class:`~fenixspoon.solvers.aerodynamics.Profile` with the chord pinned along x.

    A circle has no chord: every diameter is the longest, so `profile_of`'s furthest-apart
    search picks whichever tie `argmax` reaches first and the lift/drag axes come out rotated by
    an arbitrary angle. That is honest behaviour for an ambiguous body — and it means a cylinder
    test has to say which diameter it means.
    """
    return aero.Profile(
        points=points,
        le_index=int(np.argmin(points[:, 0])),
        te_index=int(np.argmax(points[:, 0])),
    )


def solve(points, **params):
    """Run the mock adapter on a chord-1 body in a domain with room around it."""
    context = SolverContext(progress_cb=lambda event: None)
    geometry = Domain2D(
        bounds=(-1.5, -1.5, 2.5, 1.5), obstacle=Polygon2D(points=[tuple(p) for p in points])
    )
    return MockLaplace2D().solve(
        geometry, MockLaplace2D.Params(write_vtk=False, **params), context
    )


def grid_of(result):
    """Unpack a `grid2d` result back into the arrays this module works on."""
    ny, nx = result.data["shape"]
    xmin, ymin, xmax, ymax = result.data["bounds"]
    return (
        np.linspace(xmin, xmax, nx),
        np.linspace(ymin, ymax, ny),
        np.asarray(result.data["fields"]["psi"]).reshape(ny, nx),
        np.asarray(result.data["fields"]["speed"]).reshape(ny, nx),
        np.asarray(result.data["mask"], dtype=bool).reshape(ny, nx),
    )


# ------------------------------------------------------------------------ the geometry


def test_the_chord_is_the_furthest_apart_pair_of_vertices():
    profile = aero.profile_of(naca4())
    assert profile.chord == pytest.approx(1.0, abs=1e-9)
    assert profile.leading_edge[0] == pytest.approx(0.0)
    assert profile.trailing_edge[0] == pytest.approx(1.0)
    # And the quarter chord is where section data is tabulated, not the centroid of anything.
    assert profile.quarter_chord[0] == pytest.approx(0.25)


def test_the_polygon_is_reoriented_counter_clockwise_whichever_way_it_arrived():
    """Everything downstream reads `(t_y, -t_x)` as the outward normal, which is true for a
    counter-clockwise polygon and inside-out for a clockwise one. A body given the other way
    round would put every probe inside itself and flip the sign of every load."""
    points = np.asarray(naca4())
    forward = aero.profile_of(points)
    backward = aero.profile_of(points[::-1])
    for profile in (forward, backward):
        twice_area = float(
            np.sum(
                profile.points[:, 0] * np.roll(profile.points[:, 1], -1)
                - np.roll(profile.points[:, 0], -1) * profile.points[:, 1]
            )
        )
        assert twice_area > 0, "profile_of must hand back a counter-clockwise polygon"
    # Same body either way round: the chord does not depend on the winding.
    assert forward.chord == pytest.approx(backward.chord)
    np.testing.assert_allclose(forward.trailing_edge, backward.trailing_edge)


def test_the_trailing_edge_probes_straddle_the_body_at_equal_distance():
    """Equal distance is what lets the Kutta condition read a streamfunction difference as a
    surface speed; straddling is what makes it a condition about two surfaces rather than one."""
    profile = aero.profile_of(naca4())
    offset = 0.03
    first, second = aero.probe_points(profile, offset)
    apex = profile.trailing_edge
    assert np.hypot(*(first - apex)) == pytest.approx(offset)
    assert np.hypot(*(second - apex)) == pytest.approx(offset)
    # One above the chord line and one below it.
    sides = [float((probe - apex) @ profile.chord_normal) for probe in (first, second)]
    assert min(sides) < 0 < max(sides), sides


def test_a_degenerate_body_is_refused_rather_than_producing_a_number():
    with pytest.raises(aero.AerodynamicsUnavailable):
        aero.profile_of([(0.0, 0.0), (1.0, 0.0)])
    with pytest.raises(aero.AerodynamicsUnavailable):
        aero.profile_of([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])  # collinear: no area


# ------------------------------------------------- the exact half: a cylinder with lift

CYLINDER_SPEED = 1.3
CYLINDER_RADIUS = 0.4
CYLINDER_GAMMA = 1.7
"""Circulation in the textbook (clockwise-positive) sense, which is `-Gamma_ccw`."""


@pytest.fixture(scope="module")
def lifting_cylinder():
    """The exact flow plus everything derived from it, built once for the four tests below.

    A module fixture rather than a call per test: the grid is 401x401, and four rebuilds of it
    (plus four surface walks over the same 382 faces) is most of this file's runtime for no
    additional coverage.
    """
    x, y, psi, speed, mask = exact_cylinder()
    profile = horizontal_chord(cylinder(CYLINDER_RADIUS))
    surface = aero.body_surface(mask, x, y)
    cp = aero.pressure_coefficient(surface.sample(speed), CYLINDER_SPEED)
    return {
        "x": x,
        "y": y,
        "psi": psi,
        "mask": mask,
        "profile": profile,
        "surface": surface,
        "cp": cp,
        "loads": aero.surface_loads(surface, cp, profile, alpha_deg=0.0),
        # c_l = Gamma / (U a) exactly, for a cylinder of "chord" 2a.
        "exact_c_l": CYLINDER_GAMMA / (CYLINDER_SPEED * CYLINDER_RADIUS),
    }


def exact_cylinder(resolution: int = 401, half_width: float = 3.0):
    """The closed-form lifting-cylinder flow, on a grid, with no solver involved.

    ``psi = U r sin(th) (1 - a^2/r^2) + (Gamma / 2 pi) ln(r / a)`` — the doublet plus the
    vortex. Sampling an exact solution rather than solving one is what makes the assertions
    below tests of this module: a discretisation error in a Jacobi sweep cannot hide in them.
    """
    axis = np.linspace(-half_width, half_width, resolution)
    xx, yy = np.meshgrid(axis, axis)
    radius = np.hypot(xx, yy)
    radius = np.where(radius < 1e-12, 1e-12, radius)
    angle = np.arctan2(yy, xx)
    psi = CYLINDER_SPEED * radius * np.sin(angle) * (
        1 - CYLINDER_RADIUS**2 / radius**2
    ) + (CYLINDER_GAMMA / (2 * np.pi)) * np.log(radius / CYLINDER_RADIUS)
    radial = CYLINDER_SPEED * np.sin(angle) * (
        1 + CYLINDER_RADIUS**2 / radius**2
    ) + CYLINDER_GAMMA / (2 * np.pi * radius)
    tangential = CYLINDER_SPEED * np.cos(angle) * (1 - CYLINDER_RADIUS**2 / radius**2)
    inside = radius < CYLINDER_RADIUS
    return (
        axis,
        axis,
        np.where(inside, 0.0, psi),
        np.where(inside, 0.0, np.hypot(tangential, radial)),
        inside,
    )


def test_circulation_matches_the_exact_cylinder_and_does_not_depend_on_the_contour(
    lifting_cylinder,
):
    """Two properties in one, and the second is the cheaper check.

    `Gamma_ccw` is the same on every contour enclosing the body — the difference between two of
    them is the integral of `div grad psi = 0` over the annulus between. So moving the contour
    and getting the same number is an internal consistency check that needs no exact solution,
    and the exact solution then pins the sign as well as the size. Measured error here is 0.006%.
    """
    case = lifting_cylinder
    x, y, psi, mask = case["x"], case["y"], case["psi"], case["mask"]
    close = aero.circulation(psi, x, y, mask)
    far = aero.circulation(psi, x, y, mask, margin=40)
    assert close == pytest.approx(-CYLINDER_GAMMA, rel=2e-4)
    assert close == pytest.approx(far, rel=1e-3)
    # The sign is the whole point: clockwise circulation is what lifts, so a lifting body has a
    # negative counter-clockwise circulation.
    assert close < 0


def test_lift_from_circulation_and_from_surface_pressure_agree_on_a_cylinder(lifting_cylinder):
    """Kutta-Joukowski against an independent integral of the surface pressure.

    This is the per-run self-check `analyse` performs, run here where the answer is known:
    `c_l = Gamma / (U a)` exactly. Kutta-Joukowski lands within 0.01%; the surface integral
    within 2.5%, which is the staircase, and is why `c_l` is reported from the first route.
    """
    case = lifting_cylinder
    gamma = aero.circulation(case["psi"], case["x"], case["y"], case["mask"])
    from_circulation = -2.0 * gamma / (CYLINDER_SPEED * case["profile"].chord)
    assert from_circulation == pytest.approx(case["exact_c_l"], rel=1e-3)
    assert case["loads"].c_l == pytest.approx(case["exact_c_l"], rel=0.05)


def test_potential_flow_has_no_drag_and_the_integral_shows_it(lifting_cylinder):
    """d'Alembert's paradox is an identity of the model, not a numerical result — which is
    exactly why the `inviscid` assumption lists `drag` under `excludes` rather than letting a
    caller integrate the pressure and believe the answer."""
    assert lifting_cylinder["loads"].c_d == pytest.approx(0.0, abs=1e-9)


def test_the_surface_pressure_matches_the_exact_cylinder_distribution(lifting_cylinder):
    """``C_p(th) = 1 - (2 sin th + Gamma / 2 pi a U)^2`` at every face of the staircase."""
    surface, cp = lifting_cylinder["surface"], lifting_cylinder["cp"]
    angle = np.arctan2(surface.at[:, 1], surface.at[:, 0])
    expected = 1 - (
        2 * np.sin(angle)
        + CYLINDER_GAMMA / (2 * np.pi * CYLINDER_RADIUS * CYLINDER_SPEED)
    ) ** 2
    # The face midpoint is up to half a cell off the true surface, where C_p varies fast, so
    # this is an agreement in shape and magnitude rather than pointwise. Measured RMS is 0.10
    # against a distribution spanning 6.3.
    assert float(np.sqrt(np.mean((cp - expected) ** 2))) < 0.2
    assert cp.max() == pytest.approx(1.0, abs=0.02), "the stagnation point must reach C_p = 1"


def test_the_centre_of_pressure_of_a_lifting_cylinder_is_its_centre(lifting_cylinder):
    """A cylinder's resultant acts through the axis, which is mid-chord in chord fractions —
    and it is a check on the moment as well, since `x_cp` is derived from `c_m_c4`."""
    assert aero.centre_of_pressure(lifting_cylinder["loads"]) == pytest.approx(0.5, abs=0.01)


def test_a_body_with_no_resultant_has_no_centre_of_pressure_rather_than_a_large_one():
    """The dividing line worth having: `x_cp` goes to infinity as the normal force goes to
    zero, so a near-zero denominator would produce the most confidently wrong number in the
    set. Absent beats enormous."""
    assert aero.centre_of_pressure(
        aero.SurfaceLoads(c_l=0.0, c_d=0.0, c_m_c4=0.0, c_n=0.0)
    ) is None


# ------------------------------- the exact half: the Kutta condition, in the circle plane

KUTTA_RADIUS = 0.5
KUTTA_OUTER = 40.0
KUTTA_SPEED = 1.4


def _kutta_circulation(alpha_deg: float, beta_deg: float, spacing: float) -> float:
    """Solve the Kutta condition on a cylinder analytically and return `Gamma_ccw`.

    A Joukowski airfoil is the conformal image of a circle, and its Kutta condition maps to
    *"the trailing-edge image point is a stagnation point"* on that circle. So the closed-form
    result #68 cites — ``Gamma = 4 pi U R sin(alpha + beta)`` — can be checked in the circle
    plane, where both basis solutions are elementary and no mapping code is needed:

    - ``psi_A = U (r - a^2/r) sin(th - alpha)``: the stream at incidence, body at zero;
    - ``psi_G = ln(r/R) / ln(a/R)``: the circulatory mode, body at one, outer boundary at zero.

    The probes go either side of the trailing-edge point at equal arc distance and equal normal
    offset, which is what :func:`~fenixspoon.solvers.aerodynamics.probe_points` arranges on a
    real trailing edge. Feeding the analytic values means what is under test is
    :func:`~fenixspoon.solvers.aerodynamics.kutta_body_value` and nothing else.
    """
    alpha = np.radians(alpha_deg)
    trailing = -np.radians(beta_deg)
    angles = [trailing + sign * spacing / KUTTA_RADIUS for sign in (+1, -1)]
    radius = KUTTA_RADIUS + spacing
    stream = tuple(
        KUTTA_SPEED * (radius - KUTTA_RADIUS**2 / radius) * np.sin(angle - alpha)
        for angle in angles
    )
    circulatory = tuple(
        np.log(radius / KUTTA_OUTER) / np.log(KUTTA_RADIUS / KUTTA_OUTER) for _ in angles
    )
    body_value = aero.kutta_body_value(stream, circulatory)
    # Circulation of psi_A + lam psi_G about the cylinder: the stream part contributes nothing
    # and the log part contributes -2 pi lam / ln(a/R).
    return -2.0 * np.pi * body_value / np.log(KUTTA_RADIUS / KUTTA_OUTER)


@pytest.mark.parametrize(
    ("alpha", "beta"), [(0.0, 5.0), (5.0, 0.0), (4.0, 3.0), (-6.0, 8.0), (0.0, 0.0)]
)
def test_the_kutta_condition_recovers_the_joukowski_circulation(alpha, beta):
    """``Gamma = 4 pi U R sin(alpha + beta)``, the exact lifting-Joukowski answer.

    The strongest test in this file: it checks the equation that #68 exists to add, against a
    closed form, rather than checking that a solve produces a plausible number. Note that only
    the *sum* of incidence and trailing-edge angle appears — the cases above include two
    different splits of nine degrees and must return the same circulation.
    """
    exact = -4.0 * np.pi * KUTTA_SPEED * KUTTA_RADIUS * np.sin(np.radians(alpha + beta))
    got = _kutta_circulation(alpha, beta, 0.01)
    if abs(exact) < 1e-12:
        assert abs(got) < 1e-9, "no incidence and no camber is no circulation"
    else:
        assert got == pytest.approx(exact, rel=1e-3)


def test_the_kutta_condition_converges_second_order_in_the_probe_distance():
    """The probes read a surface speed as a finite difference, so the answer is exact only in
    the limit. Halving the offset must quarter the error — measured 3.5e-3 at 0.05 down to
    3.4e-5 at 0.005 — and a first-order convergence here would mean the two probes are not
    actually symmetric about the trailing edge."""
    exact = -4.0 * np.pi * KUTTA_SPEED * KUTTA_RADIUS * np.sin(np.radians(6.0))
    errors = [
        abs(_kutta_circulation(6.0, 0.0, spacing) - exact) / abs(exact)
        for spacing in (0.04, 0.02, 0.01)
    ]
    assert errors[0] > errors[1] > errors[2]
    for coarse, fine in zip(errors, errors[1:], strict=False):
        assert coarse / fine > 3.0, f"expected ~4x per halving, got {coarse / fine:.2f}"


def test_a_circulatory_mode_that_says_nothing_is_refused_not_divided_by():
    """`psi_G` equal to its body value at both probes leaves the condition with no unknown to
    determine — which happens when the probes are inside the body. A zero lift from a division
    that nearly failed is precisely the answer #68 was filed about."""
    with pytest.raises(aero.AerodynamicsUnavailable, match="determines nothing"):
        aero.kutta_body_value((0.1, -0.1), (1.0, 1.0))


# ------------------------------------- the approximate half: the solver, against theory


def test_a_symmetric_section_has_no_lift_at_zero_incidence():
    result = solve(naca4(), resolution=120, iterations=5000, alpha=0.0)
    assert result.metrics["c_l"] == pytest.approx(0.0, abs=0.01)
    assert result.metrics["circulation"] == pytest.approx(0.0, abs=0.01)


def test_lift_is_antisymmetric_in_incidence_for_a_symmetric_section():
    """A property of the *body*, so it holds whatever the discretisation does to the magnitude —
    which makes it a check the grid cannot fake."""
    up = solve(naca4(), resolution=120, iterations=5000, alpha=4.0).metrics["c_l"]
    down = solve(naca4(), resolution=120, iterations=5000, alpha=-4.0).metrics["c_l"]
    assert up > 0 > down
    assert up == pytest.approx(-down, rel=1e-6)


def test_the_lift_curve_slope_is_near_two_pi_per_radian():
    """Thin-airfoil theory gives ``dc_l/dalpha = 2 pi``, exactly, for a zero-thickness section.

    A 12% thick section is genuinely a little stiffer than that in inviscid theory, and a
    first-order Cartesian discretisation adds its own error, so this is a band. Measured 6.84
    against 6.283 — 8.8% high — and the band below is wide enough for that and narrow enough
    that losing the Kutta condition, or getting the chord wrong, fails it.
    """
    angles = [-4.0, -2.0, 0.0, 2.0, 4.0]
    lifts = [
        solve(naca4(), resolution=120, iterations=5000, alpha=alpha).metrics["c_l"]
        for alpha in angles
    ]
    slope = float(np.polyfit(np.radians(angles), lifts, 1)[0])
    assert 0.85 * 2 * np.pi < slope < 1.25 * 2 * np.pi, f"dc_l/dalpha = {slope:.3f}/rad"


def test_a_cambered_section_lifts_at_zero_incidence_and_pitches_nose_down():
    """NACA 2412, whose numbers are in every table: zero-lift angle about -2.1 degrees and
    ``c_m,c/4`` about -0.053. Measured here: -2.34 degrees and -0.057.

    The moment's *sign* is the part worth guarding. `c_m_c4` is reported nose-up positive, which
    is the opposite sense to a right-hand moment about +z, and getting that backwards would
    produce a section that pitched the wrong way with nothing else looking wrong.
    """
    section = naca4(camber=0.02, position=0.4)
    level = solve(section, resolution=160, iterations=8000, alpha=0.0).metrics
    assert level["c_l"] > 0.15, "camber must lift at zero incidence"
    assert -0.10 < level["c_m_c4"] < -0.02, "a cambered section pitches nose-down"
    # And the centre of pressure sits aft of the quarter chord at low lift, as it must.
    assert level["x_cp"] > 0.25

    below = solve(section, resolution=160, iterations=8000, alpha=-3.0).metrics["c_l"]
    above = solve(section, resolution=160, iterations=8000, alpha=-1.0).metrics["c_l"]
    assert below < 0 < above, "the zero-lift angle must lie between -3 and -1 degrees"


def test_the_two_routes_to_lift_agree_within_the_tolerance_that_triggers_a_warning():
    """The per-run self-check #68 asked for: Kutta-Joukowski against the surface-pressure
    integral. They are independent computations of one quantity, so their difference measures
    the discretisation. Measured 11-16% on ordinary runs, which is why the warning threshold is
    25% — and why an ordinary run must not trip it."""
    result = solve(naca4(), resolution=160, iterations=8000, alpha=5.0)
    x, y, _, speed, mask = grid_of(result)
    profile = aero.profile_of(naca4())
    surface = aero.body_surface(mask, x, y)
    cp = aero.pressure_coefficient(surface.sample(speed), 1.0)
    from_pressure = aero.surface_loads(surface, cp, profile, alpha_deg=5.0).c_l
    from_circulation = result.metrics["c_l"]
    disagreement = abs(from_circulation - from_pressure) / abs(from_circulation)
    assert disagreement < aero.LIFT_AGREEMENT_TOLERANCE
    assert not [w for w in result.warnings if "disagree" in w]


def test_without_the_kutta_condition_the_lift_metrics_are_absent_and_the_result_says_why():
    """The other half of #68: if the toolkit is going to ship a mode with no meaningful
    circulation, it has to *say so* rather than return a zero. Absent metrics plus a warning
    naming them, not a plausible number."""
    result = solve(naca4(), resolution=100, iterations=2000, alpha=5.0, kutta=False)
    for absent in ("circulation", "c_l", "c_m_c4", "x_cp"):
        assert absent not in result.metrics
    assert result.metrics["cp_min"] < 0, "the flow field is still perfectly usable"
    assert result.series == [], "no circulation means no surface pressure distribution"
    assert any("no Kutta condition" in warning for warning in result.warnings)


def test_the_kutta_path_costs_two_solves_and_reports_them_as_one_progress_stream():
    events: list = []
    context = SolverContext(progress_cb=events.append)
    geometry = Domain2D(
        bounds=(-1.5, -1.5, 2.5, 1.5),
        obstacle=Polygon2D(points=[tuple(p) for p in naca4()]),
    )
    params = MockLaplace2D.Params(
        resolution=48, iterations=200, report_every=50, write_vtk=False
    )
    result = MockLaplace2D().solve(geometry, params, context)
    assert result.stats["iterations"] == pytest.approx(400), "two relaxations of 200 sweeps"
    assert [event.total for event in events] == [400] * len(events)
    assert [event.iteration for event in events] == sorted(
        event.iteration for event in events
    ), "a progress stream must not go backwards across the two solves"


# ---------------------------------------------------- the surface distribution as a series


def test_the_surface_pressure_comes_back_as_a_series_with_a_per_trace_abscissa():
    """#69's shape, produced by #68's arithmetic — the case the two issues share.

    Per-trace `x` rather than one shared abscissa because a staircase does not put a face at
    the same `x/c` on both surfaces, and resampling one side onto the other's stations would be
    inventing data.
    """
    result = solve(naca4(camber=0.02), resolution=120, iterations=5000, alpha=4.0)
    assert [entry.name for entry in result.series] == ["surface_cp"]
    curves = result.series[0]
    assert curves.x is None, "the two surfaces are sampled differently"
    assert {trace.name for trace in curves.traces} == {"cp_upper", "cp_lower"}
    for trace in curves.traces:
        assert trace.unit == "1" and trace.x is not None
        assert trace.x.name == "x/c" and trace.x.unit == "1"
        assert len(trace.values) == len(trace.x.values)
        assert trace.x.values == sorted(trace.x.values), "drawable order"
        assert -0.05 < min(trace.x.values) and max(trace.x.values) < 1.05
        assert max(trace.values) <= 1.0 + 1e-9, "C_p cannot exceed the stagnation value"
    upper = next(t for t in curves.traces if t.name == "cp_upper")
    lower = next(t for t in curves.traces if t.name == "cp_lower")
    assert min(upper.values) < min(lower.values), "a lifting section sucks harder on top"


def test_a_curve_stays_inside_the_series_ceilings():
    """The surface of a body on a 512-point grid has more faces than a plot can show, so the
    curve is decimated. Without that, a fine solve would push a legal-looking series past the
    point where it stops being compact."""
    result = solve(naca4(), resolution=360, iterations=1500, alpha=3.0)
    for trace in result.series[0].traces:
        assert len(trace.values) <= aero.MAX_SURFACE_SAMPLES


# -------------------------------------------------------------------- degenerate domains


def test_a_body_that_crowds_the_domain_edge_is_reported_not_guessed():
    """There is nowhere to draw a circulation contour, so there is no circulation to report.

    The adapter keeps the flow field — it is a valid potential flow, only the *choice* of
    circulation failed — and says in a warning which metrics went missing and why. Failing the
    whole job would throw away a usable result over a post-processing step; reporting zero
    would be the bug this issue is about.
    """
    context = SolverContext(progress_cb=lambda event: None)
    geometry = Domain2D(
        bounds=(-0.1, -0.2, 1.1, 0.2),
        obstacle=Polygon2D(points=[tuple(p) for p in naca4()]),
    )
    result = MockLaplace2D().solve(
        geometry,
        MockLaplace2D.Params(resolution=20, iterations=200, write_vtk=False),
        context,
    )
    assert "c_l" not in result.metrics
    assert result.data["fields"]["psi"], "the flow field survives"
    assert any("no circulation could be determined" in w for w in result.warnings)
    assert any("c_l" in w for w in result.warnings), "the warning must name what is missing"
