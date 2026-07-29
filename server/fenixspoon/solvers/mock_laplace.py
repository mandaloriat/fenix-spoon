"""Mock solver: 2D incompressible potential flow around a polygonal obstacle.

Solves the Laplace equation for the streamfunction psi on a regular Cartesian grid with a
Jacobi iteration — pure NumPy, no FEM, no meshing. Physically it is textbook potential flow:
uniform stream psi = U*y at the far field, psi = const on the obstacle. The point of this
solver is to exercise the entire toolkit (geometry schema, job manager, progress streaming,
cancellation, artifacts, grid2d/mesh2d results, browser rendering) without FEniCSx installed;
the numbers are plausible, not publication-grade.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Domain2D
from .base import ProgressEvent, Solver, SolverContext, SolverResult
from .registry import register


def _grid_shape(bounds, resolution: int) -> tuple[int, int]:
    """Grid shape (ny, nx) for a resolution along the longer edge — shared by the
    solver and its cost estimate so the two cannot disagree."""
    xmin, ymin, xmax, ymax = bounds
    lx, ly = xmax - xmin, ymax - ymin
    if lx >= ly:
        return max(8, round(resolution * ly / lx)), resolution
    return resolution, max(8, round(resolution * lx / ly))


def polygon_mask(points: np.ndarray, xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    """Vectorized even-odd rule (PNPOLY): True where grid points fall inside the polygon."""
    inside = np.zeros(xx.shape, dtype=bool)
    px, py = points[:, 0], points[:, 1]
    j = len(points) - 1
    for i in range(len(points)):
        denom = py[j] - py[i]
        if denom == 0.0:
            denom = 1e-30
        crosses = (py[i] > yy) != (py[j] > yy)
        xint = (px[j] - px[i]) * (yy - py[i]) / denom + px[i]
        inside ^= crosses & (xx < xint)
        j = i
    return inside


def grid_to_mesh2d(
    x: np.ndarray, y: np.ndarray, mask: np.ndarray, fields: dict[str, np.ndarray]
) -> dict:
    """Triangulate the unmasked part of a structured grid into a ``mesh2d`` payload.

    Nodes are the unmasked grid points; each cell whose four corners are all unmasked
    contributes two triangles. Node indices are compacted so clients never see holes.
    """
    ny, nx = mask.shape
    keep = ~mask
    node_id = -np.ones((ny, nx), dtype=np.int64)
    node_id[keep] = np.arange(int(keep.sum()))

    yy_idx, xx_idx = np.nonzero(keep)
    xs = x[xx_idx]
    ys = y[yy_idx]
    points = np.column_stack([xs, ys])

    c00 = node_id[:-1, :-1]
    c01 = node_id[:-1, 1:]
    c10 = node_id[1:, :-1]
    c11 = node_id[1:, 1:]
    full = (c00 >= 0) & (c01 >= 0) & (c10 >= 0) & (c11 >= 0)
    a, b, c, d = c00[full], c01[full], c10[full], c11[full]
    triangles = np.concatenate(
        [np.column_stack([a, b, d]), np.column_stack([a, d, c])], axis=0
    )

    return {
        "points": points.tolist(),
        "triangles": triangles.tolist(),
        "point_fields": {name: values[keep].ravel().tolist() for name, values in fields.items()},
    }


def write_vtk_structured_points(
    path, x: np.ndarray, y: np.ndarray, fields: dict[str, np.ndarray]
) -> None:
    """Write a legacy-VTK STRUCTURED_POINTS file (opens directly in ParaView)."""
    ny, nx = next(iter(fields.values())).shape
    with open(path, "w") as f:
        f.write("# vtk DataFile Version 3.0\n")
        f.write("fenixspoon grid2d result\n")
        f.write("ASCII\nDATASET STRUCTURED_POINTS\n")
        f.write(f"DIMENSIONS {nx} {ny} 1\n")
        f.write(f"ORIGIN {x[0]:.9g} {y[0]:.9g} 0\n")
        f.write(f"SPACING {x[1] - x[0]:.9g} {y[1] - y[0]:.9g} 1\n")
        f.write(f"POINT_DATA {nx * ny}\n")
        for name, values in fields.items():
            f.write(f"SCALARS {name} double 1\nLOOKUP_TABLE default\n")
            np.savetxt(f, values.ravel(), fmt="%.9g")


@register
class MockLaplace2D(Solver):
    name = "mock.laplace2d"
    title = "Potential flow (mock, NumPy)"
    description = (
        "2D incompressible potential flow around the obstacle: Laplace equation for the "
        "streamfunction on a Cartesian grid, Jacobi-iterated. Development stand-in that runs "
        "anywhere NumPy does."
    )

    class Params(BaseModel):
        resolution: int = Field(
            default=128, ge=16, le=512, description="Grid points along the longer domain edge"
        )
        iterations: int = Field(default=2000, ge=10, le=20000)
        u_inf: float = Field(default=1.0, description="Free-stream velocity (x direction)")
        report_every: int = Field(default=100, ge=1)
        output: Literal["grid2d", "mesh2d"] = Field(
            default="grid2d",
            description="Result kind: regular grid, or triangulated unstructured mesh",
        )
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Domain2D, params: "MockLaplace2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    def solve(
        self, geometry: Domain2D, params: "MockLaplace2D.Params", ctx: SolverContext
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)

        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)  # shape (ny, nx), row iy -> y[iy]

        pts = np.asarray(geometry.obstacle.points, dtype=float)
        mask = polygon_mask(pts, xx, yy)
        if not mask.any():
            # Snap the single nearest node to the obstacle centroid so the body exists on-grid.
            cx, cy = pts.mean(axis=0)
            mask[np.argmin(np.abs(y - cy)), np.argmin(np.abs(x - cx))] = True

        # Free stream everywhere; body held at the streamline through its centroid.
        psi = (yy * params.u_inf).astype(np.float64)
        psi_body = float(pts[:, 1].mean()) * params.u_inf
        psi[mask] = psi_body

        residual = float("inf")
        for it in range(1, params.iterations + 1):
            ctx.check_cancelled()
            new = psi.copy()
            new[1:-1, 1:-1] = 0.25 * (
                psi[1:-1, :-2] + psi[1:-1, 2:] + psi[:-2, 1:-1] + psi[2:, 1:-1]
            )
            new[mask] = psi_body  # Dirichlet on the body; edges untouched -> far-field Dirichlet
            residual = float(np.max(np.abs(new - psi)))
            psi = new
            if it % params.report_every == 0 or it == params.iterations:
                ctx.progress(
                    ProgressEvent(iteration=it, total=params.iterations, residual=residual)
                )
            if residual < 1e-9:
                break

        # Velocity magnitude from psi: u = d(psi)/dy, v = -d(psi)/dx.
        dy_ = y[1] - y[0]
        dx_ = x[1] - x[0]
        u = np.gradient(psi, dy_, axis=0)
        v = -np.gradient(psi, dx_, axis=1)
        speed = np.sqrt(u**2 + v**2)
        speed[mask] = 0.0

        if params.write_vtk:
            write_vtk_structured_points(
                ctx.artifact("solution.vtk"), x, y, {"psi": psi, "speed": speed}
            )

        stats = {"cells": float(nx * ny), "iterations": float(it)}
        if params.output == "mesh2d":
            return SolverResult(
                kind="mesh2d",
                stats=stats,
                data={
                    "bounds": [xmin, ymin, xmax, ymax],
                    **grid_to_mesh2d(x, y, mask, {"psi": psi, "speed": speed}),
                },
            )

        return SolverResult(
            kind="grid2d",
            stats=stats,
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {
                    "psi": psi.ravel().tolist(),
                    "speed": speed.ravel().tolist(),
                },
                "mask": mask.astype(np.uint8).ravel().tolist(),
            },
        )
