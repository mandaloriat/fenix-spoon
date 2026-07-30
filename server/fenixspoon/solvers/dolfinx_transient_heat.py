"""FEniCSx adapter: thermal start-up of a convectively cooled solid (issue #82).

The FEniCSx twin of ``mock.transient_heat2d``: same equation, same parameter names, same
declared metrics, on the Gmsh mesh of the solid that ``dolfinx.heat2d`` already builds.

    rho_cp dT/dt = div( k grad T ) + q ,   -k dT/dn = h (T - T_ambient)

**Backward Euler**, so the mass term joins the bilinear form and the previous step joins the
right-hand side:

    (rho_cp/dt) u v dx + k grad(u).grad(v) dx + h u v ds
        = q v dx + h T_ambient v ds + (rho_cp/dt) T_prev v dx

The left-hand side does not change between steps — a time-varying `h` or a
temperature-dependent `k` would break that, and both are excluded in the declared
assumptions rather than half-supported. This adapter does *not* exploit it: `LinearProblem`
re-assembles and re-factorises on every `solve()`, and keeping the loop that simple is worth
more here than the constant factor. Caching the factorisation is the obvious optimisation if
a long run ever needs it.

Everything about what a transient *cannot* return here is the same as in the mock, and is
written up there: the curves are the compact answer, the field is the final instant, and the
field history has nowhere to go until #82 grows one.
"""

import dolfinx
import gmsh  # noqa: F401  - availability gate, see solvers/__init__.py
import numpy as np
import ufl
from dolfinx import fem
from dolfinx.fem.petsc import LinearProblem
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from ..series import Series1DData, SeriesAxis, SeriesTrace
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import TRANSIENT_HEAT_ASSUMPTIONS, TRANSIENT_HEAT_METRICS, VTK_ARTIFACT
from .dolfinx_heat import _build_solid_mesh, _nodal_region_value, _solid_area
from .dolfinx_poisson import _nodal_speed, _p1_mesh_data, _write_vtk_unstructured
from .mock_transient_heat import ALUMINIUM_RHO_CP, crossing_time
from .registry import register

#: Same factor the steady adapter uses: `2A/h^2` is the equilateral-tiling floor and Gmsh
#: lands above it, so the estimate errs high on purpose.
_MESH_SAFETY_FACTOR = 1.4


@register
class DolfinxTransientHeat2D(Solver):
    name = "dolfinx.transient_heat2d"
    title = "Transient heat conduction (FEniCSx, region-tagged mesh)"
    description = (
        "Thermal start-up of a convectively cooled solid on an unstructured mesh: backward "
        "Euler in time, P1 elements in space, Robin condition on every exposed face. The "
        "compact answer is a pair of curves; the field is the final instant."
    )
    geometry_types = ["regions2d"]
    physics = "heat-conduction-transient"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh", "mpi4py", "petsc4py"]
    metrics = TRANSIENT_HEAT_METRICS
    assumptions = TRANSIENT_HEAT_ASSUMPTIONS
    #: One matrix, one factorisation, a fixed number of steps: same inputs, same answer (#47).
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="start-up from ambient",
            description="The sink switched on cold; read `time_to_90pc` off the curve.",
            params={"mesh_size": 0.004, "duration": 600.0, "steps": 60},
        ),
    ]

    class Params(BaseModel):
        mesh_size: float = Field(default=0.004, gt=0.0, le=1.0)
        duration: float = Field(
            default=600.0, gt=0.0, description="Simulated time in seconds, from t = 0."
        )
        steps: int = Field(
            default=60, ge=2, le=2000,
            description=(
                "Backward Euler steps. Unconditionally stable, so this sets accuracy and how "
                "finely the curves are sampled, not whether the solve survives."
            ),
        )
        h: float = Field(default=25.0, gt=0.0, le=10000.0)
        t_ambient: float = Field(default=20.0, ge=-273.15)
        t_initial: float | None = Field(
            default=None, description="Uniform initial temperature in °C; defaults to ambient."
        )
        rho_cp: float = Field(
            default=ALUMINIUM_RHO_CP, gt=0.0,
            description="Volumetric heat capacity in J/(m³·K) where a region declares none.",
        )
        write_vtk: bool = Field(default=True)

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "DolfinxTransientHeat2D.Params") -> int:
        # Cells times steps: the budget is about work, and a transient spends its mesh once
        # per step. Reporting only the mesh size would let a thousand-step run through a
        # gate sized for one solve.
        mesh = _MESH_SAFETY_FACTOR * 2.0 * _solid_area(geometry) / max(params.mesh_size, 1e-12) ** 2
        return int(mesh * params.steps)

    def solve(
        self, geometry: Regions2D, params: "DolfinxTransientHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        ctx.progress(ProgressEvent(iteration=0, total=params.steps, message="meshing with Gmsh"))
        msh, cell_tags = _build_solid_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        constants = fem.functionspace(msh, ("DG", 0))
        conductivity, source, capacity = (fem.Function(constants) for _ in range(3))
        conductivity.x.array[:] = 1.0
        source.x.array[:] = 0.0
        capacity.x.array[:] = params.rho_cp
        for index, region in enumerate(geometry.regions, start=1):
            cells = cell_tags.find(index)
            if len(cells) == 0:
                continue
            dofs = fem.locate_dofs_topological(constants, msh.topology.dim, cells)
            conductivity.x.array[dofs] = float(region.material.get("k", 1.0))
            source.x.array[dofs] = float(region.material.get("q", 0.0))
            capacity.x.array[dofs] = float(region.material.get("rho_cp", params.rho_cp))

        V = fem.functionspace(msh, ("Lagrange", 1))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        film = fem.Constant(msh, dolfinx.default_scalar_type(params.h))
        ambient = fem.Constant(msh, dolfinx.default_scalar_type(params.t_ambient))
        step = params.duration / params.steps
        inertia = capacity / fem.Constant(msh, dolfinx.default_scalar_type(step))

        initial = params.t_ambient if params.t_initial is None else params.t_initial
        previous = fem.Function(V)
        previous.x.array[:] = float(initial)

        a = (
            inertia * u * v * ufl.dx
            + conductivity * ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx
            + film * u * v * ufl.ds
        )
        rhs = (
            source * v * ufl.dx
            + film * ambient * v * ufl.ds
            + inertia * previous * v * ufl.dx
        )

        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            problem = LinearProblem(
                a, rhs, bcs=[],
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_transient_heat_",
            )
        except TypeError:  # older dolfinx without the prefix keyword
            problem = LinearProblem(a, rhs, bcs=[], petsc_options=petsc_options)

        times = [0.0]
        peak = [float(initial)]
        mean = [float(initial)]
        for index in range(1, params.steps + 1):
            ctx.check_cancelled()
            solved = problem.solve()
            current = solved[0] if isinstance(solved, tuple) else solved
            # `previous` is referenced by the form, so the next step reads whatever is in its
            # array — copying into it rather than rebinding is what makes the loop work.
            previous.x.array[:] = current.x.array
            values = np.asarray(current.x.array.real, dtype=float)
            times.append(index * step)
            peak.append(float(values.max()))
            mean.append(float(values.mean()))
            ctx.progress(
                ProgressEvent(
                    iteration=index, total=params.steps, message=f"t = {index * step:.3g} s"
                )
            )

        points, triangles, t_nodal = _p1_mesh_data(V, previous)
        k_nodal = _nodal_region_value(points, triangles, msh, cell_tags, geometry, "k", 1.0)
        flux_nodal = k_nodal * _nodal_speed(points, triangles, t_nodal)
        fields = {"T": t_nodal, "flux": flux_nodal, "k": k_nodal}
        if params.write_vtk:
            _write_vtk_unstructured(ctx.artifact("solution.vtk"), points, triangles, fields)

        history = np.asarray(peak)
        rise = history[-1] - initial
        settled = abs(history[-1] - history[-2]) < 0.01 * max(abs(rise), 1e-12)
        reached = crossing_time(np.asarray(times), history, initial + 0.9 * rise)
        capacity_values = np.asarray(capacity.x.array.real, dtype=float)

        metrics = {
            "t_rise": float(t_nodal.max() - params.t_ambient),
            "t_peak": float(history.max()),
            "time_constant": _lumped_time_constant(msh, capacity_values, params.h),
        }
        if reached is not None:
            metrics["time_to_90pc"] = reached

        xmin, ymin, xmax, ymax = geometry.bounds
        return SolverResult(
            kind="mesh2d",
            stats={
                "cells": float(len(triangles)),
                "dofs": float(V.dofmap.index_map.size_global),
                "steps": float(params.steps),
                "dt": step,
            },
            metrics=metrics,
            series=[
                Series1DData(
                    name="temperature_history",
                    description=(
                        "Peak and mean solid temperature against time. The compact answer to "
                        "a transient: the field below is only the final instant."
                    ),
                    x=SeriesAxis(name="time", unit="s", values=times),
                    traces=[
                        SeriesTrace(name="t_max", unit="degC", values=peak),
                        SeriesTrace(name="t_mean", unit="degC", values=mean),
                    ],
                )
            ],
            # A direct solve either succeeded or raised, and backward Euler cannot go
            # unstable, so there is no iteration here that could fail quietly.
            converged=True,
            warnings=(
                []
                if settled
                else [
                    f"still rising at t = {params.duration:g} s: the run stopped before "
                    "equilibrium, so `time_to_90pc` is 90% of the rise achieved so far"
                ]
            ),
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "points": points.tolist(),
                "triangles": triangles.tolist(),
                "point_fields": {name: value.tolist() for name, value in fields.items()},
            },
        )


def _lumped_time_constant(msh, capacity: np.ndarray, h: float) -> float:
    """`rho_cp V / (h A)` on the mesh: cell volumes for the store, boundary facets for the loss.

    Computed with UFL measures rather than by counting raster faces, because on an
    unstructured mesh the exposed area *is* the boundary integral — and `ds` covers the whole
    boundary here for the same reason the Robin condition does: the mesh is the solid.
    """
    one = fem.Constant(msh, dolfinx.default_scalar_type(1.0))
    volume = float(fem.assemble_scalar(fem.form(one * ufl.dx)).real)
    area = float(fem.assemble_scalar(fem.form(one * ufl.ds)).real)
    if area <= 0.0:
        return float("inf")
    return float(np.mean(capacity)) * volume / (h * area)
