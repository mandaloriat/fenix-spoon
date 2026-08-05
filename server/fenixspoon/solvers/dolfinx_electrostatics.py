"""FEniCSx adapter: axisymmetric electrostatics on a region-tagged meridian mesh.

Same physics as ``mock.electrostatics_axi2d`` — Laplace for the potential with
piecewise-constant permittivity, the capacitance read from the field energy — but on a Gmsh
triangulation of the ``axisymmetric2d`` section, so an electrode edge and its chamfer land
on element edges instead of being staircased onto a raster. That is the difference that
matters for this physics: the fringe field *is* an edge effect, and a grid that resolves the
edge coarsely under-reports exactly the part a parallel-plate estimate already misses.

The axisymmetric weak form is the plane one with one factor of `r`,

    integral eps ( dV/dr dv/dr + dV/dz dv/dz ) r dr dz = 0 ,

and that factor is the whole difference — which is the argument for a geometry kind rather
than a solver parameter: nothing in the *payload* of a plane section says whether the
horizontal coordinate is a radius, so nothing could stop this integrand being applied to a
slice, or the plane one to a body of revolution. `geometry_types` and the `422` behind it
are what say which is which.

**The axis needs no boundary condition and gets none.** In this form the boundary term
carries the same `r`, so on r = 0 it vanishes identically: the natural symmetry condition is
what the assembly produces by doing nothing there. All the adapter must avoid is putting a
Dirichlet condition on the axis, and it only ever puts one where the caller asked.

Registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly. Tests live in
``tests/test_dolfinx_electrostatics.py`` (``pytest -m fenics``).
"""

import math

import dolfinx
import gmsh  # noqa: F401  - availability gate, see solvers/__init__.py
import numpy as np
import ufl
from dolfinx import fem
from dolfinx import mesh as dmesh
from dolfinx.fem.petsc import LinearProblem
from pydantic import BaseModel, Field

from ..boundaries import predicate
from ..geometry import Axisymmetric2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import (
    ELECTROSTATICS_ASSUMPTIONS,
    ELECTROSTATICS_CONDITIONS,
    ELECTROSTATICS_METRICS,
    VTK_ARTIFACT,
)
from .dolfinx_magnetostatics import _build_tagged_mesh, _nodal_material
from .dolfinx_poisson import (
    _nodal_speed,
    _p1_mesh_data,
    _write_vtk_unstructured,
    estimate_triangles,
)
from .mock_electrostatics import EPS0
from .registry import register


def _electrode_dofs(
    geometry: Axisymmetric2D, conditions, V, msh, cell_tags
) -> list[tuple[np.ndarray, float]]:
    """Every dof held at a potential, and the potential it is held at.

    Two sources, and they are collected in the order the mock applies them so the pair
    cannot disagree about precedence: regions in painter's order first — a whole metal body
    is an equipotential, so *every* dof of its cells is constrained, not merely its outline —
    and named boundaries afterwards, since a condition names a face of the modelled window
    and is the later statement.
    """
    held: list[tuple[np.ndarray, float]] = []
    tdim = msh.topology.dim
    for index, region in enumerate(geometry.regions, start=1):
        if "voltage" not in region.material:
            continue
        cells = cell_tags.find(index)
        if len(cells) == 0:
            continue
        held.append(
            (
                fem.locate_dofs_topological(V, tdim, cells),
                float(region.material["voltage"]),
            )
        )

    msh.topology.create_connectivity(tdim - 1, tdim)
    for name, applied in conditions.items():
        if "voltage" not in applied:
            continue
        facets = dmesh.locate_entities_boundary(msh, tdim - 1, predicate(geometry, name))
        held.append(
            (
                fem.locate_dofs_topological(V, tdim - 1, facets),
                float(applied["voltage"]),
            )
        )
    return held


@register
class DolfinxElectrostaticsAxi2D(Solver):
    name = "dolfinx.electrostatics_axi2d"
    title = "Axisymmetric electrostatics (FEniCSx, region-tagged mesh)"
    description = (
        "Electrostatics on a meridian (r, z) half-section of a body of revolution, solved "
        "with dolfinx (P1 elements, LU) on a Gmsh mesh where every material region is its "
        "own physical group. The cylindrical `r` weight is in the integrand, so the reported "
        "energy and capacitance are those of the whole revolved body."
    )
    geometry_types = ["axisymmetric2d"]
    physics = "electrostatics"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh"]
    metrics = ELECTROSTATICS_METRICS
    assumptions = ELECTROSTATICS_ASSUMPTIONS
    conditions = ELECTROSTATICS_CONDITIONS
    #: Single-rank Gmsh meshing followed by a direct LU factorisation — both reproducible for
    #: the same inputs and the same library versions, which the cache key includes.
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="coarse mesh",
            description="Fast, and enough to check the section meshes and the field looks right.",
            params={"mesh_size": 2.0e-4, "write_vtk": False},
        ),
        CapabilityExample(
            title="resolved gap",
            description=(
                "Elements small against the electrode gap, which is what a capacitance needs: "
                "the fringe is an edge effect, so it is the elements *at the edge* that decide "
                "how much of it the answer contains."
            ),
            params={"mesh_size": 2.0e-5},
        ),
    ]

    class Params(BaseModel):
        # mesh2d only, for the reason the magnetostatics adapter gives: the triangulation is
        # the point of solving on a meshed section, and resampling onto a raster would throw
        # away the electrode edges this adapter exists to resolve.
        mesh_size: float = Field(default=5.0e-5, gt=0.0, description="Target element size")
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(
        cls, geometry: Axisymmetric2D, params: "DolfinxElectrostaticsAxi2D.Params"
    ) -> int:
        return estimate_triangles(geometry.bounds, params.mesh_size)

    def solve(
        self,
        geometry: Axisymmetric2D,
        params: "DolfinxElectrostaticsAxi2D.Params",
        ctx: SolverContext,
    ) -> SolverResult:
        ctx.progress(ProgressEvent(iteration=0, total=4, message="meshing with Gmsh"))
        msh, cell_tags = _build_tagged_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=4, message="assembling"))

        # Piecewise-*relative* permittivity, one value per cell via its region tag. Relative
        # rather than absolute for the reason the form below is dimensionless — see there.
        Q = fem.functionspace(msh, ("DG", 0))
        eps_r = fem.Function(Q)
        eps_r.x.array[:] = float(geometry.background.get("eps_r", 1.0))
        for index, region in enumerate(geometry.regions, start=1):
            cells = cell_tags.find(index)
            if len(cells) == 0:
                continue
            dofs = fem.locate_dofs_topological(Q, msh.topology.dim, cells)
            eps_r.x.array[dofs] = float(region.material.get("eps_r", 1.0))

        V = fem.functionspace(msh, ("Lagrange", 1))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        # `r` from the coordinate itself rather than from a per-cell array: it varies within
        # an element, and quadrature of `r` times a P1 gradient product is exact where a
        # cell-averaged radius would not be.
        r = ufl.SpatialCoordinate(msh)[0]

        # **The assembled operator is dimensionless, and that is not tidiness.** The physical
        # form carries `eps_0 * r`, which for a section a millimetre across puts every matrix
        # entry near 1e-14 — below PETSc's *absolute* zero-pivot tolerance (1e-12). The
        # factorization then fails a pivot check, and with `ksp_error_if_not_converged` off it
        # does not raise: it returns a vector of infinities, from which this adapter computed a
        # nan capacitance and reported it as an answer. Multiplying a homogeneous problem by a
        # constant cannot change its solution, so both constants come out of the operator and
        # go back into the energy below, where they belong and where nothing is factorized.
        #
        # `radius_scale` is that constant for the geometric half. Taking it from the bounds
        # rather than hard-coding a metre keeps the conditioning the same for a MEMS gap and a
        # metre-wide vessel, which is the property a fixed scale would quietly lose.
        radius_scale = float(geometry.bounds[2])
        a = eps_r * ufl.inner(ufl.grad(u), ufl.grad(v)) * (r / radius_scale) * ufl.dx
        rhs = fem.Constant(msh, dolfinx.default_scalar_type(0.0)) * v * ufl.dx

        held = _electrode_dofs(geometry, ctx.conditions, V, msh, cell_tags)
        if not held:
            raise ValueError(
                "no electrode: give a region a `voltage` material key, or name a boundary and "
                "put a `voltage` condition on it. A potential has to be pinned somewhere — "
                "with nothing held, every constant field solves this problem equally well"
            )
        bcs = [
            fem.dirichletbc(dolfinx.default_scalar_type(value), dofs, V) for dofs, value in held
        ]
        levels = [value for _, value in held]
        span = float(max(levels) - min(levels))

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="solving (LU)"))
        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            problem = LinearProblem(
                a, rhs, bcs=bcs,
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_es_",
            )
        except TypeError:  # older dolfinx without the prefix keyword
            problem = LinearProblem(a, rhs, bcs=bcs, petsc_options=petsc_options)
        solved = problem.solve()
        potential = solved[0] if isinstance(solved, tuple) else solved

        # A failed factorization is not an exception here — PETSc returns infinities and the
        # run carries on to report a nan capacitance, which is the one outcome worse than a
        # crash. Checked rather than trusted, because it is cheap and because this adapter has
        # already produced it once.
        if not np.all(np.isfinite(potential.x.array)):
            raise ValueError(
                "the linear solve returned a non-finite potential, so the factorization "
                "failed rather than converged; this is a solver fault, not a bad geometry"
            )

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=3, total=4, message="post-processing"))
        # The energy of the *revolved* body, in real units: `eps_0` and the true `r` are back,
        # since this is a sum rather than a factorization and nothing here cares about the
        # magnitude. The 2*pi is what turns a meridian integral into a volume one, and it is
        # why the capacitance below is in farads rather than farads per metre.
        energy = float(
            fem.assemble_scalar(
                fem.form(
                    0.5
                    * (EPS0 * eps_r)
                    * ufl.inner(ufl.grad(potential), ufl.grad(potential))
                    * (2.0 * math.pi)
                    * r
                    * ufl.dx
                )
            )
        )

        points, triangles, v_nodal = _p1_mesh_data(V, potential)
        e_nodal = _nodal_speed(points, triangles, v_nodal)  # |E| = |grad V|
        eps_nodal = _nodal_material(
            points, triangles, msh, cell_tags, geometry, key="eps_r", default=1.0
        )

        if params.write_vtk:
            _write_vtk_unstructured(
                ctx.artifact("solution.vtk"),
                points,
                triangles,
                {"V": v_nodal, "E": e_nodal, "eps_r": eps_nodal},
            )

        metrics = {"energy": energy}
        warnings: list[str] = []
        if span > 0.0:
            metrics["capacitance"] = 2.0 * energy / span**2
        else:
            warnings.append(
                "only one potential is held in this model, so there is no capacitance to "
                "report: give a second region or boundary a different `voltage`"
            )

        rmin, zmin, rmax, zmax = geometry.bounds
        data = {
            "bounds": [rmin, zmin, rmax, zmax],
            "points": points.tolist(),
            "triangles": triangles.tolist(),
            "point_fields": {
                "V": v_nodal.tolist(),
                "E": e_nodal.tolist(),
                "eps_r": eps_nodal.tolist(),
            },
        }
        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        stats = {
            "cells": float(msh.topology.index_map(msh.topology.dim).size_local),
            "dofs": float(V.dofmap.index_map.size_local),
        }
        # `e_max` is a declared reduction; the runtime fills it from the payload.
        return SolverResult(
            kind="mesh2d",
            data=data,
            stats=stats,
            metrics=metrics,
            warnings=warnings,
            # A direct LU factorisation does not iterate toward a tolerance, so `converged`
            # is True by construction and there is no residual to report.
            converged=True,
        )
