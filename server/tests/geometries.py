"""Shared geometry fixtures for the test suite (not a test module)."""

import numpy as np

from fenixspoon.geometry import (
    Axisymmetric2D,
    BoundarySpec,
    Domain2D,
    NearSelector,
    Polygon2D,
    Region2D,
    Regions2D,
)


def rect(x0: float, y0: float, x1: float, y1: float) -> Polygon2D:
    return Polygon2D(points=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


# A solenoid cross-section: iron core on the axis, two coil sections carrying
# opposite-signed current (the two sides of the same winding cut by the plane).
SOLENOID = Regions2D(
    bounds=(-0.06, -0.06, 0.06, 0.06),
    regions=[
        Region2D(name="core", shape=rect(-0.01, -0.03, 0.01, 0.03), material={"mu_r": 1000.0}),
        Region2D(
            name="coil_left",
            shape=rect(-0.025, -0.03, -0.015, 0.03),
            material={"current_density": -5.0e6},
        ),
        Region2D(
            name="coil_right",
            shape=rect(0.015, -0.03, 0.025, 0.03),
            material={"current_density": 5.0e6},
        ),
    ],
    background={"mu_r": 1.0},
)


def naca4(
    camber: float = 0.0, position: float = 0.4, thickness: float = 0.12, n: int = 45
) -> list[tuple[float, float]]:
    """A NACA four-digit section, chord 1, leading edge at the origin.

    Cosine-spaced so the leading edge is resolved, which is where the curvature is. Here rather
    than in one test module because these sections have *tabulated* answers — zero-lift angle,
    quarter-chord moment — so a test that uses one has an external number to check against
    instead of only an internal one, and both the mock and the FEniCSx suites want that.

    The 4-vertex diamond several other modules use is fine for exercising plumbing and useless
    for aerodynamics: it is not symmetric, so it cannot check that lift is antisymmetric in
    incidence, and no textbook lists its lift-curve slope.
    """
    station = 0.5 * (1 - np.cos(np.linspace(0, np.pi, n)))
    half = (
        5
        * thickness
        * (
            0.2969 * np.sqrt(station)
            - 0.1260 * station
            - 0.3516 * station**2
            + 0.2843 * station**3
            - 0.1036 * station**4
        )
    )
    mean = np.where(
        station < position,
        camber / position**2 * (2 * position * station - station**2),
        camber / (1 - position) ** 2 * ((1 - 2 * position) + 2 * position * station - station**2),
    )
    upper = [(float(a), float(c + t)) for a, t, c in zip(station, half, mean, strict=True)]
    lower = [(float(a), float(c - t)) for a, t, c in zip(station, half, mean, strict=True)]
    return upper + lower[::-1][1:-1]


#: A chord-1 symmetric section in a domain with room for a circulation contour around it.
NACA0012 = Domain2D(bounds=(-1.5, -1.5, 2.5, 1.5), obstacle=Polygon2D(points=naca4()))


def circle(radius: float, n: int = 48, centre: tuple[float, float] = (0.0, 0.0)) -> Polygon2D:
    """A polygonal circle. `n` is a compromise: the analytical stress concentration it stands
    in for is a *circle*, and every facet is somewhere the curvature is wrong."""
    angle = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return Polygon2D(
        points=[
            (float(centre[0] + radius * np.cos(a)), float(centre[1] + radius * np.sin(a)))
            for a in angle
        ]
    )


#: A cantilever: length 1 m, depth 100 mm, aluminium with a steel insert at mid-span so the
#: per-region material path is exercised rather than merely available. The region has to sit
#: strictly inside `bounds`, which is why a *uniform* plate cannot be written down at all —
#: `regions2d` requires at least one region and it cannot cover the domain. That is a real
#: finding for issue #81, not a quirk of this fixture.
BEAM = Regions2D(
    bounds=(0.0, -0.05, 1.0, 0.05),
    regions=[
        Region2D(
            name="insert",
            shape=rect(0.45, -0.04, 0.55, 0.04),
            material={"youngs_modulus": 2.1e11, "poisson_ratio": 0.3},
        )
    ],
    background={"youngs_modulus": 7.0e10, "poisson_ratio": 0.33},
)

#: The same beam with one material everywhere, for the cases with a closed form to check
#: against. The region is present because the schema demands one and carries the background's
#: material so nothing varies.
UNIFORM_BEAM = Regions2D(
    bounds=(0.0, -0.05, 1.0, 0.05),
    regions=[
        Region2D(
            name="body",
            shape=rect(0.45, -0.04, 0.55, 0.04),
            material={"youngs_modulus": 2.1e11, "poisson_ratio": 0.3},
        )
    ],
    background={"youngs_modulus": 2.1e11, "poisson_ratio": 0.3},
)

#: A wide plate with a small circular hole: the Kirsch problem, whose peak stress is three
#: times the far field. The hole is a tenth of the plate half-width, which is "wide" enough
#: for the infinite-plate solution to be the right comparison.
PLATE_WITH_HOLE = Domain2D(bounds=(-1.0, -1.0, 1.0, 1.0), obstacle=circle(0.1))


# ------------------------------------------------------- axisymmetric sections (#100)

#: A coaxial section, and the reason it is here: **it has a closed-form capacitance**.
#:
#: Between two concentric cylinders the field is purely radial, so `C = 2*pi*eps*L/ln(b/a)`
#: exactly — and the truncation at the two z ends costs nothing, because the exact solution
#: does not vary with z and a zero-flux plane is therefore not an approximation but a
#: symmetry. That makes it the one axisymmetric case where a solver can be checked against an
#: answer from outside this repository rather than against itself, and it is a check the `r`
#: weight cannot survive being dropped from: solved as a plane slice the same payload gives a
#: number in the wrong units, let alone the wrong size.
#:
#: The region carries the background's own permittivity and exists because the schema demands
#: at least one — the same reason `UNIFORM_BEAM` has one.
COAX = Axisymmetric2D(
    bounds=(0.001, 0.0, 0.003, 0.002),
    regions=[
        Region2D(
            name="dielectric",
            shape=rect(0.0015, 0.0005, 0.0025, 0.0015),
            material={"eps_r": 1.0},
        )
    ],
    background={"eps_r": 1.0},
    boundaries=[
        BoundarySpec(
            name="inner",
            select=NearSelector(axis="x", value=0.001),
            description="The inner conductor's surface, at r = a.",
        ),
        BoundarySpec(
            name="outer",
            select=NearSelector(axis="x", value=0.003),
            description="The outer conductor's inner surface, at r = b.",
        ),
    ],
)

#: The section that asked for the geometry kind: an adaptive-optics position sensor (#100).
#:
#: An annular electrode on the reference body, the gold-coated back of the mirror shell
#: 90 µm away as the other plate, and a chamfer on the electrode's gap-facing corners. It is
#: **a section of the same shape as physics-lab's Exercise 5, not a copy of its dimensions**:
#: the exercise's radii, chamfer and guard are specified there and that repository is not
#: edited from here. What is matched deliberately is the one number that makes the comparison
#: mean anything — the annulus area over the gap gives a parallel-plate capacitance of
#: 0.0273 nF against the exercise's quoted 0.02758 nF, within 1%.
#:
#: The published fit for the real sensor is 0.031904 nF, about 15.7% above its parallel-plate
#: value, and that excess is fringe and chamfer. A solver on *this* section should land above
#: the parallel-plate value for the same reason, by an amount this section's own proportions
#: decide — which is why the tests assert the sign and a band rather than the published
#: number. Reproducing 0.031904 nF would need the exercise's section, and that is the
#: exercise's business.
#:
#: **It does not reach the axis**, and that is the point of having it here: r starts at 19 mm.
#: A solver that assumed the axis was always in the domain would be wrong about the case the
#: kind was added for.
CAPACITIVE_SENSOR = Axisymmetric2D(
    bounds=(0.019, -0.0006, 0.0231, 0.0006),
    regions=[
        Region2D(
            name="electrode",
            shape=Polygon2D(
                points=[
                    (0.0200, 0.000095),
                    (0.02005, 0.000045),
                    (0.02205, 0.000045),
                    (0.0221, 0.000095),
                    (0.0221, 0.000245),
                    (0.0200, 0.000245),
                ]
            ),
            material={"voltage": 1.0},
        ),
        Region2D(
            name="mirror",
            shape=rect(0.0195, -0.000245, 0.0226, -0.000045),
            material={"voltage": 0.0},
        ),
    ],
    background={"eps_r": 1.0},
)

#: The electrode's inner and outer radii and the gap, so a test can compute the
#: parallel-plate value from the same numbers the section is built from rather than from a
#: constant that would go stale the moment the fixture moved.
SENSOR_R1, SENSOR_R2, SENSOR_GAP = 0.0200, 0.0221, 90e-6

#: A section whose material sits *on* the axis: the shank of a fastener, the core of a
#: solenoid — the commonest axisymmetric shape there is, and the one the strictly-inside rule
#: would make unrepresentable if it applied to r = 0. Held at a potential so it is also a
#: runnable case rather than only a validation one.
ROD_ON_AXIS = Axisymmetric2D(
    bounds=(0.0, -0.01, 0.02, 0.01),
    regions=[
        Region2D(name="rod", shape=rect(0.0, -0.005, 0.005, 0.005), material={"voltage": 1.0}),
        Region2D(
            name="shield", shape=rect(0.014, -0.008, 0.016, 0.008), material={"voltage": 0.0}
        ),
    ],
    background={"eps_r": 1.0},
)


def slab_of(beta: float, k0: float = 50.0, t_ref: float = 20.0) -> Regions2D:
    """A uniform slab with a temperature-dependent conductivity (#107).

    One region covering the whole domain, plus a matching `background`, so the material is
    uniform however a solver samples it — the closed form it is checked against assumes so,
    and a rasterised region that misses the boundary column would quietly break that.
    """
    material = {"k": k0, "beta": beta, "t_ref": t_ref}
    inset = 1.0e-3
    return Regions2D(
        bounds=(0.0, 0.0, 1.0, 0.2),
        regions=[
            Region2D(
                name="slab",
                shape=Polygon2D(
                    points=[
                        (inset, inset),
                        (1.0 - inset, inset),
                        (1.0 - inset, 0.2 - inset),
                        (inset, 0.2 - inset),
                    ]
                ),
                material=material,
            )
        ],
        background=material,
    )


#: The default slab: carbon steel, whose conductivity falls about 11% over a 280 K span.
SLAB = slab_of(-4.0e-4)
