"""Mock solver: 2D incompressible potential flow around a polygonal obstacle.

Solves the Laplace equation for the streamfunction psi on a regular Cartesian grid with a
Jacobi iteration — pure NumPy, no FEM, no meshing. Physically it is textbook potential flow:
uniform stream psi = U*y at the far field, psi = const on the obstacle. The point of this
solver is to exercise the entire toolkit (geometry schema, job manager, progress streaming,
grid2d results, browser rendering) without FEniCSx installed; the numbers are plausible, not
publication-grade.
"""

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Domain2D
from .base import ProgressCallback, ProgressEvent, Solver, SolverResult
from .registry import register


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

    def solve(
        self, geometry: Domain2D, params: "MockLaplace2D.Params", progress: ProgressCallback
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        lx, ly = xmax - xmin, ymax - ymin
        if lx >= ly:
            nx = params.resolution
            ny = max(8, round(params.resolution * ly / lx))
        else:
            ny = params.resolution
            nx = max(8, round(params.resolution * lx / ly))

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
            new = psi.copy()
            new[1:-1, 1:-1] = 0.25 * (
                psi[1:-1, :-2] + psi[1:-1, 2:] + psi[:-2, 1:-1] + psi[2:, 1:-1]
            )
            new[mask] = psi_body  # Dirichlet on the body; edges untouched -> far-field Dirichlet
            residual = float(np.max(np.abs(new - psi)))
            psi = new
            if it % params.report_every == 0 or it == params.iterations:
                progress(
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

        return SolverResult(
            kind="grid2d",
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
