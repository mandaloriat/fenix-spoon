"""FEniCSx adapter: conduction with a temperature-dependent conductivity, by Newton (#107).

Same problem as ``mock.heat_nonlinear2d``:

    -div( k(T) grad T ) = 0 ,   k(T) = k0 (1 + beta (T - T_ref))

with two faces held at fixed temperatures and every other face insulated. The twin freezes
`k` and re-solves (Picard); this one assembles the Jacobian and takes Newton steps. **They
converge to the same fixed point by different routes**, which is what makes their agreement
evidence rather than a shared bug — and unlike the modal pair they are not expected to
bracket the answer, because there is only one answer. So the primary check for both is the
Kirchhoff closed form, and each other is the second.

The residual they report is *not* the same quantity, which is exactly why 1.16 makes a
capability declare what its residual measures. Picard's is a step size between iterates;
Newton's is the norm of the nonlinear form itself, relative to the first iteration. Comparing
the two numbers would be comparing a temperature change to a heat balance.

Registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly. Tests live in
``tests/test_dolfinx_heat_nonlinear.py`` (``pytest -m fenics``).
"""

import dolfinx
import gmsh  # noqa: F401  - availability gate, see solvers/__init__.py
import numpy as np
import ufl
from dolfinx import fem, mesh
from mpi4py import MPI
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from ..series import Series1DData, SeriesAxis, SeriesTrace
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import (
    NEWTON_CONVERGENCE,
    NONLINEAR_HEAT_ASSUMPTIONS,
    NONLINEAR_HEAT_METRICS,
    VTK_ARTIFACT,
)
from .dolfinx_poisson import (
    _MESH_SAFETY_FACTOR,
    _nodal_speed,
    _p1_mesh_data,
    _write_vtk_unstructured,
)
from .mock_heat_nonlinear import MIN_CONDUCTIVITY, Edge
from .registry import register


def edge_locator(bounds: tuple[float, float, float, float], edge: Edge):
    """`f(x) -> bool` for one side of the bounding box, on the same tolerance as elsewhere."""
    xmin, ymin, xmax, ymax = bounds
    span = max(xmax - xmin, ymax - ymin)
    tol = 1e-6 * span
    axis, value = ({"xmin": (0, xmin), "xmax": (0, xmax), "ymin": (1, ymin), "ymax": (1, ymax)})[
        edge
    ]
    return lambda x: np.isclose(x[axis], value, atol=tol)


@register
class DolfinxNonlinearHeat2D(Solver):
    name = "dolfinx.heat_nonlinear2d"
    title = "Temperature-dependent conduction (FEniCSx, Newton)"
    description = (
        "Steady conduction with k(T) = k0 (1 + beta (T - T_ref)) per region, on an "
        "unstructured triangular mesh, solved by Newton's method with an assembled "
        "Jacobian. Two faces are held at fixed temperatures and the rest are insulated."
    )
    geometry_types = ["regions2d"]
    physics = "heat-conduction-nonlinear"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh", "mpi4py", "petsc4py"]
    convergence = NEWTON_CONVERGENCE
    metrics = NONLINEAR_HEAT_METRICS
    assumptions = NONLINEAR_HEAT_ASSUMPTIONS
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="coarse mesh",
            description="A quick run: Newton's iteration count barely moves with the mesh.",
            params={"mesh_size": 0.05, "t_hot": 300.0, "write_vtk": False},
        ),
    ]

    class Params(BaseModel):
        mesh_size: float = Field(
            default=0.02, gt=0.0, le=1.0, description="Target element size, in domain units"
        )
        max_iterations: int = Field(
            default=30, ge=1, le=200, description="Maximum Newton iterations"
        )
        tolerance: float = Field(
            default=1e-8,
            gt=0.0,
            le=1e-1,
            description=(
                "Relative tolerance on the nonlinear residual. The capability's "
                "`convergence` declaration says what happens when it is not reached."
            ),
        )
        hot_edge: Edge = Field(default="xmin", description="Edge held at `t_hot`")
        cold_edge: Edge = Field(default="xmax", description="Edge held at `t_cold`")
        t_hot: float = Field(default=200.0, description="Temperature of the hot edge, °C")
        t_cold: float = Field(default=20.0, description="Temperature of the cold edge, °C")
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "DolfinxNonlinearHeat2D.Params") -> int:
        xmin, ymin, xmax, ymax = geometry.bounds
        area = (xmax - xmin) * (ymax - ymin)
        return int(_MESH_SAFETY_FACTOR * area / (params.mesh_size**2))

    def solve(
        self, geometry: Regions2D, params: "DolfinxNonlinearHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        if params.hot_edge == params.cold_edge:
            raise ValueError(
                f"hot_edge and cold_edge are both {params.hot_edge!r}; one face cannot be "
                "held at two temperatures, and with only one fixed face the problem has no "
                "unique solution"
            )
        xmin, ymin, xmax, ymax = geometry.bounds
        ctx.progress(ProgressEvent(iteration=0, total=3, message="meshing"))
        nx = max(2, int(round((xmax - xmin) / params.mesh_size)))
        ny = max(2, int(round((ymax - ymin) / params.mesh_size)))
        msh = mesh.create_rectangle(
            MPI.COMM_WORLD, [(xmin, ymin), (xmax, ymax)], (nx, ny), mesh.CellType.triangle
        )
        # Locating DG0 dofs *by cell* needs the cell-to-cell connectivity, and a freshly
        # created mesh does not have it — `locate_dofs_topological` raises "Missing dims 2->2"
        # rather than computing it on demand. The linear heat adapter never hit this because
        # its mesh comes from `gmshio`, which builds the connectivity while reading the tags.
        msh.topology.create_connectivity(msh.topology.dim, msh.topology.dim)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=3, message="assembling"))
        V = fem.functionspace(msh, ("Lagrange", 1))
        Q = fem.functionspace(msh, ("DG", 0))
        k0, beta, t_ref = (fem.Function(Q) for _ in range(3))
        _fill_materials(msh, Q, geometry, k0, beta, t_ref)

        temperature = fem.Function(V, name="T")
        temperature.x.array[:] = 0.5 * (params.t_hot + params.t_cold)

        bcs = []
        for edge, value in ((params.hot_edge, params.t_hot), (params.cold_edge, params.t_cold)):
            facets = mesh.locate_entities_boundary(
                msh, msh.topology.dim - 1, edge_locator(geometry.bounds, edge)
            )
            dofs = fem.locate_dofs_topological(V, msh.topology.dim - 1, facets)
            bcs.append(fem.dirichletbc(dolfinx.default_scalar_type(value), dofs, V))

        v = ufl.TestFunction(V)
        # `ufl.max_value` rather than a Python max: the clamp has to be part of the form so
        # that the Jacobian differentiates the same function the residual evaluates. Doing it
        # by hand on the array would give Newton a derivative of something else.
        k = ufl.max_value(k0 * (1.0 + beta * (temperature - t_ref)), MIN_CONDUCTIVITY)
        residual_form = k * ufl.inner(ufl.grad(temperature), ufl.grad(v)) * ufl.dx
        jacobian_form = ufl.derivative(residual_form, temperature)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=3, message="solving (Newton)"))
        history = _newton(residual_form, jacobian_form, temperature, bcs, params, ctx)
        residual = history[-1] if history else 0.0

        points, triangles, t_nodal = _p1_mesh_data(V, temperature)
        k_expr = fem.Expression(k, V.element.interpolation_points)
        k_function = fem.Function(V)
        k_function.interpolate(k_expr)
        _, _, k_nodal = _p1_mesh_data(V, k_function)
        flux_nodal = k_nodal * _nodal_speed(points, triangles, t_nodal)

        fields = {"T": t_nodal, "flux": flux_nodal, "k": k_nodal}
        if params.write_vtk:
            _write_vtk_unstructured(ctx.artifact("solution.vtk"), points, triangles, fields)

        ctx.progress(ProgressEvent(iteration=3, total=3, message="done"))
        return SolverResult(
            kind="mesh2d",
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "points": points.tolist(),
                "triangles": triangles.tolist(),
                "point_fields": {name: value.tolist() for name, value in fields.items()},
            },
            stats={
                "cells": float(msh.topology.index_map(msh.topology.dim).size_local),
                "dofs": float(V.dofmap.index_map.size_local),
            },
            metrics={"k_ratio": float(k_nodal.max() / max(k_nodal.min(), MIN_CONDUCTIVITY))},
            converged=residual < params.tolerance,
            residual=float(residual),
            iterations=len(history),
            series=[
                Series1DData(
                    name="convergence",
                    description="Relative nonlinear residual per Newton iteration.",
                    x=SeriesAxis(
                        name="iteration",
                        unit="1",
                        values=[float(i) for i in range(1, len(history) + 1)],
                    ),
                    traces=[
                        SeriesTrace(name="residual", unit="1", values=[float(r) for r in history])
                    ],
                )
            ],
        )


def _fill_materials(msh, Q, geometry: Regions2D, k0, beta, t_ref) -> None:
    """Paint `k0`, `beta` and `t_ref` onto the cells, in the geometry's painter order.

    Point-in-polygon on cell midpoints rather than a tagged Gmsh mesh: this adapter meshes
    the whole bounding rectangle (there is no fluid to leave out here, unlike the linear heat
    pair), so the regions select cells rather than define the domain.
    """
    from .mock_laplace import polygon_mask

    tdim = msh.topology.dim
    cells = np.arange(msh.topology.index_map(tdim).size_local, dtype=np.int32)
    centres = dolfinx.mesh.compute_midpoints(msh, tdim, cells)

    for array, key, default in ((k0, "k", 1.0), (beta, "beta", 0.0), (t_ref, "t_ref", 20.0)):
        array.x.array[:] = float(geometry.background.get(key, default))
    for region in geometry.regions:
        inside = polygon_mask(
            np.asarray(region.shape.points, dtype=float), centres[:, 0], centres[:, 1]
        )
        selected = cells[inside]
        if len(selected) == 0:
            continue
        dofs = fem.locate_dofs_topological(Q, tdim, selected)
        for array, key, default in ((k0, "k", 1.0), (beta, "beta", 0.0), (t_ref, "t_ref", 20.0)):
            array.x.array[dofs] = float(region.material.get(key, default))


def _newton(residual_form, jacobian_form, temperature, bcs, params, ctx) -> list[float]:
    """Newton's method, written out rather than driven by `NewtonSolver`.

    Two reasons, and the second is the one that matters. `dolfinx.nls.petsc.NewtonSolver`
    has moved between releases — its constructor, its `solve` return and where the
    convergence criterion is set have all changed — and this repository already carries two
    version shims for that class of drift. And the residual history is the thing 1.16 asks a
    capability to report: driving the loop here means the numbers reported are the ones the
    iteration actually used, rather than whatever a solver object was persuaded to expose.
    """
    from dolfinx.fem import petsc as fem_petsc
    from petsc4py import PETSc

    jacobian = fem.form(jacobian_form)
    residual = fem.form(residual_form)
    correction = fem.Function(temperature.function_space)

    # `assemble_matrix(form)` / `assemble_vector(form)` — the variants that *return* a fresh
    # object — rather than pre-allocating with `create_matrix` / `create_vector` and filling
    # in place. The pre-allocating pair took a single form at 0.9 and takes a list at 0.11,
    # where a lone form fails with "'Form' object is not iterable"; the returning pair has
    # been stable across both, and is what `dolfinx_modal.py` already uses. Reallocating per
    # iteration costs a sparsity pattern each time, which is a real cost and a small one next
    # to the LU factorisation in the same loop.
    history: list[float] = []
    reference = 0.0
    for iteration in range(1, params.max_iterations + 1):
        ctx.check_cancelled()
        A = fem_petsc.assemble_matrix(jacobian, bcs=bcs)
        A.assemble()
        b = fem_petsc.assemble_vector(residual)

        # The correction is zero on a Dirichlet face, so the *lifted* residual is what the
        # iteration is actually driving down — measuring the raw one would report a number
        # the boundary conditions were never going to reduce.
        _apply_lifting(fem_petsc, b, jacobian, bcs, temperature)
        b.ghostUpdate(addv=PETSc.InsertMode.ADD, mode=PETSc.ScatterMode.REVERSE)
        fem_petsc.set_bc(b, bcs, temperature.x.petsc_vec, -1.0)

        norm = b.norm(PETSc.NormType.NORM_2)
        reference = norm if iteration == 1 else reference
        relative = norm / reference if reference > 0 else 0.0
        history.append(relative)
        ctx.progress(
            ProgressEvent(iteration=iteration, total=params.max_iterations, residual=relative)
        )
        if relative < params.tolerance:
            A.destroy()
            b.destroy()
            break

        solver = PETSc.KSP().create(temperature.function_space.mesh.comm)
        solver.setOperators(A)
        solver.setType("preonly")
        solver.getPC().setType("lu")
        solver.solve(b, correction.x.petsc_vec)
        solver.destroy()
        correction.x.scatter_forward()
        temperature.x.array[:] += correction.x.array
        temperature.x.scatter_forward()
        A.destroy()
        b.destroy()

    return history


def _apply_lifting(fem_petsc, b, jacobian, bcs, temperature) -> None:
    """`apply_lifting`, whichever name this dolfinx gives the scale factor.

    Renamed from `scale` to `alpha` at 0.9. Same shim shape as `dolfinx_modal.py`'s
    `diagonal`/`diag`, and kept beside the call so the reason travels with it.
    """
    x0 = [temperature.x.petsc_vec]
    try:
        fem_petsc.apply_lifting(b, [jacobian], [bcs], x0=x0, alpha=-1.0)
    except TypeError:  # dolfinx <= 0.8 spelled it `scale`
        fem_petsc.apply_lifting(b, [jacobian], [bcs], x0=x0, scale=-1.0)
