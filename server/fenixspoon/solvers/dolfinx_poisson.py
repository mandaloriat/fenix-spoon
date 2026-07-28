"""FEniCSx adapter: the same potential-flow problem on an unstructured Gmsh mesh.

This module registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly — i.e.
inside the ``dolfinx/dolfinx`` Docker image or a conda env with ``fenics-dolfinx`` +
``python-gmsh``. Validated against dolfinx 0.11; tests live in
``tests/test_dolfinx_adapter.py`` (``pytest -m fenics``).

The solve mirrors ``mock.laplace2d`` semantically — Laplace for the streamfunction, uniform
stream at the far field, constant psi on the obstacle — so results are directly comparable.
The FEM solution is sampled back onto a regular grid to produce the same ``grid2d`` result
payload, keeping every client identical across the mock and real paths. Emitting the raw
unstructured mesh (``mesh2d`` result kind) is an M1 deliverable.
"""

import dolfinx
import gmsh  # noqa: F401  - availability gate, see solvers/__init__.py
import numpy as np
import ufl
from dolfinx import fem
from dolfinx import mesh as dmesh
from dolfinx.fem.petsc import LinearProblem
from mpi4py import MPI
from pydantic import BaseModel, Field

from ..geometry import Domain2D
from .base import ProgressEvent, Solver, SolverContext, SolverResult
from .registry import register


def _build_mesh(geometry: Domain2D, mesh_size: float):
    """Rectangle minus polygon, meshed with Gmsh (OpenCascade kernel)."""
    xmin, ymin, xmax, ymax = geometry.bounds
    # interruptible=False: skip gmsh's signal handlers, which cannot be installed from the
    # worker threads the job manager runs solvers on.
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        rect = occ.addRectangle(xmin, ymin, 0.0, xmax - xmin, ymax - ymin)
        pts = [occ.addPoint(x, y, 0.0, mesh_size) for x, y in geometry.obstacle.points]
        lines = [
            occ.addLine(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))
        ]
        loop = occ.addCurveLoop(lines)
        hole = occ.addPlaneSurface([loop])
        out, _ = occ.cut([(2, rect)], [(2, hole)])
        domain = out[0][1]
        occ.synchronize()
        gmsh.model.addPhysicalGroup(2, [domain], tag=1)
        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        gmsh.model.mesh.generate(2)
        from dolfinx.io.gmsh import model_to_mesh

        data = model_to_mesh(gmsh.model, MPI.COMM_WORLD, 0, gdim=2)
        # dolfinx >= 0.11 returns a MeshData object; earlier versions a (mesh, ct, ft) tuple.
        return data.mesh if hasattr(data, "mesh") else data[0]
    finally:
        gmsh.finalize()


@register
class DolfinxPotentialFlow2D(Solver):
    name = "dolfinx.potential_flow2d"
    title = "Potential flow (FEniCSx, unstructured mesh)"
    description = (
        "Laplace equation for the streamfunction on a Gmsh triangulation of the domain with "
        "the obstacle removed, solved with dolfinx (P1 elements, LU). Experimental M1 adapter."
    )

    class Params(BaseModel):
        mesh_size: float = Field(default=0.05, gt=0.0, description="Target element size")
        resolution: int = Field(
            default=128, ge=16, le=512, description="Sampling grid for the grid2d result"
        )
        u_inf: float = Field(default=1.0)

    def solve(
        self,
        geometry: Domain2D,
        params: "DolfinxPotentialFlow2D.Params",
        ctx: SolverContext,
    ) -> SolverResult:
        ctx.progress(ProgressEvent(iteration=0, total=4, message="meshing with Gmsh"))
        msh = _build_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=4, message="assembling"))
        V = fem.functionspace(msh, ("Lagrange", 1))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        a = ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx
        L = fem.Constant(msh, dolfinx.default_scalar_type(0.0)) * v * ufl.dx

        xmin, ymin, xmax, ymax = geometry.bounds
        obstacle_pts = np.asarray(geometry.obstacle.points)
        psi_body = float(obstacle_pts[:, 1].mean()) * params.u_inf
        eps = 1e-9

        def on_outer(x):
            return (
                (np.abs(x[0] - xmin) < eps)
                | (np.abs(x[0] - xmax) < eps)
                | (np.abs(x[1] - ymin) < eps)
                | (np.abs(x[1] - ymax) < eps)
            )

        tdim = msh.topology.dim
        msh.topology.create_connectivity(tdim - 1, tdim)
        boundary_facets = dmesh.exterior_facet_indices(msh.topology)
        outer_facets = dmesh.locate_entities_boundary(msh, tdim - 1, on_outer)
        body_facets = np.setdiff1d(boundary_facets, outer_facets)

        psi_inf = fem.Function(V)
        psi_inf.interpolate(lambda x: params.u_inf * x[1])
        bc_outer = fem.dirichletbc(
            psi_inf, fem.locate_dofs_topological(V, tdim - 1, outer_facets)
        )
        bc_body = fem.dirichletbc(
            dolfinx.default_scalar_type(psi_body),
            fem.locate_dofs_topological(V, tdim - 1, body_facets),
            V,
        )

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="solving (LU)"))
        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            # dolfinx >= 0.10 requires an options prefix.
            problem = LinearProblem(
                a, L, bcs=[bc_outer, bc_body],
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_",
            )
        except TypeError:  # older dolfinx without the keyword
            problem = LinearProblem(a, L, bcs=[bc_outer, bc_body], petsc_options=petsc_options)
        solved = problem.solve()
        # dolfinx >= 0.11 returns (function, convergence_reason, iterations).
        psi_h = solved[0] if isinstance(solved, tuple) else solved

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=3, total=4, message="sampling onto grid"))
        data = _sample_grid2d(msh, psi_h, geometry, params.resolution, obstacle_pts)
        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        return SolverResult(kind="grid2d", data=data)


def _sample_grid2d(msh, psi_h, geometry: Domain2D, resolution: int, obstacle_pts) -> dict:
    """Evaluate the FEM solution on a regular grid, reusing the mock solver's mask/format."""
    from dolfinx import geometry as dgeo

    from .mock_laplace import polygon_mask

    xmin, ymin, xmax, ymax = geometry.bounds
    lx, ly = xmax - xmin, ymax - ymin
    if lx >= ly:
        nx, ny = resolution, max(8, round(resolution * ly / lx))
    else:
        ny, nx = resolution, max(8, round(resolution * lx / ly))
    x = np.linspace(xmin, xmax, nx)
    y = np.linspace(ymin, ymax, ny)
    xx, yy = np.meshgrid(x, y)
    pts3 = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])

    tree = dgeo.bb_tree(msh, msh.topology.dim)
    candidates = dgeo.compute_collisions_points(tree, pts3)
    colliding = dgeo.compute_colliding_cells(msh, candidates, pts3)
    values = np.zeros(pts3.shape[0])
    have = np.zeros(pts3.shape[0], dtype=bool)
    eval_pts, eval_cells, eval_idx = [], [], []
    for i in range(pts3.shape[0]):
        cells_i = colliding.links(i)
        if len(cells_i) > 0:
            eval_pts.append(pts3[i])
            eval_cells.append(cells_i[0])
            eval_idx.append(i)
    if eval_pts:
        vals = psi_h.eval(np.asarray(eval_pts), np.asarray(eval_cells, dtype=np.int32))
        values[np.asarray(eval_idx)] = vals.ravel()
        have[np.asarray(eval_idx)] = True

    psi = values.reshape(ny, nx)
    mask = polygon_mask(np.asarray(obstacle_pts, dtype=float), xx, yy) | ~have.reshape(ny, nx)
    u = np.gradient(psi, y[1] - y[0], axis=0)
    v = -np.gradient(psi, x[1] - x[0], axis=1)
    speed = np.sqrt(u**2 + v**2)
    speed[mask] = 0.0
    return {
        "bounds": [xmin, ymin, xmax, ymax],
        "shape": [ny, nx],
        "fields": {"psi": psi.ravel().tolist(), "speed": speed.ravel().tolist()},
        "mask": mask.astype(np.uint8).ravel().tolist(),
    }
