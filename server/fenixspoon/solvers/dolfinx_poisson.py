"""FEniCSx adapter: the same potential-flow problem on an unstructured Gmsh mesh.

This module registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly — i.e.
inside the ``dolfinx/dolfinx`` Docker image or a conda env with ``fenics-dolfinx`` +
``python-gmsh``. Validated against dolfinx 0.11; tests live in
``tests/test_dolfinx_adapter.py`` (``pytest -m fenics``).

The solve mirrors ``mock.laplace2d`` semantically — Laplace for the streamfunction, uniform
stream at the far field, constant psi on the obstacle — so results are directly comparable.
Both result kinds are supported: ``grid2d`` samples the FEM solution onto a regular grid
(identical payload to the mock path), while ``mesh2d`` emits the actual P1 triangulation
with nodal fields. The solution is also attached as a legacy-VTK unstructured-grid artifact
that opens directly in ParaView.
"""

from typing import Literal

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
from ._gmsh import gmsh_session
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import POTENTIAL_FLOW_METRICS, VTK_ARTIFACT
from .mock_laplace import _cp_min
from .registry import register

# Equilateral triangles of side h tile an area A with 2A/h^2 of them, but that is a
# *floor*: the adapters set ``Mesh.MeshSizeMax``, so h is an upper bound on edge length
# and Gmsh's Delaunay refinement comes in finer. Measured against the airfoil domain the
# real count runs ~1.35x the ideal tiling; the factor below keeps the estimate on the
# conservative side of that with room for geometries that refine harder.
_MESH_SAFETY_FACTOR = 2.0


def estimate_triangles(bounds, mesh_size: float) -> int:
    """Conservative triangle count for a domain meshed at ``mesh_size``.

    Used only for the submit-time budget check, where over-estimating is the safe
    direction: refusing a job that would have fitted costs the user one parameter
    change, admitting one that does not costs the box.
    """
    xmin, ymin, xmax, ymax = bounds
    area = (xmax - xmin) * (ymax - ymin)
    return int(_MESH_SAFETY_FACTOR * 2.0 * area / max(mesh_size, 1e-12) ** 2)


def _build_mesh(geometry: Domain2D, mesh_size: float):
    """Rectangle minus polygon, meshed with Gmsh (OpenCascade kernel)."""
    xmin, ymin, xmax, ymax = geometry.bounds
    with gmsh_session():
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


@register
class DolfinxPotentialFlow2D(Solver):
    name = "dolfinx.potential_flow2d"
    title = "Potential flow (FEniCSx, unstructured mesh)"
    description = (
        "Laplace equation for the streamfunction on a Gmsh triangulation of the domain with "
        "the obstacle removed, solved with dolfinx (P1 elements, LU). Experimental M1 adapter."
    )
    physics = "potential-flow"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh"]
    metrics = POTENTIAL_FLOW_METRICS
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="coarse mesh",
            description="A few thousand triangles: quick, and enough to check the setup meshes.",
            params={"mesh_size": 0.08, "write_vtk": False},
        ),
        CapabilityExample(
            title="resolved, on the FEM mesh",
            description=(
                "Returns the triangulation itself rather than a sampling grid, which is what "
                "makes the unstructured solve worth running — no resampling in between."
            ),
            params={"mesh_size": 0.02, "output": "mesh2d"},
        ),
    ]

    class Params(BaseModel):
        mesh_size: float = Field(default=0.05, gt=0.0, description="Target element size")
        resolution: int = Field(
            default=128, ge=16, le=512, description="Sampling grid for the grid2d result"
        )
        u_inf: float = Field(default=1.0)
        output: Literal["grid2d", "mesh2d"] = Field(
            default="grid2d",
            description="Result kind: regular sampling grid, or the FEM triangulation itself",
        )
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Domain2D, params: "DolfinxPotentialFlow2D.Params") -> int:
        return estimate_triangles(geometry.bounds, params.mesh_size)

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
        ctx.progress(ProgressEvent(iteration=3, total=4, message="post-processing"))
        # Extracting the triangulation costs O(cells); skip it when nothing needs it.
        if params.output == "mesh2d" or params.write_vtk:
            points, triangles, psi_nodal = _p1_mesh_data(V, psi_h)
            speed_nodal = _nodal_speed(points, triangles, psi_nodal)

        if params.write_vtk:
            _write_vtk_unstructured(
                ctx.artifact("solution.vtk"),
                points,
                triangles,
                {"psi": psi_nodal, "speed": speed_nodal},
            )

        if params.output == "mesh2d":
            data = {
                "bounds": [xmin, ymin, xmax, ymax],
                "points": points.tolist(),
                "triangles": triangles.tolist(),
                "point_fields": {
                    "psi": psi_nodal.tolist(),
                    "speed": speed_nodal.tolist(),
                },
            }
        else:
            data = _sample_grid2d(msh, psi_h, geometry, params.resolution, obstacle_pts)
        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        stats = {
            "cells": float(msh.topology.index_map(msh.topology.dim).size_local),
            "dofs": float(V.dofmap.index_map.size_local),
        }
        speed_key = "fields" if params.output == "grid2d" else "point_fields"
        peak_speed = max(data[speed_key]["speed"], default=0.0)
        return SolverResult(
            kind=params.output,
            data=data,
            stats=stats,
            metrics=_cp_min(float(peak_speed), params.u_inf),
            # A direct LU factorisation does not iterate toward a tolerance, so
            # `converged` is True by construction and there is no residual to report.
            # Saying so beats leaving both null, which reads as "nobody checked".
            converged=True,
        )


def _p1_mesh_data(V, psi_h):
    """Extract the P1 triangulation in dof ordering: points (N,2), triangles (M,3), psi (N,)."""
    points = V.tabulate_dof_coordinates()[:, :2].copy()
    msh = V.mesh
    num_cells = msh.topology.index_map(msh.topology.dim).size_local
    triangles = np.asarray(
        [V.dofmap.cell_dofs(c) for c in range(num_cells)], dtype=np.int64
    )
    psi = np.asarray(psi_h.x.array.real, dtype=float)[: len(points)]
    return points, triangles, psi


def _nodal_speed(points, triangles, psi):
    """|velocity| = |grad psi| per node: constant P1 cell gradients, area-averaged at nodes."""
    p = points[triangles]  # (M, 3, 2)
    x1, y1 = p[:, 0, 0], p[:, 0, 1]
    x2, y2 = p[:, 1, 0], p[:, 1, 1]
    x3, y3 = p[:, 2, 0], p[:, 2, 1]
    area2 = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
    area2 = np.where(np.abs(area2) < 1e-30, 1e-30, area2)
    bx = np.stack([y2 - y3, y3 - y1, y1 - y2], axis=1) / area2[:, None]
    by = np.stack([x3 - x2, x1 - x3, x2 - x1], axis=1) / area2[:, None]
    vals = psi[triangles]
    gx = (bx * vals).sum(axis=1)
    gy = (by * vals).sum(axis=1)
    cell_speed = np.hypot(gx, gy)
    weight = np.abs(area2) / 2.0
    acc = np.zeros(len(points))
    wacc = np.zeros(len(points))
    np.add.at(acc, triangles, (cell_speed * weight)[:, None].repeat(3, axis=1))
    np.add.at(wacc, triangles, weight[:, None].repeat(3, axis=1))
    return acc / np.maximum(wacc, 1e-30)


def _write_vtk_unstructured(path, points, triangles, fields) -> None:
    """Write a legacy-VTK UNSTRUCTURED_GRID file (opens directly in ParaView)."""
    with open(path, "w") as f:
        f.write("# vtk DataFile Version 3.0\n")
        f.write("fenixspoon mesh2d result\n")
        f.write("ASCII\nDATASET UNSTRUCTURED_GRID\n")
        f.write(f"POINTS {len(points)} double\n")
        np.savetxt(f, np.column_stack([points, np.zeros(len(points))]), fmt="%.9g")
        f.write(f"CELLS {len(triangles)} {len(triangles) * 4}\n")
        np.savetxt(
            f,
            np.column_stack([np.full(len(triangles), 3), triangles]),
            fmt="%d",
        )
        f.write(f"CELL_TYPES {len(triangles)}\n")
        np.savetxt(f, np.full(len(triangles), 5), fmt="%d")  # 5 = VTK_TRIANGLE
        f.write(f"POINT_DATA {len(points)}\n")
        for name, values in fields.items():
            f.write(f"SCALARS {name} double 1\nLOOKUP_TABLE default\n")
            np.savetxt(f, np.asarray(values), fmt="%.9g")


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
