"""FEniCSx adapter: the same potential-flow problem on an unstructured Gmsh mesh.

This module registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly — i.e.
inside the ``dolfinx/dolfinx`` Docker image or a conda env with ``fenics-dolfinx`` +
``python-gmsh``. Validated against dolfinx 0.11; tests live in
``tests/test_dolfinx_adapter.py`` (``pytest -m fenics``).

The solve mirrors ``mock.laplace2d`` semantically — Laplace for the streamfunction, uniform
stream at the far field, constant psi on the obstacle, circulation set by the Kutta condition
at the trailing edge — so results are directly comparable. Both result kinds are supported:
``grid2d`` samples the FEM solution onto a regular grid (identical payload to the mock path),
while ``mesh2d`` emits the actual P1 triangulation with nodal fields. The solution is also
attached as a legacy-VTK unstructured-grid artifact that opens directly in ParaView.

**The Kutta condition is imposed on the mesh; the surface integrals run on a sampled grid.**
That split is deliberate and the line between the two halves is where it is for a reason.

With the Kutta condition on (issue #68) this adapter solves twice and superposes, exactly as the
mock does. The condition itself needs only **two point evaluations per basis solution**, so it
is answered from the triangulation directly (``_MeshProbe``): the body's streamfunction value is
a *Dirichlet value of the field this adapter returns*, not post-processing, and resolving it by
Cartesian interpolation would have meant `mesh_size` alone could not converge `c_l` — with the
trailing edge, the one place an unstructured mesh is decisively better, being the one place a
raster got consulted.

The **surface pressure integral** is a different matter, and it is handed the sampled field on
purpose. A facet integral over the real body would be more accurate — this mesh resolves the
trailing edge properly and would not be approximating a curved surface by a staircase. What
sampling buys instead is that `mock.laplace2d` and `dolfinx.potential_flow2d` are cross-validated
through *one* implementation of the loads rather than two, which is the property
`docs/gallery.md` relies on. The facet-integral version is a real improvement left undone, not
an oversight.
"""

from functools import partial
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
from . import aerodynamics as aero
from ._gmsh import gmsh_session
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import POTENTIAL_FLOW_ASSUMPTIONS, POTENTIAL_FLOW_METRICS, VTK_ARTIFACT
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
        "the obstacle removed, solved with dolfinx (P1 elements, LU), with circulation set by "
        "the Kutta condition at the trailing edge. Experimental M1 adapter."
    )
    physics = "potential-flow"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh"]
    metrics = POTENTIAL_FLOW_METRICS
    assumptions = POTENTIAL_FLOW_ASSUMPTIONS
    #: Single-rank Gmsh meshing followed by a direct LU factorisation — both reproducible for
    #: the same inputs and the same library versions, which the cache key includes. The
    #: caveat is MPI: a multi-rank run decomposes differently and would not reproduce, which
    #: is exactly what `features.mpi = False` already says this adapter does not support (#47).
    deterministic = True
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
        CapabilityExample(
            title="lift at incidence",
            description=(
                "Five degrees angle of attack. `resolution` matters here even for a `mesh2d` "
                "result: it is the grid the aerodynamic post-processing samples onto."
            ),
            params={"mesh_size": 0.02, "resolution": 256, "alpha": 5.0},
        ),
    ]

    class Params(BaseModel):
        mesh_size: float = Field(default=0.05, gt=0.0, description="Target element size")
        resolution: int = Field(
            default=128,
            ge=16,
            le=512,
            description=(
                "Sampling grid for the grid2d result, and for the aerodynamic "
                "post-processing — so it affects `c_l` even when `output` is mesh2d"
            ),
        )
        u_inf: float = Field(default=1.0, description="Free-stream speed")
        alpha: float = Field(
            default=0.0,
            ge=-30.0,
            le=30.0,
            description=(
                "Angle of attack in degrees. Rotates the free stream, not the geometry, so a "
                "sweep re-uses the same mesh"
            ),
        )
        kutta: bool = Field(
            default=True,
            description=(
                "Fix the circulation by the Kutta condition at the trailing edge, which is "
                "what makes lift meaningful. Costs a second solve"
            ),
        )
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
        # mesh, assemble, solve, [circulatory solve, Kutta], post-process — then `done`.
        stage = _Stages(ctx, total=6 if params.kutta else 4)
        stage.report("meshing with Gmsh")
        msh = _build_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        stage.report("assembling")
        V = fem.functionspace(msh, ("Lagrange", 1))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        a = ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx
        L = fem.Constant(msh, dolfinx.default_scalar_type(0.0)) * v * ufl.dx

        xmin, ymin, xmax, ymax = geometry.bounds
        obstacle_pts = np.asarray(geometry.obstacle.points)
        heuristic = aero.centroid_body_value(obstacle_pts, params.u_inf, params.alpha)
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
        outer_dofs = fem.locate_dofs_topological(V, tdim - 1, outer_facets)
        body_dofs = fem.locate_dofs_topological(V, tdim - 1, body_facets)

        psi_inf = fem.Function(V)
        # `aero.far_field_psi` rather than the expression inline: incidence has one definition,
        # and this is the adapter's actual Dirichlet data. Two copies would let the boundary
        # condition disagree with the centroid heuristic above it and with `mock.laplace2d`,
        # which is exactly the cross-validation this module leans on.
        psi_inf.interpolate(
            lambda x: aero.far_field_psi(x[0], x[1], params.u_inf, params.alpha)
        )
        zero = fem.Function(V)  # already zero: the far field of the circulatory mode

        def run(outer, body_value):
            """One Laplace solve for one set of Dirichlet data."""
            bcs = [
                fem.dirichletbc(outer, outer_dofs),
                fem.dirichletbc(dolfinx.default_scalar_type(body_value), body_dofs, V),
            ]
            options = {"ksp_type": "preonly", "pc_type": "lu"}
            try:
                # dolfinx >= 0.10 requires an options prefix.
                problem = LinearProblem(
                    a, L, bcs=bcs,
                    petsc_options=options,
                    petsc_options_prefix="fenixspoon_",
                )
            except TypeError:  # older dolfinx without the keyword
                problem = LinearProblem(a, L, bcs=bcs, petsc_options=options)
            solved = problem.solve()
            # dolfinx >= 0.11 returns (function, convergence_reason, iterations).
            return solved[0] if isinstance(solved, tuple) else solved

        stage.report("solving (LU)")
        profile: aero.Profile | None = None
        kutta_ok = False
        aero_warnings: list[str] = []

        if not params.kutta:
            psi_h = run(psi_inf, heuristic)
        else:
            # Same superposition as the mock: solve for the free-stream part with the body at
            # zero, then for the pure circulatory part with the body at one, and the body value
            # becomes an unknown the trailing edge determines. Two factorisations of the *same*
            # matrix modulo boundary rows, which is why this is a doubling rather than worse.
            psi_a = run(psi_inf, 0.0)
            ctx.check_cancelled()
            stage.report("solving (circulatory mode)")
            psi_gamma = run(zero, 1.0)

            ctx.check_cancelled()
            stage.report("imposing the Kutta condition")
            body_value = heuristic
            try:
                profile = aero.profile_of(obstacle_pts)
                # Probed on the triangulation, not on a sampling grid. `body_value` is a
                # Dirichlet value of the field returned below, so reading it off a raster would
                # cap `c_l`'s accuracy at the raster's — at the trailing edge, of all places.
                probe = _MeshProbe(msh)
                body_value = aero.resolve_kutta(
                    profile,
                    partial(probe.at, psi_a),
                    partial(probe.at, psi_gamma),
                    step=params.mesh_size,
                )
                kutta_ok = True
            except aero.AerodynamicsUnavailable as exc:
                # A valid potential flow with an arbitrary circulation beats no result at all,
                # so the job succeeds and says which metrics are missing and why.
                aero_warnings.append(aero.describe_unavailable(exc))
            psi_h = fem.Function(V)
            psi_h.x.array[:] = psi_a.x.array + body_value * psi_gamma.x.array

        ctx.check_cancelled()
        stage.report("post-processing")
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

        # A grid is needed to *report* a grid2d payload, and to integrate the surface pressure.
        # Neither is wanted for a mesh2d result with no circulation, so it is built lazily —
        # point location over `resolution^2` points is the expensive part of this adapter after
        # meshing and the factorisation.
        sampler = (
            _GridSampler(msh, geometry, params.resolution, obstacle_pts)
            if params.output == "grid2d" or kutta_ok
            else None
        )
        grid_psi = sampler.sample(psi_h) if sampler is not None else None
        # Computed once and passed on: `payload` used to recompute it, which cost two extra
        # full-grid gradients on the commonest path.
        grid_speed = (
            sampler.speed(grid_psi) if sampler is not None and grid_psi is not None else None
        )

        aerodynamics: aero.Aerodynamics | None = None
        if kutta_ok and profile is not None and sampler is not None:
            try:
                aerodynamics = aero.analyse(
                    profile=profile,
                    psi=grid_psi,
                    speed=grid_speed,
                    x=sampler.x,
                    y=sampler.y,
                    mask=sampler.mask,
                    u_inf=params.u_inf,
                    alpha_deg=params.alpha,
                )
            except aero.AerodynamicsUnavailable as exc:
                aero_warnings.append(aero.describe_unavailable(exc))

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
            assert sampler is not None and grid_psi is not None  # grid2d always builds one
            data = sampler.payload(grid_psi, grid_speed)
        stage.done()
        stats = {
            "cells": float(msh.topology.index_map(msh.topology.dim).size_local),
            "dofs": float(V.dofmap.index_map.size_local),
        }
        speed_key = "fields" if params.output == "grid2d" else "point_fields"
        peak_speed = max(data[speed_key]["speed"], default=0.0)
        if not params.kutta:
            aero_warnings.append(aero.NO_CIRCULATION_WARNING)
        return SolverResult(
            kind=params.output,
            data=data,
            stats=stats,
            metrics={
                **_cp_min(float(peak_speed), params.u_inf),
                **(aerodynamics.metrics if aerodynamics else {}),
            },
            series=aerodynamics.series if aerodynamics else [],
            # A direct LU factorisation does not iterate toward a tolerance, so
            # `converged` is True by construction and there is no residual to report.
            # Saying so beats leaving both null, which reads as "nobody checked".
            converged=True,
            warnings=aero_warnings + (aerodynamics.warnings if aerodynamics else []),
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


class _Stages:
    """Sequential progress reporting for a solve whose stage count depends on its params.

    The stage indices used to be written out as literals — `iteration=2`, `iteration=3`,
    `iteration=total - 1` — against a `total` that was itself `6 if kutta else 5`. That encoded
    the branch structure twice, left the non-Kutta stream jumping 2 → 4 → 5, and meant inserting
    a stage required finding every literal. Counting is the whole job.
    """

    def __init__(self, ctx: SolverContext, total: int) -> None:
        self._ctx = ctx
        self._total = total
        self._done = 0

    def report(self, message: str) -> None:
        self._done += 1
        self._ctx.progress(
            ProgressEvent(iteration=self._done, total=self._total, message=message)
        )

    def done(self) -> None:
        self._ctx.progress(
            ProgressEvent(iteration=self._total, total=self._total, message="done")
        )


class _MeshProbe:
    """Evaluates P1 functions at arbitrary points on the triangulation.

    This is what lets the Kutta condition be answered on the mesh rather than on a sampling
    grid. It is a *point* query — two per basis solution — so it shares nothing with
    :class:`_GridSampler` beyond the dolfinx calls: that class locates tens of thousands of
    points once and caches the result, this one locates two and does not.

    Returns ``None`` where the point is in no cell, which is what
    :func:`~fenixspoon.solvers.aerodynamics.resolve_kutta` reads as "try further out" — the
    trailing edge of a thin section is exactly where a probe can land in the hole.
    """

    def __init__(self, msh) -> None:
        from dolfinx import geometry as dgeo

        self._dgeo = dgeo
        self._msh = msh
        self._tree = dgeo.bb_tree(msh, msh.topology.dim)

    def at(self, function, px: float, py: float) -> float | None:
        point = np.array([[px, py, 0.0]])
        candidates = self._dgeo.compute_collisions_points(self._tree, point)
        cells = self._dgeo.compute_colliding_cells(self._msh, candidates, point)
        found = cells.links(0)
        if len(found) == 0:
            return None
        return float(function.eval(point, np.asarray([found[0]], dtype=np.int32)).ravel()[0])


class _GridSampler:
    """Evaluates FEM functions on a regular grid, locating the points only once.

    Point location — the bounding-box tree, the collision query, the per-point cell pick — is
    what makes sampling expensive, and it depends only on the mesh and the grid. Holding it
    means the Kutta path samples its two basis solutions for barely more than one solve's
    worth of work, and it is the same lattice for all of them, which the superposition on the
    grid requires.

    The mask is the mock solver's, unchanged, plus the points that landed in no cell at all —
    those are inside the obstacle hole or a hair outside the meshed rectangle, and either way
    they carry no solution.
    """

    def __init__(self, msh, geometry: Domain2D, resolution: int, obstacle_pts) -> None:
        from dolfinx import geometry as dgeo

        from .mock_laplace import _grid_shape, polygon_mask

        xmin, ymin, xmax, ymax = geometry.bounds
        self.bounds = (xmin, ymin, xmax, ymax)
        # The mock adapter's shape rule, imported rather than restated: a `grid2d` from either
        # adapter should be the same lattice for the same geometry and resolution, and two
        # copies of that arithmetic is how they would stop being.
        self.shape = _grid_shape(geometry.bounds, resolution)
        ny, nx = self.shape
        self.x = np.linspace(xmin, xmax, nx)
        self.y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(self.x, self.y)
        points = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])

        tree = dgeo.bb_tree(msh, msh.topology.dim)
        candidates = dgeo.compute_collisions_points(tree, points)
        colliding = dgeo.compute_colliding_cells(msh, candidates, points)
        located, cells, indices = [], [], []
        for index in range(points.shape[0]):
            found = colliding.links(index)
            if len(found) > 0:
                located.append(points[index])
                cells.append(found[0])
                indices.append(index)
        self._points = np.asarray(located) if located else np.zeros((0, 3))
        self._cells = np.asarray(cells, dtype=np.int32)
        self._indices = np.asarray(indices, dtype=np.int64)
        inside = np.zeros(points.shape[0], dtype=bool)
        inside[self._indices] = True
        self.mask = (
            polygon_mask(np.asarray(obstacle_pts, dtype=float), xx, yy)
            | ~inside.reshape(ny, nx)
        )

    def sample(self, function) -> np.ndarray:
        """One FEM function on the grid, zero where nothing was located."""
        ny, nx = self.shape
        values = np.zeros(ny * nx)
        if len(self._indices):
            values[self._indices] = function.eval(self._points, self._cells).ravel()
        return values.reshape(ny, nx)

    def speed(self, psi: np.ndarray) -> np.ndarray:
        """``|grad psi|`` on the grid, zeroed inside the body."""
        return aero.speed_on_grid(psi, self.x, self.y, self.mask)

    def payload(self, psi: np.ndarray, speed: np.ndarray | None = None) -> dict:
        """The `grid2d` result payload for a sampled field.

        ``speed`` is an argument because the caller usually has it already — the aerodynamic
        post-processing needs it too, and recomputing it here cost two extra full-grid gradients
        on the commonest path.
        """
        ny, nx = self.shape
        if speed is None:
            speed = self.speed(psi)
        return {
            "bounds": list(self.bounds),
            "shape": [ny, nx],
            "fields": {
                "psi": psi.ravel().tolist(),
                "speed": speed.ravel().tolist(),
            },
            "mask": self.mask.astype(np.uint8).ravel().tolist(),
        }


