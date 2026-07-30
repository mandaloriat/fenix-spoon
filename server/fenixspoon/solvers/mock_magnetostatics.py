"""Mock solver: 2D magnetostatics of a current-carrying cross-section.

Solves the magnetic vector potential equation for an out-of-plane current density,

    -div( (1/mu) grad A_z ) = J_z ,      B = ( dA_z/dy , -dA_z/dx ) ,

on a regular Cartesian grid with Gauss-Seidel-style Jacobi sweeps. Permeability is
piecewise constant per material region, so face coefficients use the harmonic mean —
the standard finite-volume treatment for discontinuous coefficients, without which the
field at an iron/air interface is visibly wrong.

Like ``mock.laplace2d`` this is a development stand-in that runs anywhere NumPy does;
it exists so the ``regions2d`` geometry schema and the solenoid demo can be built and
tested without FEniCSx. The physics is textbook but the discretization is first-order.

Material keys read from the geometry (everything else ignored):

- ``mu_r`` — relative permeability (default 1.0)
- ``current_density`` — J_z in A/m² (default 0.0)
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import MAGNETOSTATICS_METRICS, VTK_ARTIFACT
from .mock_laplace import (
    _grid_shape,
    grid_to_mesh2d,
    polygon_mask,
    write_vtk_structured_points,
)
from .registry import register

MU0 = 4.0e-7 * np.pi


def rasterize_regions(
    geometry: Regions2D, xx: np.ndarray, yy: np.ndarray, key: str, default: float
) -> np.ndarray:
    """Sample one material property onto the grid, later regions winning over earlier ones."""
    out = np.full(xx.shape, float(geometry.background.get(key, default)))
    for region in geometry.regions:
        mask = polygon_mask(np.asarray(region.shape.points, dtype=float), xx, yy)
        out[mask] = float(region.material.get(key, default))
    return out


def _harmonic_mean(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return 2.0 * a * b / np.maximum(a + b, 1e-300)


@register
class MockMagnetostatics2D(Solver):
    name = "mock.magnetostatics2d"
    title = "Magnetostatics (mock, NumPy)"
    description = (
        "2D magnetostatics for out-of-plane currents: vector potential A_z on a Cartesian "
        "grid with per-region permeability and current density, harmonic-mean face "
        "coefficients. Development stand-in that runs anywhere NumPy does."
    )
    geometry_types = ["regions2d"]
    physics = "magnetostatics"
    availability = "mock"
    metrics = MAGNETOSTATICS_METRICS
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="fast preview",
            description="Coarse and short: enough to see where the flux goes, not to measure it.",
            params={"resolution": 64, "iterations": 600, "write_vtk": False},
        ),
        CapabilityExample(
            title="resolved",
            description=(
                "What the solenoid demo submits. The iteration count matters more here than "
                "in the flow solver: the iron/air contrast slows convergence considerably."
            ),
            params={"resolution": 192, "iterations": 8000},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=128, ge=16, le=512, description="Grid points along the longer domain edge"
        )
        iterations: int = Field(default=3000, ge=10, le=40000)
        report_every: int = Field(default=100, ge=1)
        output: Literal["grid2d", "mesh2d"] = "grid2d"
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "MockMagnetostatics2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    def solve(
        self, geometry: Regions2D, params: "MockMagnetostatics2D.Params", ctx: SolverContext
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)

        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)
        dx = x[1] - x[0]
        dy = y[1] - y[0]

        mu_r = rasterize_regions(geometry, xx, yy, "mu_r", 1.0)
        current = rasterize_regions(geometry, xx, yy, "current_density", 0.0)
        nu = 1.0 / (MU0 * np.maximum(mu_r, 1e-12))  # reluctivity

        # Face reluctivities (harmonic mean across the interface).
        nu_e = _harmonic_mean(nu[1:-1, 1:-1], nu[1:-1, 2:])
        nu_w = _harmonic_mean(nu[1:-1, 1:-1], nu[1:-1, :-2])
        nu_n = _harmonic_mean(nu[1:-1, 1:-1], nu[2:, 1:-1])
        nu_s = _harmonic_mean(nu[1:-1, 1:-1], nu[:-2, 1:-1])
        diag = (nu_e + nu_w) / dx**2 + (nu_n + nu_s) / dy**2

        # A = 0 on the outer boundary: flux is confined to the modelled window.
        a_pot = np.zeros((ny, nx))
        rhs = current[1:-1, 1:-1]

        residual = float("inf")
        for it in range(1, params.iterations + 1):
            ctx.check_cancelled()
            interior = (
                nu_e * a_pot[1:-1, 2:] / dx**2
                + nu_w * a_pot[1:-1, :-2] / dx**2
                + nu_n * a_pot[2:, 1:-1] / dy**2
                + nu_s * a_pot[:-2, 1:-1] / dy**2
                + rhs
            ) / diag
            residual = float(np.max(np.abs(interior - a_pot[1:-1, 1:-1])))
            a_pot[1:-1, 1:-1] = interior
            if it % params.report_every == 0 or it == params.iterations:
                ctx.progress(
                    ProgressEvent(iteration=it, total=params.iterations, residual=residual)
                )
            if residual < 1e-14:
                break

        # B = (dA/dy, -dA/dx)
        bx = np.gradient(a_pot, dy, axis=0)
        by = -np.gradient(a_pot, dx, axis=1)
        b_mag = np.hypot(bx, by)

        fields = {"A": a_pot, "B": b_mag, "mu_r": mu_r}
        stats = {"cells": float(nx * ny), "iterations": float(it)}
        if params.write_vtk:
            write_vtk_structured_points(ctx.artifact("solution.vtk"), x, y, fields)

        if params.output == "mesh2d":
            no_hole = np.zeros((ny, nx), dtype=bool)
            return SolverResult(
                kind="mesh2d",
                stats=stats,
                data={
                    "bounds": [xmin, ymin, xmax, ymax],
                    **grid_to_mesh2d(x, y, no_hole, fields),
                },
            )

        return SolverResult(
            kind="grid2d",
            stats=stats,
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {name: values.ravel().tolist() for name, values in fields.items()},
                "mask": np.zeros(ny * nx, dtype=np.uint8).tolist(),
            },
        )
