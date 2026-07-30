"""Mock solver: steady conduction in a solid, convectively cooled at every surface.

Solves the stationary heat equation with a volumetric source and piecewise-constant
conductivity,

    -div( k grad T ) = q ,

**over the solid only** — the regions of a ``regions2d`` geometry. The surrounding fluid
is not meshed. Instead every face where solid meets fluid, and every face on the outer
boundary, carries a convective condition

    -k dT/dn = h (T - T_ambient) .

That choice is the whole model, and it is worth stating why. The obvious alternative —
treat the air as another conducting region — was tried first and is wrong for this
problem. Air conducts at 0.026 W/(m·K) against aluminium's 205, so heat cannot leave
through the fins at all: they become decoration, and the chip temperature climbs until
the *outer domain boundary* alone can shed the load. On a test geometry that meant
running past 220 °C and still rising. A heat sink whose fins do nothing is not a heat
sink, and an example that shows fins not working teaches the wrong lesson.

Convecting from the solid surface is also how heat sinks are actually analysed: the fluid
is represented by a coefficient, not resolved. Fin area then does what fin area is for —
add fins and the chip runs cooler, which is the thing this example exists to demonstrate.

Material keys read from the geometry (everything else ignored):

- ``k`` — thermal conductivity in W/(m·K) (default 1.0)
- ``q`` — volumetric heat source in W/m³ (default 0.0)

A development stand-in that runs anywhere NumPy does. The physics is textbook, the
discretization first-order finite volume.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import HEAT_METRICS, VTK_ARTIFACT
from .mock_laplace import _grid_shape, grid_to_mesh2d, polygon_mask, write_vtk_structured_points
from .mock_magnetostatics import _harmonic_mean, rasterize_regions
from .registry import register


def solid_mask(geometry: Regions2D, xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    """True where any region covers the grid point. The background is fluid."""
    mask = np.zeros(xx.shape, dtype=bool)
    for region in geometry.regions:
        mask |= polygon_mask(np.asarray(region.shape.points, dtype=float), xx, yy)
    return mask


@register
class MockHeat2D(Solver):
    name = "mock.heat2d"
    title = "Heat sink (mock, NumPy)"
    description = (
        "Steady conduction in a solid with per-region conductivity and volumetric heat "
        "sources, cooled by convection at every solid surface. The fluid is represented "
        "by a convection coefficient rather than meshed, which is how heat sinks are "
        "analysed. Development stand-in that runs anywhere NumPy does."
    )
    geometry_types = ["regions2d"]
    physics = "heat-conduction"
    availability = "mock"
    metrics = HEAT_METRICS
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="fast preview",
            description="Coarse grid, few sweeps. The temperature is not converged; the shape is.",
            params={"resolution": 80, "iterations": 800, "write_vtk": False},
        ),
        CapabilityExample(
            title="forced air",
            description=(
                "The gallery heat sink under a fan rather than in still air. Raising `h` is "
                "the comparison this example exists for: it is the cheapest way to cool a "
                "part, and it changes the answer more than most geometry edits."
            ),
            params={"resolution": 200, "iterations": 6000, "h": 120.0},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=160, ge=16, le=512, description="Grid points along the longer domain edge"
        )
        iterations: int = Field(
            default=3000, ge=10, le=40000, description="Maximum relaxation sweeps"
        )
        h: float = Field(
            default=25.0,
            gt=0.0,
            le=10000.0,
            description=(
                "Convection coefficient in W/(m²·K) at solid surfaces. Around 10 for still "
                "air, 25 for gentle airflow, 100+ for forced air."
            ),
        )
        t_ambient: float = Field(
            default=20.0, description="Ambient fluid temperature in °C", ge=-273.15
        )
        relaxation: float = Field(
            default=1.9,
            gt=0.0,
            lt=2.0,
            description=(
                "Over-relaxation factor for the red-black sweep. 1.0 is plain "
                "Gauss-Seidel; values near 2 converge fastest but can oscillate."
            ),
        )
        report_every: int = Field(default=100, ge=1, description="Sweeps between progress events")
        output: Literal["grid2d", "mesh2d"] = Field(
            default="grid2d", description="Result kind to return"
        )
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "MockHeat2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    def solve(
        self, geometry: Regions2D, params: "MockHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)

        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)
        dx = float(x[1] - x[0])
        dy = float(y[1] - y[0])

        solid = solid_mask(geometry, xx, yy)
        if not solid.any():
            raise ValueError("no solid cells: every region fell between grid points")
        conductivity = np.maximum(rasterize_regions(geometry, xx, yy, "k", 1.0), 1e-12)
        source = rasterize_regions(geometry, xx, yy, "q", 0.0)

        coefficients, diagonal = _assemble(solid, conductivity, params.h, dx, dy)
        # Every solid cell must be able to reach the fluid somehow, or its temperature is
        # unbounded — an isolated blob with a source has no steady state at all.
        if not np.all(diagonal[solid] > 0):
            raise ValueError("some solid cells have no conductive or convective coupling")

        # Start from the answer the global energy balance already gives, rather than from
        # ambient. Aluminium conducts about ten thousand times better across a cell than
        # the surface convects away from it, so the solid is nearly isothermal and the
        # slowest mode by far is the whole block charging up through that weak coupling —
        # exactly the mode a local relaxation is worst at. Total power in over total
        # surface conductance out is that level in closed form, which leaves the sweeps
        # only the temperature *distribution* to resolve, and that is a fast local mode.
        temperature = np.full((ny, nx), _balanced_level(
            solid, source, params.h, params.t_ambient, dx, dy
        ), dtype=float)
        ambient_flux = _ambient_flux(solid, params.h, params.t_ambient, dx, dy)
        rhs = np.where(solid, source + ambient_flux, 0.0)
        safe_diagonal = np.maximum(diagonal, 1e-30)

        # Red-black Gauss-Seidel with over-relaxation, not Jacobi. Diffusion needs about
        # N^2 Jacobi sweeps to converge across N cells, and the reference heat sink is 160
        # across: measured, plain Jacobi reached a 2.7 K rise after 12000 sweeps where the
        # converged answer is 28.1 K — an example reporting a number ten times too small.
        # Together with the initial guess above, 200 sweeps now land within 1% of the
        # value 40000 Jacobi sweeps were still crawling towards. The two colours update
        # independently, so each half stays one vectorized expression.
        rows, cols = np.indices((ny, nx))
        colours = [(rows + cols) % 2 == parity for parity in (0, 1)]

        residual = float("inf")
        for it in range(1, params.iterations + 1):
            ctx.check_cancelled()
            residual = 0.0
            for colour in colours:
                neighbours = (
                    coefficients["e"] * _shift(temperature, 0, -1)
                    + coefficients["w"] * _shift(temperature, 0, 1)
                    + coefficients["n"] * _shift(temperature, -1, 0)
                    + coefficients["s"] * _shift(temperature, 1, 0)
                )
                target = (neighbours + rhs) / safe_diagonal
                update = colour & solid
                change = params.relaxation * (target - temperature)
                residual = max(residual, float(np.max(np.abs(change[update]), initial=0.0)))
                temperature = np.where(update, temperature + change, temperature)

            if it % params.report_every == 0 or it == params.iterations:
                ctx.progress(
                    ProgressEvent(iteration=it, total=params.iterations, residual=residual)
                )
            if residual < 1e-9:
                break

        # Fluid cells are not part of the solve; show them at ambient so the picture reads
        # as "metal against air" rather than as a field with holes in it.
        temperature = np.where(solid, temperature, params.t_ambient)
        flux = np.hypot(
            -conductivity * np.gradient(temperature, dx, axis=1),
            -conductivity * np.gradient(temperature, dy, axis=0),
        ) * solid

        fields = {"T": temperature, "flux": flux, "k": np.where(solid, conductivity, 0.0)}
        stats = {
            "cells": float(nx * ny),
            "solid_cells": float(solid.sum()),
            "iterations": float(it),
        }
        # `t_max` and `t_rise` used to be `stats` keys. They were never costs — they are the
        # answer the study exists to produce — and issue #46 is where that conflation gets
        # undone. `t_max` is now computed generically from its declaration (the `max` of the
        # field `T`); only `t_rise` is here, because it needs `t_ambient` and a parameter is
        # not in the result payload.
        converged = residual < 1e-9
        diagnostics = {
            "metrics": {"t_rise": float(temperature.max() - params.t_ambient)},
            "converged": converged,
            "residual": residual,
            "warnings": (
                []
                if converged
                else [
                    f"stopped at the iteration cap ({params.iterations}) with residual "
                    f"{residual:.3g}; the temperature may not be converged"
                ]
            ),
        }
        if params.write_vtk:
            write_vtk_structured_points(ctx.artifact("solution.vtk"), x, y, fields)

        if params.output == "mesh2d":
            return SolverResult(
                kind="mesh2d",
                stats=stats,
                **diagnostics,
                data={
                    "bounds": [xmin, ymin, xmax, ymax],
                    **grid_to_mesh2d(x, y, np.zeros((ny, nx), dtype=bool), fields),
                },
            )

        return SolverResult(
            kind="grid2d",
            stats=stats,
            **diagnostics,
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {name: values.ravel().tolist() for name, values in fields.items()},
                # The mask marks fluid, so a viewer can grey out what was not solved.
                "mask": (~solid).astype(np.uint8).ravel().tolist(),
            },
        )


def _shift(values: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Neighbour values, edge-padded so boundary cells see themselves rather than wrap."""
    return np.roll(np.roll(values, rows, axis=0), cols, axis=1)


def _neighbour_is_solid(solid: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Solidity of a neighbour, with off-grid neighbours counted as fluid.

    Off-grid has to be fluid, not solid: a cell at the domain edge is exposed to the
    surroundings, and treating that face as a conduction face to nowhere would insulate
    the model's outer surface.
    """
    shifted = _shift(solid, rows, cols)
    if rows > 0:
        shifted[:rows, :] = False
    elif rows < 0:
        shifted[rows:, :] = False
    if cols > 0:
        shifted[:, :cols] = False
    elif cols < 0:
        shifted[:, cols:] = False
    return shifted


def _assemble(
    solid: np.ndarray, conductivity: np.ndarray, h: float, dx: float, dy: float
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Per-face conduction coefficients, and the diagonal they sum into.

    A solid-solid face conducts with the harmonic mean of the two conductivities — the
    standard treatment for a discontinuous coefficient, without which an interface
    between materials is averaged into something neither of them is. A solid-fluid face
    convects instead, which contributes to the diagonal only (its ambient term is a
    constant and lands in the right-hand side).
    """
    coefficients: dict[str, np.ndarray] = {}
    diagonal = np.zeros_like(conductivity)
    for key, (rows, cols, spacing) in {
        "e": (0, -1, dx),
        "w": (0, 1, dx),
        "n": (-1, 0, dy),
        "s": (1, 0, dy),
    }.items():
        neighbour_solid = _neighbour_is_solid(solid, rows, cols)
        conduction = (
            _harmonic_mean(conductivity, _shift(conductivity, rows, cols)) / spacing**2
        )
        coefficients[key] = np.where(solid & neighbour_solid, conduction, 0.0)
        diagonal += coefficients[key]
        # Faces onto fluid convect: h/d into the diagonal, h*T_ambient/d into the rhs.
        diagonal += np.where(solid & ~neighbour_solid, h / spacing, 0.0)
    return coefficients, diagonal


def _ambient_flux(solid: np.ndarray, h: float, t_ambient: float, dx: float, dy: float):
    """The `h * T_ambient / d` term for every face of a solid cell that meets fluid."""
    flux = np.zeros(solid.shape)
    for rows, cols, spacing in ((0, -1, dx), (0, 1, dx), (-1, 0, dy), (1, 0, dy)):
        exposed = solid & ~_neighbour_is_solid(solid, rows, cols)
        flux += np.where(exposed, h * t_ambient / spacing, 0.0)
    return flux


def _balanced_level(
    solid: np.ndarray,
    source: np.ndarray,
    h: float,
    t_ambient: float,
    dx: float,
    dy: float,
) -> float:
    """The uniform temperature at which total power in equals total convected power out.

    Not an approximation of the answer — it is the answer's *mean level*, exact for a
    perfectly conducting solid, and the relaxation corrects the distribution around it.
    Falls back to ambient when nothing is exposed, which the caller has already rejected.
    """
    power_in = float(np.sum(np.where(solid, source, 0.0)) * dx * dy)
    conductance = 0.0
    for rows, cols, face in ((0, -1, dy), (0, 1, dy), (-1, 0, dx), (1, 0, dx)):
        exposed = solid & ~_neighbour_is_solid(solid, rows, cols)
        conductance += float(exposed.sum()) * h * face
    if conductance <= 0.0:
        return t_ambient
    return t_ambient + power_in / conductance
