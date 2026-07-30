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

from .base import ArtifactSpec, MetricSpec

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
