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

from .base import ArtifactSpec, Assumption, ConditionSpec, ConvergenceSpec, MetricSpec

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

#: The other way to be two-dimensional, and it arrived with `axisymmetric2d` (#100).
#:
#: Deliberately spelled `two_dimensional` like its sibling above, because it answers the same
#: question — *what is the third dimension doing?* — and a caller filtering on the name should
#: find an answer whichever reduction a capability made. What it says is the opposite of the
#: prism, and the difference is not cosmetic: results are for the **whole revolved body**
#: rather than per unit depth, so a capacitance comes back in farads and not farads per metre.
AXISYMMETRIC = Assumption(
    name="two_dimensional",
    statement=(
        "A meridian (r, z) half-section of a body of revolution. Nothing varies with the "
        "azimuthal angle, and every reported quantity is for the whole revolved body rather "
        "than per unit depth. A feature that breaks the symmetry — a slot, a lead-out, a "
        "tilt between the two bodies — is not modelled and cannot be approximated by "
        "refining this one."
    ),
    excludes=["azimuthal_variation", "tilt", "non_axisymmetric_features"],
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

#: Axisymmetric electrostatics: the capacitance, and the two numbers it is built from (#100).
#:
#: `capacitance` is the one a sensor is designed against, and it is the adapter's own rather
#: than a declared reduction for a reason worth stating: it is `2W/V²`, and the `V` is a
#: property of the *geometry* — the potential difference between the electrodes — which is
#: nowhere in the result payload. Neither is the energy, which is an integral over the mesh
#: rather than a reduction of a nodal array. `e_max` is a genuine reduction and is declared
#: as one, which is also what makes the breakdown assumption below checkable.
ELECTROSTATICS_METRICS = [
    MetricSpec(
        name="capacitance",
        unit="F",
        description=(
            "Capacitance between the two electrodes, from the field energy: C = 2W/V². In "
            "farads for the whole body of revolution — not per unit depth, which is what the "
            "axisymmetric reduction buys. Absent when the model holds fewer than two distinct "
            "potentials, since there is then no pair for a capacitance to be between."
        ),
    ),
    MetricSpec(
        name="energy",
        unit="J",
        description=(
            "Electrostatic field energy, W = ½∫ε|∇V|² dV over the revolved volume. The "
            "quantity the capacitance is read off, reported beside it so a caller can see "
            "what it was divided by."
        ),
    ),
    MetricSpec(
        name="e_max",
        unit="V/m",
        description=(
            "Peak field magnitude anywhere in the model, which is where a dielectric breaks "
            "down first. Read it at an electrode edge with suspicion: a sharp corner has an "
            "unbounded field in the continuum, so this number keeps rising as the mesh is "
            "refined and describes the corner's sharpness as much as the design's."
        ),
        field="E",
        reduction="max",
    ),
]

#: Axisymmetric electrostatics: what the model assumes, and the one number that says when it
#: stops being true.
#:
#: `no_breakdown` follows `linear_material`'s pattern from the magnetostatics set: a limit
#: against a metric the run already reports, so "is this design in trouble?" is checkable
#: rather than merely readable. The limit is dry air at atmospheric pressure — the wrong
#: number for a vacuum gap or for a solid dielectric, which is why the statement names it.
ELECTROSTATICS_ASSUMPTIONS = [
    Assumption(
        name="electrostatic",
        statement=(
            "No time derivative anywhere: charges are at rest and the field is the gradient "
            "of a potential. Nothing here describes a displacement current, a dielectric "
            "relaxation time, or the loss tangent that makes a real capacitor dissipate at "
            "frequency — a device driven fast enough for those to matter needs a different "
            "model, not a finer mesh of this one."
        ),
        excludes=["loss_tangent", "displacement_current", "resonant_frequency"],
    ),
    Assumption(
        name="no_space_charge",
        statement=(
            "The dielectric carries no free charge: the equation solved is div(ε grad V) = 0. "
            "Trapped charge in an insulator, an ionised gap and a semiconductor depletion "
            "region all violate this, and each would shift the potential distribution rather "
            "than merely scale it."
        ),
        excludes=["space_charge", "charge_injection"],
    ),
    Assumption(
        name="linear_dielectric",
        statement=(
            "Permittivity is constant per region: D = ε_r ε_0 E, with no field dependence, no "
            "anisotropy and no hysteresis. Good for air, glass and most polymers; poor for a "
            "ferroelectric, whose ε_r varies by a factor over the range a device sees."
        ),
        excludes=["ferroelectric_hysteresis", "anisotropic_permittivity"],
    ),
    Assumption(
        name="ideal_conductors",
        statement=(
            "An electrode is a perfect equipotential with no resistance and no surface "
            "structure. The capacitance is therefore geometric, and nothing here describes "
            "electrode resistance, contact potential, or the roughness of a coating."
        ),
        excludes=["electrode_resistance", "contact_potential", "surface_roughness"],
    ),
    Assumption(
        name="no_breakdown",
        statement=(
            "The dielectric is assumed to hold. The solve is linear and will happily report a "
            "field above the strength of the material actually between the electrodes — "
            "compare `e_max` against it. The declared limit is dry air at atmospheric "
            "pressure, roughly 3 MV/m; a vacuum gap or a solid dielectric holds far more, and "
            "a sub-millimetre air gap holds more too (Paschen), so treat it as the "
            "conservative case rather than the answer."
        ),
        quantity="e_max",
        limit=3.0e6,
        comparator="<",
        excludes=["breakdown_voltage", "partial_discharge", "corona"],
    ),
    Assumption(
        name="truncated_domain",
        statement=(
            "The modelled window is closed by a zero-flux condition wherever no potential is "
            "imposed, so no field crosses the truncation. That is exact on the axis of "
            "revolution and on a symmetry plane, and an approximation everywhere else: "
            "capacitance to anything outside the window — a case, a nearby ground, the rest "
            "of the instrument — is not in the answer. Move the boundary out and watch the "
            "number settle, which is the only check this model offers on it."
        ),
        excludes=["stray_capacitance", "capacitance_to_ground"],
    ),
    AXISYMMETRIC,
]

#: What an electrostatics load case may say on a named boundary (#100).
#:
#: One key, because one is what the physics needs: an electrode surface at a potential. The
#: other half of the pair — a *region* held at a potential by its `voltage` material key — is
#: not a condition, and the difference is real rather than a spelling: a region is a metal
#: body in the section, a boundary is a face of the modelled window, and the sensor's two
#: electrodes are the first while its symmetry planes are the second.
ELECTROSTATICS_CONDITIONS = [
    ConditionSpec(
        name="voltage",
        unit="V",
        description=(
            "Potential held on this boundary, in volts. With no potential imposed anywhere "
            "the problem is singular — a field is defined by differences, so something must "
            "pin one — and the solve says so rather than returning an arbitrary constant."
        ),
        kind="dirichlet",
    ),
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
            "k varies by tens of percent over the range the solve spans. The capability that "
            "removes this assumption is `heat_nonlinear2d`, which reports `k_ratio` — the "
            "spread of the solved conductivity — so a run can tell you whether it mattered."
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


#: One stored instant of a transient, written once per saved step (#86). Declared with
#: `{index}` because the count is a parameter, not a property of the capability — the whole
#: reason `ArtifactSpec.name` learned a pattern.
FRAME_ARTIFACT = ArtifactSpec(
    name="frame_{index}.vtk",
    content_type="model/vnd.vtk",
    description=(
        "One instant of the field, as legacy VTK. These are the result's `frames`: the "
        "time index lists them with the instant each holds, and a caller fetches the one it "
        "is looking at rather than receiving the history."
    ),
    when="save_every",
)

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
            "Without a load case, the clamped and loaded boundaries are named as edges of "
            "the bounding rectangle (`xmin`, `xmax`, `ymin`, `ymax`) by the `fixed_edge` "
            "and `load_edge` parameters, and the traction is uniform along the loaded one. "
            "**A load case lifts this**: conditions supplied for the geometry's named "
            "boundaries (#85) reach part of an edge, the hole boundary or an arbitrary "
            "outline, and they replace the two parameters entirely rather than adding to "
            "them. What neither route expresses is a concentrated force: a traction is per "
            "unit length, so a point load has to be approximated by a short loaded stretch."
        ),
        # Narrowed rather than deleted, as #85 asked. `partial_edge_load` and
        # `hole_boundary_load` left the list because a load case genuinely reaches them and
        # a caller filtering on `excludes` would otherwise be told no about something the
        # server will do. `point_load` stayed because neither route offers it.
        #
        # Stated in prose rather than through `when`, and that is a real limit worth naming:
        # `Assumption.when` gates on a *parameter*, and "a load case was supplied" is not a
        # parameter — it arrives beside the params rather than inside them. So a caller
        # reading this statically cannot be told which of the two regimes it is in, and the
        # statement has to describe both.
        excludes=["point_load"],
    ),
    TWO_DIMENSIONAL,
]


#: What an elasticity load case may say on a named boundary (#85).
#:
#: Shared by the pair for the same reason the metrics are: a caller that swaps
#: `mock.elasticity2d` for `dolfinx.elasticity2d` must find the same vocabulary, or the two
#: are interchangeable only in principle. `test_paired_adapters_declare_the_same_conditions`
#: fails if a new adapter for an existing physics invents its own spelling.
#:
#: Deliberately small. Five keys cover clamped, rolled and loaded — the restraints and loads
#: a plane problem actually needs — and each new one has to earn its place by being something
#: an adapter can honour on *any* selected boundary, not just on an edge of the bounding box.
ELASTICITY_CONDITIONS = [
    ConditionSpec(
        name="fixed",
        unit="1",
        description=(
            "Nonzero clamps this boundary in both directions. The usual restraint, and the "
            "one a cantilever's root needs: with nothing fixed anywhere the problem is "
            "singular and the solve reports a rigid-body motion rather than a deflection."
        ),
        kind="dirichlet",
    ),
    ConditionSpec(
        name="fixed_x",
        unit="1",
        description=(
            "Nonzero holds x while leaving y free — a roller, and the honest way to model a "
            "symmetry plane normal to x. Clamping such a plane in both directions instead "
            "would suppress the Poisson contraction along it and overstate the stiffness."
        ),
        kind="dirichlet",
    ),
    ConditionSpec(
        name="fixed_y",
        unit="1",
        description="Nonzero holds y while leaving x free; the other roller.",
        kind="dirichlet",
    ),
    ConditionSpec(
        name="traction_x",
        unit="Pa",
        description=(
            "Uniform surface traction along x on this boundary, force per unit area, "
            "positive towards +x. Per unit depth in z like everything else here."
        ),
        kind="neumann",
    ),
    ConditionSpec(
        name="traction_y",
        unit="Pa",
        description="Uniform surface traction along y, positive towards +y.",
        kind="neumann",
    ),
]


#: One stored mode shape, written once per computed mode (#101). The modal twin of
#: :data:`FRAME_ARTIFACT`, declared with `{index}` for the same reason: the count is a
#: parameter, not a property of the capability.
MODE_ARTIFACT = ArtifactSpec(
    name="mode_{index}.vtk",
    content_type="model/vnd.vtk",
    description=(
        "One mode shape, as legacy VTK. These are the result's `modes`: the index lists them "
        "with the mode number each holds, and a caller fetches the one it wants to look at "
        "rather than receiving the whole modal basis. Amplitudes are mass-normalised, so a "
        "shape is comparable between modes and means nothing on its own."
    ),
    when="write_vtk",
)

#: Modal analysis: where it resonates, and the count that says whether to believe it (#101).
#:
#: `f_1` is the number a design is judged on — *the first elastic frequency*, which for a
#: free-free structure is not the first frequency. That distinction is the reason
#: `rigid_body_modes` is a declared metric rather than a diagnostic: it is how a caller reads
#: the spectrum correctly, and it doubles as the check that the solve is sound. A free-free
#: plane structure has exactly three (two translations and a rotation); four means the
#: eigensolver found a spurious null mode, two means a restraint the caller did not intend.
#:
#: Both are `over="run"`: neither is a reduction of the payload, which carries one mode shape.
#: The spectrum itself is a *series*, not a metric — that is what #69 built curves for, and
#: "where does it resonate" is the row of its table that had no producer until this pair.
MODAL_METRICS = [
    MetricSpec(
        name="f_1",
        unit="Hz",
        over="run",
        description=(
            "Lowest **elastic** natural frequency, in hertz: the first mode that deforms the "
            "structure, with rigid-body modes excluded. The number a design is checked "
            "against, because a structure driven near it responds far beyond the static "
            "deflection an equilibrium solve reports."
        ),
    ),
    MetricSpec(
        name="rigid_body_modes",
        unit="1",
        over="run",
        description=(
            "How many modes came back at essentially zero frequency — motions that cost no "
            "strain energy. **Three is correct for an unrestrained plane structure** (two "
            "translations and a rotation) and zero for a fully restrained one; anything else "
            "means the model is not the one intended, and it is reported rather than "
            "filtered so that a caller can see it."
        ),
    ),
]

#: Modal analysis: what an eigensolve assumes, and the two that most often surprise.
#:
#: `undamped` is the one to read. It is not a small approximation of a damped structure — it
#: is a different question: the frequencies are right to order zeta^2 for anything lightly
#: damped, and the *amplitude at resonance*, which is what a caller usually wants next, is
#: not in this answer at all and cannot be recovered from it.
#:
#: `discretised_mass` is here because the pair converges from **opposite sides**, and a caller
#: comparing them should know why before concluding one is wrong.
MODAL_ASSUMPTIONS = [
    Assumption(
        name="undamped",
        statement=(
            "No damping of any kind. What comes back are the undamped natural frequencies, "
            "which for a lightly damped structure differ from the damped ones by a factor of "
            "sqrt(1 - zeta^2) — negligible below a few percent of critical. What is *not* "
            "here is any amplitude: a resonant response, a Q factor and a damping ratio are "
            "all properties of the damping this model does not have."
        ),
        excludes=["damping_ratio", "q_factor", "resonant_amplitude", "frequency_response"],
    ),
    Assumption(
        name="free_vibration",
        statement=(
            "No load, and none is accepted. An eigensolve asks what the structure does when "
            "nothing drives it; a load case may restrain a boundary but cannot push on one, "
            "and a traction is refused rather than ignored. Forced response — the answer to "
            "'how much does it move at 50 Hz' — is a different solve."
        ),
        excludes=["forced_response", "harmonic_excitation", "base_excitation"],
    ),
    Assumption(
        name="small_vibration",
        statement=(
            "Linear vibration about an unstressed, undeformed state. There is no stress "
            "stiffening and no preload, so a taut membrane and a spun rotor — whose "
            "stiffness comes *from* their load — are outside this model, and their "
            "frequencies would be under-predicted rather than approximated."
        ),
        excludes=["stress_stiffening", "prestress", "spin_softening", "large_amplitude"],
    ),
    Assumption(
        name="discretised_mass",
        statement=(
            "Both mass and stiffness are discretised, and each biases the frequencies. The "
            "NumPy half lumps the mass at the nodes, which under-predicts; the FEniCSx half "
            "uses the consistent mass matrix, which over-predicts; and the constant-strain "
            "element the mock uses is stiff in bending, which pushes back the other way. So "
            "the pair brackets rather than agrees, and refining is what closes the gap — "
            "compare two resolutions before quoting a frequency to three figures."
        ),
    ),
    TWO_DIMENSIONAL,
]

#: What a modal load case may say on a named boundary (#101).
#:
#: **The restraints of :data:`ELASTICITY_CONDITIONS`, and literally those objects** — filtered
#: rather than re-spelled, so the two capabilities cannot drift into two vocabularies for one
#: clamp. That sharing is the point: a load case authored to hold a bracket for a stress check
#: applies unchanged to the modal solve of the same bracket, which is exactly the reuse #85
#: made a load case an object for.
#:
#: The tractions are *absent*, and absent is a refusal: a key no capability declares is a 422
#: at submit. An eigensolve has no load, so a load case carrying one describes a different
#: problem, and saying so beats accepting it and ignoring it.
MODAL_CONDITIONS = [spec for spec in ELASTICITY_CONDITIONS if spec.kind == "dirichlet"]


#: Transient heat: the metrics a start-up answers that a steady solve cannot (#82).
#:
#: The interesting one is what is *missing* from the declared reductions. A declared metric
#: is a reduction of the result payload, and a transient's payload is one instant — so
#: `t_final_max` can be declared and the peak over the whole history cannot. `t_peak` is
#: computed by the adapter for exactly that reason, and the pair differ whenever a solve
#: overshoots.
#:
#: Which of the two a metric is, is now *declared* rather than implied by its name (#86):
#: `over="run"` marks the three that only the adapter can supply, and the constructor
#: refuses to let one of them also name a field. `t_rise` stays `payload` — it is the final
#: peak minus a parameter, so it is a property of the result that came back, just not a
#: pure reduction of it.
TRANSIENT_HEAT_METRICS = [
    MetricSpec(
        name="t_final_max",
        unit="degC",
        description=(
            "Peak temperature at the final instant — a reduction of the field that comes "
            "back, and therefore the one temperature here a caller can verify from the "
            "payload alone."
        ),
        field="T",
        reduction="max",
    ),
    MetricSpec(
        name="t_peak",
        unit="degC",
        over="run",
        description=(
            "Highest temperature reached at any time during the run, which is the number a "
            "rating must survive. Equal to `t_final_max` for a monotonic warm-up and larger "
            "whenever the solve overshoots."
        ),
    ),
    MetricSpec(
        name="t_rise",
        unit="K",
        description="Final peak temperature above ambient — the steady figure, once settled.",
    ),
    MetricSpec(
        name="time_to_90pc",
        unit="s",
        over="run",
        description=(
            "Time to reach 90% of the temperature rise, interpolated between steps so the "
            "answer is not a property of the step size. Absent when the run never got "
            "there. This is the quantity `steady_state` puts out of reach, which is the "
            "whole reason this capability exists."
        ),
    ),
    MetricSpec(
        name="time_constant",
        unit="s",
        over="run",
        description=(
            "Lumped time constant `rho_cp V / (h A)`: how fast the body would charge if it "
            "were too conductive to hold a gradient. A closed form, not a measurement — "
            "compare it with `time_to_90pc` (which is about 2.3 time constants) to see how "
            "far from lumped the device actually is."
        ),
    ),
]

#: Transient heat: everything the steady set assumes except `steady_state`, which is the one
#: this capability exists to lift — plus the two the time discretisation introduces.
TRANSIENT_HEAT_ASSUMPTIONS = [
    Assumption(
        name="linear_material",
        statement=(
            "Conductivity and heat capacity are constant per region and independent of "
            "temperature, so the response is linear in the load and the time constant does "
            "not drift as the device warms."
        ),
    ),
    Assumption(
        name="convection_coefficient",
        statement=(
            "The fluid is not solved. It enters as a single coefficient `h` on exposed "
            "faces, and it is held constant in time — a real device's natural convection "
            "strengthens as it warms, which would shorten the tail of the curve."
        ),
        excludes=["flow_field", "buoyancy", "local_heat_transfer_coefficient"],
    ),
    Assumption(
        name="no_radiation",
        statement="Radiative exchange is omitted entirely, as in the steady adapters.",
        excludes=["emissivity", "radiative_flux"],
    ),
    Assumption(
        name="uniform_initial_temperature",
        statement=(
            "The body starts at one temperature everywhere. A device switched on cold is "
            "exactly that; a device restarted while still warm from the last cycle is not, "
            "and no initial field can be supplied."
        ),
        excludes=["duty_cycle_history", "non_uniform_initial_state"],
    ),
    Assumption(
        name="first_order_in_time",
        statement=(
            "Backward Euler: unconditionally stable, and first-order accurate, so the curve "
            "is damped rather than oscillatory when the step is coarse. Halve `steps` and "
            "the early rise moves; if it moves much, the step was too coarse to read."
        ),
    ),
    TWO_DIMENSIONAL,
]


# ------------------------------------------------------------------- convergence (#107)

#: What the relaxation mocks converge to, and why they hand back an unconverged field.
#:
#: Every NumPy stand-in here sweeps a grid until the change per sweep falls below a
#: tolerance, and every one of them stops at its iteration cap if it does not get there. That
#: was already true and already reported — `converged`, `residual` and a warning have been on
#: a result since 1.3 — but nothing *declared* it, so `residual` was a bare float whose
#: meaning lived in the adapter's source. It is a temperature change per sweep in `mock.heat2d`
#: and a change in vector potential in `mock.magnetostatics2d`, which is exactly the kind of
#: difference a caller cannot guess.
#:
#: `on_failure="return"` is deliberate rather than lenient. A partly-relaxed field is a
#: legitimate preview — `mock.heat2d`'s own "fast preview" example says the temperature is not
#: converged and the shape is — and a caller that asked for 800 sweeps on a 160-cell grid
#: asked for that. What makes it safe is that the caller can now *know* before submitting.
RELAXATION_CONVERGENCE = ConvergenceSpec(
    method="relaxation",
    measures="largest change in the unknown over one sweep",
    unit=None,
    default_tolerance=1e-9,
    on_failure="return",
)

#: The same, for the mocks whose unknown is a temperature, where the residual has a unit.
RELAXATION_CONVERGENCE_K = ConvergenceSpec(
    method="relaxation",
    measures="largest temperature change over one sweep",
    unit="K",
    default_tolerance=1e-9,
    on_failure="return",
)

#: Successive substitution on a temperature-dependent conductivity (#107).
#:
#: **`on_failure="fail"`, and that is the whole point of the pair.** Freeze `k` at the current
#: temperature, solve the linear problem, repeat: the fixed point is the answer and an iterate
#: short of it solves a conduction problem with the *wrong* conductivity — a field that is a
#: solution to a question nobody asked. Returning it with a flag is the plausible answer this
#: project exists not to give, which is why the relaxation adapters' policy would be wrong
#: here even though it is right there.
PICARD_CONVERGENCE = ConvergenceSpec(
    method="picard",
    measures=(
        "largest temperature change between successive outer iterations, "
        "relative to the temperature rise"
    ),
    unit=None,
    default_tolerance=1e-6,
    on_failure="fail",
)

#: Newton on the same problem: the FEniCSx half of the pair.
#:
#: A different residual from Picard's — this one is the norm of the discrete heat balance
#: itself rather than a step size — and declaring both is what stops a caller comparing two
#: numbers that are not the same quantity. Same `on_failure` for the same reason.
NEWTON_CONVERGENCE = ConvergenceSpec(
    method="newton",
    measures="norm of the nonlinear residual, relative to the first iteration's",
    unit=None,
    default_tolerance=1e-8,
    on_failure="fail",
)


# ------------------------------------------------- temperature-dependent conduction (#107)

#: What a nonlinear conduction solve answers, beyond the linear pair's peak and flux.
#:
#: `k_ratio` is the one that is new, and it is the metric that says whether this capability
#: was worth using: it is the spread of the *solved* conductivity field, so a value near 1
#: means the material barely varied over the range and the linear adapter would have given
#: the same answer for a fraction of the cost. A capability that can tell a caller it was
#: unnecessary is more useful than one that cannot.
NONLINEAR_HEAT_METRICS = [
    MetricSpec(
        name="t_max",
        unit="degC",
        description="Peak temperature in the domain.",
        field="T",
        reduction="max",
    ),
    MetricSpec(
        name="flux_max",
        unit="W/m^2",
        description="Peak conductive heat flux magnitude, k(T)|grad T|.",
        field="flux",
        reduction="max",
    ),
    MetricSpec(
        name="k_ratio",
        unit="1",
        description=(
            "Highest conductivity in the converged field over the lowest. 1.0 means the "
            "material did not vary over the range this solve spanned, and a linear solve "
            "would have answered the same question more cheaply."
        ),
        # Not a reduction: it is the ratio of two reductions of one field, and declaring it
        # as `max` of `k` would report a number in W/(m·K) under a dimensionless name.
    ),
]

#: What the nonlinear pair assumes — and, as importantly, which of the linear pair's
#: assumptions it *retires*.
#:
#: `constant_properties` was in `HEAT_ASSUMPTIONS` from #70, complete with the sentence
#: saying when it fails. This is the capability that removes it, which is why this list is a
#: separate object rather than an extension: an assumption set describes one capability, and
#: sharing the linear pair's would have had this adapter claim a limitation it does not have.
NONLINEAR_HEAT_ASSUMPTIONS = [
    Assumption(
        name="steady_state",
        statement=(
            "No time derivative: the temperature reported is the equilibrium reached after a "
            "time this model cannot tell you. A nonlinear transient is a harder problem than "
            "either half of this one and is not it."
        ),
        excludes=["thermal_time_constant", "transient_temperature", "duty_cycle_rise"],
    ),
    Assumption(
        name="linear_conductivity_law",
        statement=(
            "k varies *linearly* with temperature, k(T) = k0 (1 + beta (T - T_ref)). That is "
            "a good fit for metals over a few hundred kelvin and a poor one across a phase "
            "change, where k moves discontinuously — this reports a smooth answer either way, "
            "which is the failure mode to watch for. Extrapolating far outside the range beta "
            "was fitted over is the same error in slower motion."
        ),
        excludes=["phase_change", "latent_heat", "tabulated_conductivity"],
    ),
    Assumption(
        name="positive_conductivity",
        statement=(
            "k is clamped at a small positive value. A beta steep enough to drive it negative "
            "over the solved range is outside the model's regime, and the clamp means such a "
            "case returns a *converged-looking* answer for a material that stopped being the "
            "one you described — check `k_ratio` against what you expect."
        ),
    ),
    Assumption(
        name="fixed_and_insulated_faces",
        statement=(
            "Two faces are held at fixed temperatures and every other face is insulated. No "
            "convection, no radiation, no flux boundary — the linear heat pair is where the "
            "convective surface model lives, and this capability trades it for a problem with "
            "a closed-form answer to be checked against."
        ),
        excludes=["convection_coefficient", "emissivity", "radiative_flux"],
    ),
    TWO_DIMENSIONAL,
]
