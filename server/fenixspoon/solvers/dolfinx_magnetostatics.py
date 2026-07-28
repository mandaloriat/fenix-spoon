"""FEniCSx adapter: 2D magnetostatics on a region-tagged unstructured mesh.

Same physics as ``mock.magnetostatics2d`` — vector potential for out-of-plane currents,

    -div( (1/mu) grad A_z ) = J_z ,      B = ( dA_z/dy , -dA_z/dx )

— but on a Gmsh triangulation where each ``regions2d`` region becomes its own physical
group, so permeability and current density are genuinely piecewise-constant over cells
rather than sampled onto a grid. This is the payoff of the ``regions2d`` schema: material
interfaces land exactly on element edges instead of being staircased.

Registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly. Tests live in
``tests/test_dolfinx_magnetostatics.py`` (``pytest -m fenics``).
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

from ..geometry import Regions2D
from ._gmsh import gmsh_session
from .base import ProgressEvent, Solver, SolverContext, SolverResult
from .dolfinx_poisson import _nodal_speed, _p1_mesh_data, _write_vtk_unstructured
from .mock_magnetostatics import MU0
from .registry import register


def _build_tagged_mesh(geometry: Regions2D, mesh_size: float):
    """Mesh the domain with one physical group per region (background = tag 0).

    Regions are fragmented against the background rectangle so nested regions produce
    disjoint surfaces. Fragments are attributed to regions using the map ``occ.fragment``
    returns — entry ``i`` lists the children of input entity ``i`` — rather than by
    probing fragment geometry. Geometric probing is a trap here: the background fragment
    (the rectangle *minus* the regions) can have its centre of mass sitting inside a
    region, which for a symmetric solenoid tags the whole background as iron.

    Iterating regions in order means later regions overwrite earlier ones where they
    nest, which is exactly the painter's-order precedence the schema documents.
    """
    xmin, ymin, xmax, ymax = geometry.bounds
    with gmsh_session():
        occ = gmsh.model.occ
        background = occ.addRectangle(xmin, ymin, 0.0, xmax - xmin, ymax - ymin)

        region_surfaces = []
        for region in geometry.regions:
            pts = [occ.addPoint(x, y, 0.0, mesh_size) for x, y in region.shape.points]
            lines = [occ.addLine(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
            region_surfaces.append(occ.addPlaneSurface([occ.addCurveLoop(lines)]))

        pieces, piece_map = occ.fragment([(2, background)], [(2, s) for s in region_surfaces])
        occ.synchronize()

        tag_of: dict[int, int] = {
            surface: 0 for dim, surface in pieces if dim == 2
        }
        # piece_map[0] belongs to the background object; the tools follow in order.
        for index in range(1, len(geometry.regions) + 1):
            for dim, surface in piece_map[index]:
                if dim == 2:
                    tag_of[surface] = index

        by_tag: dict[int, list[int]] = {}
        for surface, tag in tag_of.items():
            by_tag.setdefault(tag, []).append(surface)
        # The background gets physical group 0. It must get one: dolfinx's model_to_mesh
        # requires every cell to be tagged exactly once and raises "All cells are expected
        # to be tagged once" if the background is left out. Gmsh honours tag=0 verbatim
        # (it returns 0 and registers group (2, 0)), so there is no collision with the
        # 1..N region tags.
        for tag, surfaces in sorted(by_tag.items()):
            gmsh.model.addPhysicalGroup(2, surfaces, tag=tag)

        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        gmsh.model.mesh.generate(2)
        from dolfinx.io.gmsh import model_to_mesh

        data = model_to_mesh(gmsh.model, MPI.COMM_WORLD, 0, gdim=2)
        if hasattr(data, "mesh"):
            return data.mesh, data.cell_tags
        return data[0], data[1]


@register
class DolfinxMagnetostatics2D(Solver):
    name = "dolfinx.magnetostatics2d"
    title = "Magnetostatics (FEniCSx, region-tagged mesh)"
    description = (
        "2D magnetostatics for out-of-plane currents on a Gmsh mesh where every material "
        "region is its own physical group, solved with dolfinx (P1 elements, LU). "
        "Permeability and current density are piecewise constant per cell."
    )
    geometry_types = ["regions2d"]

    class Params(BaseModel):
        # This adapter emits mesh2d only: the FEM triangulation is the whole point of
        # solving on a region-tagged mesh, and resampling it onto a raster would throw
        # away the material interfaces it exists to resolve. Clients wanting a grid can
        # use mock.magnetostatics2d, which is grid-native.
        mesh_size: float = Field(default=0.004, gt=0.0, description="Target element size")
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    def solve(
        self, geometry: Regions2D, params: "DolfinxMagnetostatics2D.Params", ctx: SolverContext
    ) -> SolverResult:
        ctx.progress(ProgressEvent(iteration=0, total=4, message="meshing with Gmsh"))
        msh, cell_tags = _build_tagged_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=4, message="assembling"))

        # Piecewise-constant material coefficients, one value per cell via its region tag.
        Q = fem.functionspace(msh, ("DG", 0))
        nu = fem.Function(Q)  # reluctivity 1/mu
        current = fem.Function(Q)
        nu.x.array[:] = 1.0 / (MU0 * float(geometry.background.get("mu_r", 1.0)))
        current.x.array[:] = float(geometry.background.get("current_density", 0.0))
        for index, region in enumerate(geometry.regions, start=1):
            cells = cell_tags.find(index)
            if len(cells) == 0:
                continue
            dofs = fem.locate_dofs_topological(Q, msh.topology.dim, cells)
            nu.x.array[dofs] = 1.0 / (MU0 * float(region.material.get("mu_r", 1.0)))
            current.x.array[dofs] = float(region.material.get("current_density", 0.0))

        V = fem.functionspace(msh, ("Lagrange", 1))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        a = nu * ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx
        L = current * v * ufl.dx

        # A = 0 on the outer boundary: flux confined to the modelled window.
        tdim = msh.topology.dim
        msh.topology.create_connectivity(tdim - 1, tdim)
        boundary_facets = dmesh.exterior_facet_indices(msh.topology)
        bc = fem.dirichletbc(
            dolfinx.default_scalar_type(0.0),
            fem.locate_dofs_topological(V, tdim - 1, boundary_facets),
            V,
        )

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="solving (LU)"))
        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            problem = LinearProblem(
                a, L, bcs=[bc],
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_mag_",
            )
        except TypeError:  # older dolfinx without the prefix keyword
            problem = LinearProblem(a, L, bcs=[bc], petsc_options=petsc_options)
        solved = problem.solve()
        a_pot = solved[0] if isinstance(solved, tuple) else solved

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=3, total=4, message="post-processing"))
        points, triangles, a_nodal = _p1_mesh_data(V, a_pot)
        b_nodal = _nodal_speed(points, triangles, a_nodal)  # |B| = |grad A|
        mu_nodal = _nodal_material(points, triangles, msh, cell_tags, geometry)

        if params.write_vtk:
            _write_vtk_unstructured(
                ctx.artifact("solution.vtk"),
                points,
                triangles,
                {"A": a_nodal, "B": b_nodal, "mu_r": mu_nodal},
            )

        xmin, ymin, xmax, ymax = geometry.bounds
        data = {
            "bounds": [xmin, ymin, xmax, ymax],
            "points": points.tolist(),
            "triangles": triangles.tolist(),
            "point_fields": {
                "A": a_nodal.tolist(),
                "B": b_nodal.tolist(),
                "mu_r": mu_nodal.tolist(),
            },
        }
        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        return SolverResult(kind="mesh2d", data=data)


def _nodal_material(points, triangles, msh, cell_tags, geometry: Regions2D) -> np.ndarray:
    """Per-node mu_r for display: the max over incident cells, so interfaces stay visible."""
    mu_by_tag = {0: float(geometry.background.get("mu_r", 1.0))}
    for index, region in enumerate(geometry.regions, start=1):
        mu_by_tag[index] = float(region.material.get("mu_r", 1.0))

    num_cells = msh.topology.index_map(msh.topology.dim).size_local
    tags = np.zeros(num_cells, dtype=np.int32)
    for tag in mu_by_tag:
        if tag == 0:
            continue
        found = cell_tags.find(tag)
        tags[found[found < num_cells]] = tag
    cell_mu = np.array([mu_by_tag.get(int(t), 1.0) for t in tags], dtype=float)

    out = np.zeros(len(points))
    np.maximum.at(out, triangles, cell_mu[: len(triangles)][:, None].repeat(3, axis=1))
    return out
