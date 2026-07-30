"""Mock solver: 2D incompressible potential flow around a polygonal obstacle.

Solves the Laplace equation for the streamfunction psi on a regular Cartesian grid with a
Jacobi iteration — pure NumPy, no FEM, no meshing. Physically it is textbook potential flow:
uniform stream at the far field, psi = const on the obstacle. The point of this solver is to
exercise the entire toolkit (geometry schema, job manager, progress streaming, cancellation,
artifacts, grid2d/mesh2d/series1d results, browser rendering) without FEniCSx installed; the
numbers are plausible, not publication-grade.

**Circulation is a solved-for quantity, not a leftover (issue #68).** Which constant the body's
streamline carries determines the circulation, and therefore the lift. This adapter used to
pick it by a heuristic — the free-stream value at the obstacle's centroid — which made lift an
artefact of an arbitrary choice, and said so nowhere. With `kutta` on, which is the default, it
runs the Laplace problem twice and picks that constant so the flow leaves the trailing edge
smoothly. The bookkeeping is in :mod:`fenixspoon.solvers.aerodynamics`; the cost is one extra
solve, and what it buys is `circulation`, `c_l`, `c_m_c4` and `x_cp` being real numbers.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Domain2D
from . import aerodynamics as aero
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import POTENTIAL_FLOW_ASSUMPTIONS, POTENTIAL_FLOW_METRICS, VTK_ARTIFACT
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
    x: np.ndarray,
    y: np.ndarray,
    mask: np.ndarray,
    fields: dict[str, np.ndarray],
    vector_fields: dict[str, np.ndarray] | None = None,
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

    payload = {
        "points": points.tolist(),
        "triangles": triangles.tolist(),
        "point_fields": {name: values[keep].ravel().tolist() for name, values in fields.items()},
    }
    if vector_fields:
        # A vector field is (ny, nx, 2), so `keep` selects rows of pairs rather than
        # scalars — the trailing axis survives the mask untouched.
        payload["point_vector_fields"] = {
            name: values[keep].reshape(-1, 2).tolist() for name, values in vector_fields.items()
        }
    return payload


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


def _cp_min(speed_max: float, u_inf: float) -> dict[str, float]:
    """Minimum pressure coefficient from the peak speed, as declared in `POTENTIAL_FLOW_METRICS`.

    Not a field reduction, which is why the adapter computes it rather than the generic path:
    it needs `u_inf`, and a parameter is not in the result. Undefined at zero free stream —
    the coefficient is normalised by the dynamic pressure, and there is none — so it is
    omitted rather than reported as an infinity a caller would have to special-case.
    """
    return {"cp_min": 1.0 - (speed_max / u_inf) ** 2} if u_inf else {}


class _Clock:
    """Progress across however many relaxations one solve is made of.

    The Kutta path runs the Laplace problem twice, and a progress stream that restarted at
    iteration 1 halfway through would read as a job going backwards. Counting sweeps across
    both and reporting the combined total keeps a progress bar monotonic, which is the only
    thing a progress bar is for.
    """

    def __init__(self, ctx: SolverContext, every: int, total: int) -> None:
        self._ctx = ctx
        self._every = every
        self.total = total
        self.done = 0

    def tick(self, residual: float, stage: str, *, final: bool = False) -> None:
        self.done += 1
        if self.done % self._every == 0 or final:
            # `stage` is what makes the stream readable across two solves. Residual is
            # monotone *within* a relaxation and jumps when the next one starts from its own
            # initial guess, so without a label a subscriber watching the number alone would
            # see the solve go backwards halfway through.
            self._ctx.progress(
                ProgressEvent(
                    iteration=self.done,
                    total=self.total,
                    residual=residual,
                    message=stage,
                )
            )


@register
class MockLaplace2D(Solver):
    name = "mock.laplace2d"
    title = "Potential flow (mock, NumPy)"
    description = (
        "2D incompressible potential flow around the obstacle: Laplace equation for the "
        "streamfunction on a Cartesian grid, Jacobi-iterated, with circulation set by the "
        "Kutta condition at the trailing edge so that lift is a real number. Development "
        "stand-in that runs anywhere NumPy does."
    )
    physics = "potential-flow"
    availability = "mock"
    metrics = POTENTIAL_FLOW_METRICS
    assumptions = POTENTIAL_FLOW_ASSUMPTIONS
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="fast preview",
            description=(
                "Finishes in a fraction of a second. Use it to check that a geometry edit did "
                "what you meant before paying for a resolved solve."
            ),
            params={"resolution": 64, "iterations": 400, "write_vtk": False},
        ),
        CapabilityExample(
            title="resolved",
            description="What the airfoil demo submits: a readable field in a few seconds.",
            params={"resolution": 256, "iterations": 4000},
        ),
        CapabilityExample(
            title="lift at incidence",
            description=(
                "Five degrees angle of attack, resolved enough for the two routes to lift to "
                "agree. Read `c_l` from `metrics` and the surface distribution from `series`."
            ),
            params={"resolution": 192, "iterations": 12000, "alpha": 5.0},
        ),
        CapabilityExample(
            title="flow field only, no circulation",
            description=(
                "The pre-#68 behaviour, kept because it is half the cost and the streamlines "
                "are all some callers want. Lift is meaningless in this mode and the "
                "`no_circulation` assumption says so."
            ),
            params={"resolution": 128, "iterations": 2000, "kutta": False},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=128, ge=16, le=512, description="Grid points along the longer domain edge"
        )
        iterations: int = Field(
            default=2000,
            ge=10,
            le=20000,
            description=(
                "Jacobi sweeps per solve. With `kutta` on there are two solves, so this is "
                "half the total work"
            ),
        )
        u_inf: float = Field(default=1.0, description="Free-stream speed")
        alpha: float = Field(
            default=0.0,
            ge=-30.0,
            le=30.0,
            description=(
                "Angle of attack in degrees. Rotates the free stream, not the geometry, so a "
                "sweep re-uses the same domain"
            ),
        )
        kutta: bool = Field(
            default=True,
            description=(
                "Fix the circulation by the Kutta condition at the trailing edge, which is "
                "what makes lift meaningful. Costs a second solve; turn it off for the flow "
                "field alone"
            ),
        )
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
        # Unchanged by `kutta`, deliberately. The budget this feeds is a *cell* budget — a
        # proxy for the memory a job needs — and the second solve reuses the same grid. It
        # doubles the wall clock, which is the job timeout's business, not this one's.
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    @staticmethod
    def _relax(
        edges: np.ndarray,
        body_value: float,
        mask: np.ndarray,
        params: "MockLaplace2D.Params",
        ctx: SolverContext,
        clock: _Clock,
        stage: str,
    ) -> tuple[np.ndarray, float]:
        """Jacobi-relax the Laplace problem for one set of boundary data.

        The boundary conditions arrive as the *initial field*: interior nodes are averaged and
        the domain edges are never written, so whatever ``edges`` holds there stays there as a
        Dirichlet condition, and ``body_value`` is re-imposed on the mask every sweep. That is
        what lets the same six lines produce both halves of the superposition — the free-stream
        solution and the circulatory one differ only in what is handed in here.
        """
        psi = np.asarray(edges, dtype=np.float64).copy()
        psi[mask] = body_value
        residual = float("inf")
        for sweep in range(1, params.iterations + 1):
            ctx.check_cancelled()
            new = psi.copy()
            new[1:-1, 1:-1] = 0.25 * (
                psi[1:-1, :-2] + psi[1:-1, 2:] + psi[:-2, 1:-1] + psi[2:, 1:-1]
            )
            new[mask] = body_value  # Dirichlet on the body; edges untouched -> far field
            residual = float(np.max(np.abs(new - psi)))
            psi = new
            settled = residual < 1e-9
            clock.tick(residual, stage, final=settled or sweep == params.iterations)
            if settled:
                break
        return psi, residual

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

        stream = aero.far_field_psi(xx, yy, params.u_inf, params.alpha)
        clock = _Clock(ctx, params.report_every, params.iterations * (2 if params.kutta else 1))
        # The streamline through the obstacle's centroid: what this adapter used to hold the
        # body at, and still does when there is no Kutta condition to do better.
        centroid = pts.mean(axis=0)
        heuristic = float(
            aero.far_field_psi(centroid[0], centroid[1], params.u_inf, params.alpha)
        )

        profile: aero.Profile | None = None
        aero_warnings: list[str] = []
        if not params.kutta:
            psi, residual = self._relax(
                stream, heuristic, mask, params, ctx, clock, "relaxing the streamfunction"
            )
        else:
            # The superposition #68 is built on: `psi_a` is the free stream with the body held
            # at zero, `psi_gamma` is the pure circulatory solution with the body at one, and
            # `psi_a + lam * psi_gamma` solves the same problem for *every* `lam`. So one extra
            # solve turns the body's streamfunction value from a heuristic into an unknown the
            # trailing edge can determine — and the condition that determines it is linear, so
            # there is no iteration on top.
            psi_a, residual_a = self._relax(
                stream, 0.0, mask, params, ctx, clock, "relaxing the free-stream mode"
            )
            psi_gamma, residual_g = self._relax(
                np.zeros_like(stream), 1.0, mask, params, ctx, clock,
                "relaxing the circulatory mode",
            )
            residual = max(residual_a, residual_g)
            body_value = heuristic
            try:
                profile = aero.profile_of(pts)
                body_value = aero.resolve_kutta(
                    profile,
                    aero.GridField(x=x, y=y, values=psi_a, mask=mask),
                    aero.GridField(x=x, y=y, values=psi_gamma, mask=mask),
                ).body_value
            except aero.AerodynamicsUnavailable as exc:
                # The field is still a valid potential flow — only the *choice* of circulation
                # failed — so the job succeeds with the heuristic body value and a warning
                # naming what is missing. Failing the whole solve would throw away a usable
                # flow field over a post-processing step.
                profile = None
                aero_warnings.append(aero.describe_unavailable(exc))
            psi = psi_a + body_value * psi_gamma

        # Velocity magnitude from psi: u = d(psi)/dy, v = -d(psi)/dx.
        dy_ = y[1] - y[0]
        dx_ = x[1] - x[0]
        u = np.gradient(psi, dy_, axis=0)
        v = -np.gradient(psi, dx_, axis=1)
        speed = np.sqrt(u**2 + v**2)
        speed[mask] = 0.0
        # The velocity itself, not just its magnitude. Direction is the whole reason to
        # look at a flow field, and until protocol 1.1 there was nowhere to put it — the
        # result kinds carried scalars only, so this adapter computed `u` and `v` and threw
        # the pair away. Zeroed inside the obstacle for the same reason `speed` is: there
        # is no flow there, and a stale gradient would draw arrows into the body.
        u_out = np.where(mask, 0.0, u)
        v_out = np.where(mask, 0.0, v)
        velocity = np.stack([u_out, v_out], axis=-1)

        # The aerodynamic post-processing reads the *combined* field, so it has to come after
        # the superposition rather than beside the solves that make it up.
        aerodynamics: aero.Aerodynamics | None = None
        if profile is not None:
            try:
                aerodynamics = aero.analyse(
                    profile=profile,
                    psi=psi,
                    speed=speed,
                    x=x,
                    y=y,
                    mask=mask,
                    u_inf=params.u_inf,
                    alpha_deg=params.alpha,
                )
            except aero.AerodynamicsUnavailable as exc:
                aero_warnings.append(aero.describe_unavailable(exc))

        if params.write_vtk:
            write_vtk_structured_points(
                ctx.artifact("solution.vtk"), x, y, {"psi": psi, "speed": speed}
            )

        stats = {"cells": float(nx * ny), "iterations": float(clock.done)}
        converged = residual < 1e-9
        warnings = list(aero_warnings)
        if not converged:
            warnings.append(
                f"stopped at the iteration cap ({params.iterations} sweeps per solve) with "
                f"residual {residual:.3g}; raise `iterations` for a converged field"
            )
        if not params.kutta:
            # Said on every run rather than left to the capability declaration. A caller that
            # reached this mode by accident is exactly the caller who will not have read the
            # `assumptions` section, and a zero lift with no explanation beside it is the
            # failure #68 was filed about.
            warnings.append(
                "no Kutta condition was imposed (`kutta` is false), so the circulation is an "
                "artefact of where the body's streamline was fixed and lift is not a physical "
                "result. `circulation`, `c_l`, `c_m_c4` and `x_cp` are therefore absent."
            )
        common = {
            "stats": stats,
            "metrics": {
                **_cp_min(float(speed[~mask].max(initial=0.0)), params.u_inf),
                **(aerodynamics.metrics if aerodynamics else {}),
            },
            "series": aerodynamics.series if aerodynamics else [],
            "converged": converged,
            "residual": residual,
            "warnings": warnings + (aerodynamics.warnings if aerodynamics else []),
        }
        if params.output == "mesh2d":
            return SolverResult(
                kind="mesh2d",
                **common,
                data={
                    "bounds": [xmin, ymin, xmax, ymax],
                    **grid_to_mesh2d(
                        x, y, mask, {"psi": psi, "speed": speed}, {"velocity": velocity}
                    ),
                },
            )

        return SolverResult(
            kind="grid2d",
            **common,
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {
                    "psi": psi.ravel().tolist(),
                    # `speed` stays alongside `velocity` rather than being derived by every
                    # client: the viewer colours by it on every frame, and recomputing a
                    # magnitude over ~170k points in JS to save a field on the wire is the
                    # wrong trade. Documented in the wire protocol so it is a decision, not
                    # an accident.
                    "speed": speed.ravel().tolist(),
                },
                "vector_fields": {"velocity": velocity.reshape(-1, 2).tolist()},
                "mask": mask.astype(np.uint8).ravel().tolist(),
            },
        )
