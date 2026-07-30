"""Shared geometry fixtures for the test suite (not a test module)."""

import numpy as np

from fenixspoon.geometry import Domain2D, Polygon2D, Region2D, Regions2D


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
