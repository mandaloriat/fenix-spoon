"""FEniCSx adapter: steady conduction in a solid, convectively cooled.

Same physics as ``mock.heat2d``:

    -div( k grad T ) = q          in the solid
    -k dT/dn = h (T - T_ambient)  on every exposed face

The second line is the whole modelling decision, and it is why this adapter meshes
**only the regions** rather than the bounding rectangle. The fluid is not a region with a
low conductivity — it is a boundary condition. Model it as a region instead and the fins
stop working: air conducts at 0.026 W/(m*K) against aluminium's 205, so heat cannot leave
through them and the source just accumulates. Heat sinks are analysed the same way in
practice, with the fluid reduced to a film coefficient.

That also makes this the one adapter with no Dirichlet condition anywhere. The Robin term
is what makes the problem coercive: without it, a pure-Neumann conduction problem with a
source has no solution at all (nothing balances the heat going in) and the assembled
matrix is singular.

Registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly. Tests live in
``tests/test_dolfinx_heat.py`` (``pytest -m fenics``), and the one that matters is the
cross-validation against ``mock.heat2d`` — two independent discretisations of the same
problem agreeing is the only evidence either of them is right rather than merely
self-consistent.
"""

import dolfinx
import gmsh  # noqa: F401  - availability gate, see solvers/__init__.py
import numpy as np
import ufl
from dolfinx import fem
from dolfinx.fem.petsc import LinearProblem
from mpi4py import MPI
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from ._gmsh import gmsh_session
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import HEAT_ASSUMPTIONS, HEAT_METRICS, VTK_ARTIFACT
from .dolfinx_poisson import (
    _MESH_SAFETY_FACTOR,
    _nodal_speed,
    _p1_mesh_data,
    _write_vtk_unstructured,
)
from .registry import register


def _solid_area(geometry: Regions2D) -> float:
    """Total polygon area of the regions, by the shoelace formula.

    Regions that overlap are counted twice. That is deliberate for a budget estimate:
    double-counting errs high, and the alternative — a real union — is a geometry
    operation this does not need to do just to size a mesh.
    """
    total = 0.0
    for region in geometry.regions:
        pts = region.shape.points
        n = len(pts)
        total += 0.5 * abs(
            sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))
        )
    return total


def _build_solid_mesh(geometry: Regions2D, mesh_size: float):
    """Mesh the union of the regions — and nothing else — one physical group per region.

    Unlike the magnetostatics adapter there is no background rectangle: the fluid is a
    boundary condition, so it must not be meshed. Every exterior facet of what comes out
    of here is therefore a face where solid meets fluid (or the domain edge), which is
    exactly where the convective condition applies, and lets the form use `ds` over the
    whole boundary without classifying facets.

    Regions are fragmented against each other so overlaps become separate surfaces rather
    than duplicated ones. Attribution uses the map ``occ.fragment`` returns — entry ``i``
    lists the children of input entity ``i`` — and iterating in order means later regions
    overwrite earlier ones where they overlap, which is the painter's-order precedence the
    geometry schema documents.
    """
    # Unreachable through the API — `Regions2D.regions` is `min_length=1`, so an empty
    # geometry is a 422 long before a solver sees it. Kept because this function is also
    # callable directly, and "no regions" would otherwise mean an empty mesh and a
    # singular matrix rather than a sentence.
    if not geometry.regions:
        raise ValueError("no solid regions: this solver meshes the regions, not the domain")

    with gmsh_session():
        occ = gmsh.model.occ

        surfaces = []
        for region in geometry.regions:
            pts = [occ.addPoint(x, y, 0.0, mesh_size) for x, y in region.shape.points]
            lines = [occ.addLine(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
            surfaces.append(occ.addPlaneSurface([occ.addCurveLoop(lines)]))

        if len(surfaces) == 1:
            # `fragment` needs something to fragment against; one region is already its
            # own only piece.
            occ.synchronize()
            tag_of = {surfaces[0]: 1}
        else:
            pieces, piece_map = occ.fragment(
                [(2, surfaces[0])], [(2, s) for s in surfaces[1:]]
            )
            occ.synchronize()
            tag_of = {surface: 0 for dim, surface in pieces if dim == 2}
            for index, children in enumerate(piece_map, start=1):
                for dim, surface in children:
                    if dim == 2:
                        tag_of[surface] = index

        by_tag: dict[int, list[int]] = {}
        for surface, tag in tag_of.items():
            by_tag.setdefault(tag, []).append(surface)
        # Every cell must be tagged exactly once or dolfinx's model_to_mesh refuses the
        # mesh. Tag 0 cannot occur here — every fragment descends from some region — but
        # a stray one would be a silent mis-assignment, so it is an error rather than a
        # default.
        if 0 in by_tag:
            raise ValueError("internal: a meshed fragment belongs to no region")
        for tag, group in sorted(by_tag.items()):
            gmsh.model.addPhysicalGroup(2, group, tag=tag)

        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        gmsh.model.mesh.generate(2)
        from dolfinx.io.gmsh import model_to_mesh

        data = model_to_mesh(gmsh.model, MPI.COMM_WORLD, 0, gdim=2)
        if hasattr(data, "mesh"):
            return data.mesh, data.cell_tags
        return data[0], data[1]


@register
class DolfinxHeat2D(Solver):
    name = "dolfinx.heat2d"
    title = "Heat sink (FEniCSx, region-tagged mesh)"
    description = (
        "Steady conduction through the solid regions of a `regions2d` geometry, cooled by "
        "convection on every exposed face. Solved with dolfinx (P1 elements, LU) on a Gmsh "
        "mesh covering the regions only — the surrounding fluid is a boundary condition, "
        "not a meshed material."
    )
    geometry_types = ["regions2d"]
    physics = "heat-conduction"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh"]
    metrics = HEAT_METRICS
    assumptions = HEAT_ASSUMPTIONS
    #: Single-rank Gmsh meshing followed by a direct LU factorisation — both reproducible for
    #: the same inputs and the same library versions, which the cache key includes. The
    #: caveat is MPI: a multi-rank run decomposes differently and would not reproduce, which
    #: is exactly what `features.mpi = False` already says this adapter does not support (#47).
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="coarse mesh",
            description=(
                "Meshes the fins in a couple of elements each — a setup check, not a result."
            ),
            params={"mesh_size": 0.004, "write_vtk": False},
        ),
        CapabilityExample(
            title="forced air",
            description=(
                "The same comparison mock.heat2d offers, on the FEM mesh: `h` is what a fan "
                "buys you, and it moves `t_rise` further than most geometry edits."
            ),
            params={"mesh_size": 0.0015, "h": 120.0},
        ),
    ]

    class Params(BaseModel):
        # Deliberately the same names, units and prose as mock.heat2d's parameters, so a
        # client that generates its form from `params_schema` shows the same controls for
        # either adapter and switching between them keeps the user's settings.
        mesh_size: float = Field(default=0.0015, gt=0.0, description="Target element size")
        h: float = Field(
            default=25.0,
            gt=0.0,
            le=10000.0,
            description=(
                "Convection coefficient in W/(m²·K) at solid surfaces. Around 10 for still "
                "air, 25 for gentle airflow, 100+ for forced air."
            ),
        )
        t_ambient: float = Field(
            default=20.0, description="Ambient fluid temperature in °C", ge=-273.15
        )
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "DolfinxHeat2D.Params") -> int:
        """Estimate from the solid area, not the bounding box.

        Over-estimating is the safe direction for a budget check, but only up to a point:
        this adapter meshes the regions alone, and for a heat sink those are a few percent
        of the bounding rectangle — 24% for the gallery heat sink, so estimating from the
        whole domain over-counted by 4.1x. That turns into refusing jobs that would have
        run comfortably, once the mesh is fine enough for the domain figure to cross
        `FENIXSPOON_MAX_CELLS` while the real one is far below it.

        Summing region areas still over-estimates — overlapping regions are counted twice,
        and the safety factor stays — but it is wrong by a bounded factor rather than by
        the fraction of the domain that happens to be solid.
        """
        area = _solid_area(geometry)
        return int(_MESH_SAFETY_FACTOR * 2.0 * area / max(params.mesh_size, 1e-12) ** 2)

    def solve(
        self, geometry: Regions2D, params: "DolfinxHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        ctx.progress(ProgressEvent(iteration=0, total=4, message="meshing with Gmsh"))
        msh, cell_tags = _build_solid_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=4, message="assembling"))

        # Piecewise-constant conductivity and volumetric source, one value per cell.
        Q = fem.functionspace(msh, ("DG", 0))
        conductivity, source = fem.Function(Q), fem.Function(Q)
        conductivity.x.array[:] = 1.0
        source.x.array[:] = 0.0
        for index, region in enumerate(geometry.regions, start=1):
            cells = cell_tags.find(index)
            if len(cells) == 0:
                continue
            dofs = fem.locate_dofs_topological(Q, msh.topology.dim, cells)
            conductivity.x.array[dofs] = float(region.material.get("k", 1.0))
            source.x.array[dofs] = float(region.material.get("q", 0.0))

        V = fem.functionspace(msh, ("Lagrange", 1))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        # Wrapped in the mesh's scalar type rather than a bare float: dolfinx builds
        # against real or complex PETSc, and a raw Python float is not accepted in a
        # complex build.
        film = fem.Constant(msh, dolfinx.default_scalar_type(params.h))
        ambient = fem.Constant(msh, dolfinx.default_scalar_type(params.t_ambient))

        # Robin on the entire exterior. `ds` needs no facet tags because the mesh is the
        # solid: every boundary facet is an exposed face by construction.
        a = conductivity * ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx + film * u * v * ufl.ds
        L = source * v * ufl.dx + film * ambient * v * ufl.ds

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="solving (LU)"))
        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            problem = LinearProblem(
                a, L, bcs=[],
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_heat_",
            )
        except TypeError:  # older dolfinx without the prefix keyword
            problem = LinearProblem(a, L, bcs=[], petsc_options=petsc_options)
        solved = problem.solve()
        temperature = solved[0] if isinstance(solved, tuple) else solved

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=3, total=4, message="post-processing"))
        points, triangles, t_nodal = _p1_mesh_data(V, temperature)
        k_nodal = _nodal_region_value(points, triangles, msh, cell_tags, geometry, "k", 1.0)
        # |q| = k |grad T|, with the gradient recovered the same way the other adapters do.
        flux_nodal = k_nodal * _nodal_speed(points, triangles, t_nodal)

        fields = {"T": t_nodal, "flux": flux_nodal, "k": k_nodal}
        if params.write_vtk:
            _write_vtk_unstructured(ctx.artifact("solution.vtk"), points, triangles, fields)

        xmin, ymin, xmax, ymax = geometry.bounds
        data = {
            "bounds": [xmin, ymin, xmax, ymax],
            "points": points.tolist(),
            "triangles": triangles.tolist(),
            "point_fields": {name: value.tolist() for name, value in fields.items()},
        }
        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        cells = float(msh.topology.index_map(msh.topology.dim).size_local)
        stats = {
            "cells": cells,
            # Every cell is solid here, but the key is what mock.heat2d reports and what
            # the demo reads, so both adapters answer the same question.
            "solid_cells": cells,
            "dofs": float(V.dofmap.index_map.size_local),
        }
        return SolverResult(
            kind="mesh2d",
            data=data,
            stats=stats,
            # `t_max` is a declared reduction of `T` and is filled by the runtime; only
            # `t_rise` needs a parameter, so only `t_rise` is computed here. Both used to
            # be `stats` keys, which conflated what the solve cost with what it answered.
            metrics={"t_rise": float(t_nodal.max() - params.t_ambient)},
            # A direct LU factorisation does not iterate toward a tolerance, so
            # `converged` is True by construction and there is no residual to report.
            # Saying so beats leaving both null, which reads as "nobody checked".
            converged=True,
        )


def _nodal_region_value(
    points, triangles, msh, cell_tags, geometry: Regions2D, key: str, default: float
) -> np.ndarray:
    """Per-node material value for display: the max over incident cells.

    Max rather than an average so a material interface stays a visible edge instead of
    being smeared across the nodes that straddle it.
    """
    # Filled by assignment per region rather than by looking up a tag per cell: one pass
    # over regions instead of a Python-level loop over every cell, and it drops the
    # intermediate tag array entirely. (`dolfinx_magnetostatics._nodal_material` is the
    # same function with a different key and still has the older shape; they are close
    # enough to be worth sharing if a third adapter needs one.)
    num_cells = msh.topology.index_map(msh.topology.dim).size_local
    cell_value = np.full(num_cells, default, dtype=float)
    for index, region in enumerate(geometry.regions, start=1):
        found = cell_tags.find(index)
        cell_value[found[found < num_cells]] = float(region.material.get(key, default))

    out = np.zeros(len(points))
    np.maximum.at(out, triangles, cell_value[: len(triangles)][:, None].repeat(3, axis=1))
    return out
