"""Shared geometry fixtures for the test suite (not a test module)."""

from fenixspoon.geometry import Polygon2D, Region2D, Regions2D


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
