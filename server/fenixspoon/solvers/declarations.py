"""Capability declarations shared between the adapter pairs (roadmap M2.5, issue #43).

Every physics in the gallery ships twice — a NumPy stand-in and a FEniCSx solve — and the
two are [cross-validated against each other](../../../docs/gallery.md) precisely because
they are meant to answer the *same* question. That has a consequence for discovery: a
caller that switches `mock.heat2d` for `dolfinx.heat2d` must find the same metric names, or
the pair is only interchangeable in principle.

So the metric sets live here once and both adapters point at them, the way the heat pair
already shares its parameter names and prose. `test_paired_adapters_declare_the_same_metrics`
fails if a new adapter for an existing physics declares its own vocabulary instead.

These are declarations; the values come back with the result. A metric that names a `field`
and a `reduction` is computed generically after the solve (`fill_declared_metrics`), so no
adapter writes `float(T.max())` twice; the ones with neither are the adapter's own, because
they need a parameter and a parameter is not in the result payload.
"""

from .base import ArtifactSpec, Assumption, MetricSpec

#: Every capability in this repository is a cross-section of a body infinitely long in z, and
#: not one of them said so before #70. Shared rather than repeated per physics because it is
#: the same statement in each: the third dimension is not modelled, so nothing about end
#: effects, finite span or three-dimensional flow is in the answer.
TWO_DIMENSIONAL = Assumption(
    name="two_dimensional",
    statement=(
        "A cross-section of a body infinitely long in z. Results are per unit depth and no "
        "end effect, finite span or out-of-plane variation is modelled."
    ),
    excludes=["end_effects", "span_efficiency", "three_dimensional_flow"],
)

#: The legacy-VTK dump every adapter can attach. One spec because it is one file with one
#: meaning, and all four adapters gate it behind a param called `write_vtk`.
VTK_ARTIFACT = ArtifactSpec(
    name="solution.vtk",
    content_type="model/vnd.vtk",
    description=(
        "The full field as a legacy-VTK file, openable directly in ParaView. This is how a "
        "caller gets the numeric arrays that the compact result levels leave out."
    ),
    when="write_vtk",
)

#: Potential flow: what an aerodynamicist reads off a streamfunction solve.
POTENTIAL_FLOW_METRICS = [
    MetricSpec(
        name="speed_max",
        unit="m/s",
        description="Peak local flow speed, which is where the profile accelerates the flow most.",
        field="speed",
        reduction="max",
    ),
    MetricSpec(
        name="cp_min",
        unit="1",
        description=(
            "Minimum pressure coefficient, 1 - (speed_max / u_inf)^2. The suction peak: how "
            "hard this shape pulls, and the first number a profile change is judged on."
        ),
        # Not a field reduction: it is a function of `speed_max` and the `u_inf` param, so
        # there is no array to take a max of. Declared with `field` null rather than
        # pretending `speed` reduces to it.
    ),
    # The four below exist because of #68, and they are the reason it was filed: a
    # potential-flow capability whose most famous output was silently zero. They arrive only
    # with the Kutta condition in force — without it there is no circulation to report, which
    # is what the `no_circulation` assumption's `excludes` list says.
    MetricSpec(
        name="circulation",
        unit="m^2/s",
        description=(
            "Circulation about the body, counter-clockwise positive, from a contour integral "
            "of the velocity. The single quantity lift depends on: L' = -rho * u_inf * "
            "circulation, so a negative value here is positive lift."
        ),
        boundary="body",
    ),
    MetricSpec(
        name="c_l",
        unit="1",
        description=(
            "Lift coefficient, -2 * circulation / (u_inf * chord) by Kutta-Joukowski. The "
            "number a section is chosen on. Checked every run against an independent integral "
            "of the surface pressure; a disagreement appears in `warnings`."
        ),
        boundary="body",
    ),
    MetricSpec(
        name="c_m_c4",
        unit="1",
        description=(
            "Pitching moment about the quarter chord, nose-up positive — so a cambered "
            "section reads negative, as tabulated section data does. From the surface pressure "
            "integral, which is the less accurate of this adapter's two routes to a load."
        ),
        boundary="body",
    ),
    MetricSpec(
        name="x_cp",
        unit="1",
        description=(
            "Centre of pressure as a fraction of chord aft of the leading edge, where the "
            "resultant normal force acts. Absent when that force is too near zero to locate "
            "one — an uncambered section at zero incidence has no centre of pressure."
        ),
        boundary="body",
    ),
]

#: Potential flow: what the model leaves out, which for this physics is most of a drag polar.
#:
#: `inviscid` is the load-bearing one. Potential flow has no boundary layer, so there is no
#: wall shear, no separation and no stall — d'Alembert's paradox is not an approximation here
#: but an identity, and a caller integrating the returned pressure gets *exactly* zero drag.
#: Saying so in `excludes` is what turns "can this tell me about drag?" from a plausible zero
#: into a definite no.
#:
#: `kutta_condition` and `no_circulation` are the two halves of the same switch, declared
#: separately because a caller filters on names and "the circulation is a modelling choice"
#: reads very differently from "there is none". Both carry `when` and `when_value`, so a caller
#: can see which applies to the run it is about to submit — and one that has not chosen yet sees
#: both, which is the honest answer to "what might this assume".
POTENTIAL_FLOW_ASSUMPTIONS = [
    Assumption(
        name="incompressible",
        statement=(
            "Density is constant. Valid below roughly Mach 0.3; past that the pressure "
            "coefficient is wrong by more than the compressibility correction it omits."
        ),
        quantity="mach",
        limit=0.3,
        comparator="<",
    ),
    Assumption(
        name="inviscid",
        statement=(
            "No boundary layer, so no wall shear, no separation and no stall. Pressure drag "
            "integrates to exactly zero (d'Alembert), which is an identity of the model "
            "rather than a numerical result — the suction over the forebody is cancelled by "
            "pressure recovery behind it."
        ),
        excludes=["drag", "c_d", "skin_friction", "stall", "alpha_stall", "separation"],
    ),
    Assumption(
        name="irrotational",
        statement=(
            "Vorticity is zero everywhere in the fluid; the entire circulation of the flow is "
            "the bound vortex on the body. No wake is modelled, so nothing here describes "
            "unsteady shedding or the influence of one body on another's wake."
        ),
        excludes=["wake", "shedding_frequency"],
    ),
    Assumption(
        name="kutta_condition",
        statement=(
            "Circulation is fixed by requiring equal surface speed on either side of the "
            "trailing edge, which is what makes lift finite and unique. It assumes a sharp "
            "trailing edge and attached flow, so it is a poor model at high incidence — "
            "where a real section has separated and this model cannot know."
        ),
        when="kutta",
    ),
    Assumption(
        name="no_circulation",
        statement=(
            "With `kutta` false the streamfunction is set to an arbitrary constant on the "
            "body, so the solution carries no meaningful circulation and lift is essentially "
            "zero at every incidence — not approximately zero, and not a physical result. "
            "Use it to look at the flow field, never to read a lift coefficient."
        ),
        excludes=["lift", "c_l", "circulation", "c_m_c4", "x_cp"],
        when="kutta",
        when_value=False,
    ),
    TWO_DIMENSIONAL,
]

#: Magnetostatics: saturation first, then the flux the winding actually links.
MAGNETOSTATICS_METRICS = [
    MetricSpec(
        name="b_max",
        unit="T",
        description=(
            "Peak flux density anywhere in the model. Compared against the iron's saturation "
            "point — past roughly 1.5 T for common steels the permeability collapses and a "
            "linear solve stops describing the device."
        ),
        field="B",
        reduction="max",
    ),
    MetricSpec(
        name="a_max",
        unit="Wb/m",
        description=(
            "Peak vector potential. With A = 0 on the outer boundary this is the flux per "
            "unit depth between the peak and that boundary, so it scales with flux linkage."
        ),
        field="A",
        reduction="max",
    ),
]

#: Magnetostatics: linear material, and the number that says when that stops being true.
#:
#: `b_max`'s description already said the honest thing — "past roughly 1.5 T for common steels
#: the permeability collapses and a linear solve stops describing the device" — but it said it
#: as prose inside a metric, so a caller could not act on it and it was only there because
#: someone happened to write it in that one place. Here it is the same sentence with the
#: number in a field, against the metric the run already reports, which is what makes it
#: checkable rather than readable.
MAGNETOSTATICS_ASSUMPTIONS = [
    Assumption(
        name="linear_material",
        statement=(
            "Permeability is constant per region: B = mu_r * mu_0 * H with no B-H curve. Past "
            "roughly 1.5 T for common steels the iron saturates, the permeability collapses, "
            "and a linear solve over-predicts the flux — compare the reported `b_max` against "
            "the saturation point of the material actually intended."
        ),
        quantity="b_max",
        limit=1.5,
        comparator="<",
        excludes=["saturation", "incremental_permeability"],
    ),
    Assumption(
        name="magnetostatic",
        statement=(
            "Steady currents only: no time derivative, so no eddy currents, no skin effect "
            "and no hysteresis or core loss. A device driven at frequency needs a different "
            "model, not a finer mesh of this one."
        ),
        excludes=["eddy_current", "core_loss", "hysteresis", "inductance_at_frequency"],
    ),
    TWO_DIMENSIONAL,
]

#: Heat conduction: the assumption that makes `t_max` an equilibrium rather than an answer.
#:
#: The steady one matters most because the wrong reading of it is silent: a caller asking about
#: a thermal transient gets a plausible number that answers a different question. Nothing
#: about the payload says the number is an asymptote.
HEAT_ASSUMPTIONS = [
    Assumption(
        name="steady_state",
        statement=(
            "No time derivative: every temperature reported is the equilibrium the device "
            "settles at, reached after a time this model cannot tell you. A transient — "
            "warm-up, a duty cycle, a thermal time constant — is a different problem."
        ),
        excludes=["thermal_time_constant", "transient_temperature", "duty_cycle_rise"],
    ),
    Assumption(
        name="constant_properties",
        statement=(
            "Conductivity is constant per region and independent of temperature. Good for "
            "metals over a modest range; poor across a phase change or for a material whose "
            "k varies by tens of percent over the range the solve spans."
        ),
    ),
    Assumption(
        name="convection_coefficient",
        statement=(
            "The fluid is not solved. It enters as a single coefficient `h` on exposed solid "
            "faces, which is how heat sinks are analysed — but it means the coefficient is an "
            "input rather than a result, and no flow field, buoyancy pattern or fin-to-fin "
            "interference is modelled."
        ),
        excludes=["flow_field", "buoyancy", "local_heat_transfer_coefficient"],
    ),
    Assumption(
        name="no_radiation",
        statement=(
            "Radiative exchange is omitted entirely. Negligible for a fan-cooled sink near "
            "ambient; not negligible for a hot surface in still air, where radiation can "
            "carry a comparable share of the load and this model would over-predict the rise."
        ),
        excludes=["emissivity", "radiative_flux"],
    ),
    TWO_DIMENSIONAL,
]

#: Heat conduction: the two numbers a heat-sink study exists to produce, plus the flux.
#:
#: `t_max` and `t_rise` used to be `stats` keys on both adapters, which conflated what the
#: solve cost with what it answered. Protocol 1.3 moved them here (#46), and the heat-sink
#: demo reads them from `metrics` now.
HEAT_METRICS = [
    MetricSpec(
        name="t_max",
        unit="degC",
        description=(
            "Peak temperature in the solid — the number a component's rating is read against."
        ),
        field="T",
        reduction="max",
    ),
    MetricSpec(
        name="t_rise",
        unit="K",
        description=(
            "Peak temperature above ambient, t_max - t_ambient. The design figure, because "
            "it is the part of t_max the geometry controls."
        ),
        # Depends on the `t_ambient` param as well as the field, so it is not a reduction.
    ),
    MetricSpec(
        name="flux_max",
        unit="W/m^2",
        description=(
            "Peak conductive heat flux magnitude, k|grad T|. Where the metal is working "
            "hardest, which is where a fin is worth thickening."
        ),
        field="flux",
        reduction="max",
    ),
]


#: Linear elasticity: what a structural check reads off a static solve (#81).
#:
#: `sigma_vm_max` is the load-bearing one, and the one whose declaration has to be honest: it
#: is a *nodal-averaged* peak, so a coarse mesh under-reports it, and the very place it is
#: read — a re-entrant corner or a hole edge — is where the discretization error is largest.
#: That is not a caveat this pair can hide behind a number, so it is in the description.
ELASTICITY_METRICS = [
    MetricSpec(
        name="u_max",
        unit="m",
        description=(
            "Largest displacement magnitude anywhere in the body. The stiffness number: what "
            "a deflection limit is checked against."
        ),
        field="u_mag",
        reduction="max",
    ),
    MetricSpec(
        name="sigma_vm_max",
        unit="Pa",
        description=(
            "Peak von Mises equivalent stress, compared against a yield strength to decide "
            "whether the part holds. Averaged from the elements to the nodes, so a coarse "
            "mesh under-reports it — and a stress concentration is exactly where that error "
            "is worst. Refine before quoting it."
        ),
        field="sigma_vm",
        reduction="max",
    ),
    MetricSpec(
        name="compliance",
        unit="J/m",
        description=(
            "External work done by the applied traction, f·u, per unit depth. The scalar a "
            "stiffness optimisation minimises — and, unlike a peak, a global quantity that "
            "converges quickly with mesh refinement."
        ),
        # Needs the load vector, which is not in the result payload, so the adapter computes it.
    ),
]

#: Linear elasticity: what the model leaves out. Four of these are the standard linear-static
#: envelope; the fifth is a limitation of *this implementation* rather than of the physics, and
#: it is declared for the same reason as the rest — a caller should not have to read the source
#: to discover that a boundary condition can only be placed on an edge of the bounding box.
ELASTICITY_ASSUMPTIONS = [
    Assumption(
        name="linear_elastic",
        statement=(
            "Stress is proportional to strain everywhere, with no yield surface. Past the "
            "material's proof stress the reported numbers describe a body that would in fact "
            "have deformed permanently, and they overstate the stress rather than redistribute "
            "it. Compare `sigma_vm_max` against a yield strength yourself — nothing here does."
        ),
        excludes=["plastic_strain", "residual_stress", "limit_load"],
    ),
    Assumption(
        name="small_strain",
        statement=(
            "Equilibrium is written on the undeformed shape and strain is the linearised "
            "measure, so the solve cannot know that a deflected structure carries load "
            "differently. Buckling in particular is invisible: a slender member in compression "
            "returns a perfectly reasonable stress right through the load that would collapse it."
        ),
        excludes=["buckling_load", "large_rotation", "geometric_stiffening"],
    ),
    Assumption(
        name="static",
        statement=(
            "No inertia and no time: the answer is the equilibrium reached under a load applied "
            "slowly. Natural frequencies, impact and any dynamic amplification are outside it."
        ),
        excludes=["natural_frequency", "transient_response", "dynamic_amplification"],
    ),
    Assumption(
        name="plane_idealisation",
        statement=(
            "Either plane stress (a thin plate loaded in its plane, out-of-plane stress zero) "
            "or plane strain (a long prismatic body, out-of-plane strain zero), chosen by the "
            "`plane` parameter. Neither is a thin *bending* plate, and the two differ by terms "
            "of order nu in every stress — picking the wrong one is a quiet error, not a "
            "refusal."
        ),
        excludes=["plate_bending", "out_of_plane_load"],
    ),
    Assumption(
        name="edge_aligned_boundary_conditions",
        statement=(
            "The clamped and loaded boundaries are named as edges of the bounding rectangle "
            "(`xmin`, `xmax`, `ymin`, `ymax`) and the traction is uniform along the loaded "
            "one. A load on part of an edge, on the hole boundary, or on an arbitrary curve "
            "cannot be expressed — no geometry kind here can name such a boundary, which is "
            "the open design question in issue #81."
        ),
        excludes=["partial_edge_load", "hole_boundary_load", "point_load"],
    ),
    TWO_DIMENSIONAL,
]
