"""FEniCSx adapter: 2D linear elasticity of a loaded plate (issue #81).

The FEniCSx twin of ``mock.elasticity2d``: same physics, same parameter names, same declared
metrics, same boundary-condition convention — and a proper unstructured mesh with a
variational form instead of constant-strain triangles.

This module registers only when ``dolfinx``, ``gmsh`` and ``mpi4py`` import cleanly, so the
same codebase runs in a plain virtualenv (where this capability is simply absent) and in the
dolfinx image.

## The form

Displacement `u` in a vector P1 or P2 space, with

    sigma(u) = 2 mu eps(u) + lambda tr(eps(u)) I ,   eps(u) = sym(grad u)

and `a = inner(sigma(u), eps(v)) dx`. Plane stress and plane strain differ only in
`lambda` — the plane-stress value is `2 mu lambda / (lambda + 2 mu)`, which is the standard
substitution and the only place the two cases diverge.

Degree 2 is available and is the default, because this adapter exists to be the one whose
numbers can be quoted: P1 triangles are stiff in bending exactly as the mock's CST elements
are, and a quadratic space removes that error rather than tolerating it.

## Post-processing happens in NumPy, deliberately

Stresses come from the P1 nodal displacement through the very same helpers the mock uses —
`strain_displacement`, `constitutive`, `von_mises`. That is not laziness: it means the two
adapters compute their reported metric by one route, so a disagreement between them is a
disagreement about the *solve*, which is the thing the cross-validation is meant to detect.
`dolfinx.potential_flow2d` takes the same approach for its nodal speed.

## Boundary conditions

As in the mock: `fixed_edge` and `load_edge` name edges of the bounding rectangle, and the
traction is uniform along the loaded one. The loaded edge is selected inside the form with a
`conditional` on the spatial coordinate rather than with facet tags — one expression, no
tag bookkeeping, and it cannot fall out of step with the locator used for the clamp.
"""

from typing import Literal

import numpy as np
import ufl
from dolfinx import default_scalar_type, fem
from dolfinx.fem.petsc import LinearProblem
from pydantic import BaseModel, Field, model_validator

from ..geometry import Domain2D, Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import ELASTICITY_ASSUMPTIONS, ELASTICITY_METRICS, VTK_ARTIFACT
from .dolfinx_magnetostatics import _build_tagged_mesh
from .dolfinx_poisson import (
    _build_mesh as _build_holed_mesh,
)
from .dolfinx_poisson import (
    _p1_mesh_data,
    _write_vtk_unstructured,
    estimate_triangles,
)
from .mock_elasticity import (
    Edge,
    constitutive,
    sample_material,
    strain_displacement,
    von_mises,
)
from .registry import register


def lame(youngs_modulus: float, poisson_ratio: float, plane: str) -> tuple[float, float]:
    """Lamé parameters, with the plane-stress substitution applied where it belongs."""
    mu = youngs_modulus / (2.0 * (1.0 + poisson_ratio))
    lmbda = (
        youngs_modulus
        * poisson_ratio
        / ((1.0 + poisson_ratio) * (1.0 - 2.0 * poisson_ratio))
    )
    if plane == "stress":
        lmbda = 2.0 * mu * lmbda / (lmbda + 2.0 * mu)
    return mu, lmbda


def edge_indicator(msh, bounds, edge: str, eps: float):
    """A UFL indicator that is 1 on one edge of the bounding rectangle and 0 elsewhere."""
    xmin, ymin, xmax, ymax = bounds
    x = ufl.SpatialCoordinate(msh)
    test = {
        "xmin": ufl.le(x[0], xmin + eps),
        "xmax": ufl.ge(x[0], xmax - eps),
        "ymin": ufl.le(x[1], ymin + eps),
        "ymax": ufl.ge(x[1], ymax - eps),
    }[edge]
    return ufl.conditional(test, 1.0, 0.0)


def edge_locator(bounds, edge: str, eps: float):
    """A NumPy predicate for the same edge, for `locate_dofs_geometrical`."""
    xmin, ymin, xmax, ymax = bounds
    axis, target = {
        "xmin": (0, xmin), "xmax": (0, xmax), "ymin": (1, ymin), "ymax": (1, ymax)
    }[edge]

    def locator(x):
        return np.abs(x[axis] - target) < eps

    return locator


@register
class DolfinxElasticity2D(Solver):
    name = "dolfinx.elasticity2d"
    title = "Linear elasticity (FEniCSx, unstructured mesh)"
    description = (
        "2D linear elasticity of a plate: displacement and von Mises stress under a uniform "
        "edge traction with one edge clamped, on a Gmsh mesh. Plane stress or plane strain, "
        "per-region material, quadratic elements by default so bending is resolved rather "
        "than merely represented."
    )
    geometry_types = ["domain2d", "regions2d"]
    physics = "elasticity"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh", "mpi4py", "petsc4py"]
    metrics = ELASTICITY_METRICS
    assumptions = ELASTICITY_ASSUMPTIONS
    #: A direct LU solve of a linear system on a mesh Gmsh builds deterministically from the
    #: same geometry: same inputs, same answer. Safe to cache (#47).
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="cantilever under end shear",
            description="Clamped at xmin, sheared down at xmax. Compare with PL^3/3EI.",
            params={"mesh_size": 0.02, "fixed_edge": "xmin", "load_edge": "xmax",
                    "traction": [0.0, -1.0e6]},
        ),
        CapabilityExample(
            title="stress concentration",
            description=(
                "A plate with a hole pulled along x. The classical check: the peak equivalent "
                "stress is three times the far field for a circular hole in a wide plate."
            ),
            params={"mesh_size": 0.03, "fixed_edge": "xmin", "load_edge": "xmax",
                    "traction": [1.0e6, 0.0]},
        ),
    ]

    class Params(BaseModel):
        mesh_size: float = Field(
            default=0.05, gt=0.0, le=1.0, description="Target Gmsh element size, in metres."
        )
        degree: int = Field(
            default=2, ge=1, le=2,
            description=(
                "Displacement polynomial degree. 2 unless you are trading accuracy for time."
            ),
        )
        youngs_modulus: float = Field(
            default=2.1e11, gt=0.0, description="E in Pa; steel by default."
        )
        poisson_ratio: float = Field(default=0.3, ge=0.0, lt=0.5, description="nu.")
        plane: Literal["stress", "strain"] = Field(
            default="stress",
            description="`stress` for a thin plate loaded in its plane, `strain` for a long body.",
        )
        fixed_edge: Edge = Field(default="xmin", description="Edge clamped in both directions.")
        load_edge: Edge = Field(default="xmax", description="Edge the traction is applied to.")
        traction: tuple[float, float] = Field(
            default=(0.0, -1.0e6), description="Uniform traction on `load_edge` as [tx, ty] in Pa."
        )
        write_vtk: bool = Field(default=True, description="Attach the solution as a VTK artifact")

        @model_validator(mode="after")
        def _check_edges(self) -> "DolfinxElasticity2D.Params":
            if self.fixed_edge == self.load_edge:
                raise ValueError("fixed_edge and load_edge must be different edges")
            return self

    @classmethod
    def estimate_cells(cls, geometry, params: "DolfinxElasticity2D.Params") -> int:
        return estimate_triangles(geometry.bounds, params.mesh_size)

    def solve(
        self,
        geometry: Domain2D | Regions2D,
        params: "DolfinxElasticity2D.Params",
        ctx: SolverContext,
    ) -> SolverResult:
        ctx.progress(ProgressEvent(iteration=0, total=4, message="meshing with Gmsh"))
        if isinstance(geometry, Domain2D):
            msh, cell_tags = _build_holed_mesh(geometry, params.mesh_size), None
        else:
            msh, cell_tags = _build_tagged_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=4, message="assembling"))

        # Piecewise-constant Lamé parameters, one value per cell via its region tag. A
        # `domain2d` has no regions, so both are uniform and come from the parameters.
        constants = fem.functionspace(msh, ("DG", 0))
        mu_field, lmbda_field = fem.Function(constants), fem.Function(constants)
        mu, lmbda = lame(params.youngs_modulus, params.poisson_ratio, params.plane)
        if isinstance(geometry, Regions2D):
            mu, lmbda = lame(
                float(geometry.background.get("youngs_modulus", params.youngs_modulus)),
                float(geometry.background.get("poisson_ratio", params.poisson_ratio)),
                params.plane,
            )
        mu_field.x.array[:] = mu
        lmbda_field.x.array[:] = lmbda
        if cell_tags is not None:
            for index, region in enumerate(geometry.regions, start=1):
                cells = cell_tags.find(index)
                if len(cells) == 0:
                    continue
                dofs = fem.locate_dofs_topological(constants, msh.topology.dim, cells)
                region_mu, region_lmbda = lame(
                    float(region.material.get("youngs_modulus", params.youngs_modulus)),
                    float(region.material.get("poisson_ratio", params.poisson_ratio)),
                    params.plane,
                )
                mu_field.x.array[dofs] = region_mu
                lmbda_field.x.array[dofs] = region_lmbda

        V = fem.functionspace(msh, ("Lagrange", params.degree, (2,)))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)

        def epsilon(w):
            return ufl.sym(ufl.grad(w))

        def sigma(w):
            return 2.0 * mu_field * epsilon(w) + lmbda_field * ufl.tr(epsilon(w)) * ufl.Identity(2)

        xmin, ymin, xmax, ymax = geometry.bounds
        eps = 1e-9 * min(xmax - xmin, ymax - ymin)
        traction = fem.Constant(msh, np.asarray(params.traction, dtype=default_scalar_type))

        a = ufl.inner(sigma(u), epsilon(v)) * ufl.dx
        rhs = (
            ufl.dot(traction, v)
            * edge_indicator(msh, geometry.bounds, params.load_edge, eps)
            * ufl.ds
        )

        clamped = fem.locate_dofs_geometrical(
            V, edge_locator(geometry.bounds, params.fixed_edge, eps)
        )
        if len(clamped) == 0:
            raise ValueError(
                f"no mesh node lies on the {params.fixed_edge!r} edge, so the body is not "
                "restrained; with `regions2d` the material must reach the bounding rectangle"
            )
        bc = fem.dirichletbc(
            fem.Constant(msh, np.zeros(2, dtype=default_scalar_type)), clamped, V
        )

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="solving (LU)"))
        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            problem = LinearProblem(
                a, rhs, bcs=[bc],
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_elasticity_",
            )
        except TypeError:  # older dolfinx without the prefix keyword
            problem = LinearProblem(a, rhs, bcs=[bc], petsc_options=petsc_options)
        solved = problem.solve()
        displacement = solved[0] if isinstance(solved, tuple) else solved

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=3, total=4, message="post-processing"))

        # The P1 view of the solution: the nodes a `mesh2d` payload carries. On a degree-2
        # space this discards the mid-edge values, which is a rendering decision — the
        # protocol has no quadratic result kind, and the vertex values are the ones a viewer
        # draws.
        linear = fem.functionspace(msh, ("Lagrange", 1))
        points, triangles, _ = _p1_mesh_data(linear, fem.Function(linear))
        nodal = fem.Function(fem.functionspace(msh, ("Lagrange", 1, (2,))))
        nodal.interpolate(displacement)
        vectors = np.asarray(nodal.x.array.real, dtype=float).reshape(-1, 2)[: len(points)]

        centroids = points[triangles].mean(axis=1)
        modulus, poisson = sample_material(
            geometry, centroids, params.youngs_modulus, params.poisson_ratio
        )
        b_matrix, _ = strain_displacement(points, triangles)
        d_matrix = constitutive(modulus, poisson, params.plane)
        flat = vectors.reshape(-1)
        dofs = np.repeat(triangles * 2, 2, axis=1)
        dofs[:, 1::2] += 1
        strain = np.einsum("eij,ej->ei", b_matrix, flat[dofs])
        equivalent = von_mises(np.einsum("eij,ej->ei", d_matrix, strain), poisson, params.plane)

        counts = np.bincount(triangles.ravel(), minlength=len(points))
        nodal_stress = np.bincount(
            triangles.ravel(), weights=np.repeat(equivalent, 3), minlength=len(points)
        ) / np.maximum(counts, 1)

        magnitude = np.hypot(vectors[:, 0], vectors[:, 1])
        fields = {"u_mag": magnitude, "sigma_vm": nodal_stress}
        if params.write_vtk:
            _write_vtk_unstructured(ctx.artifact("solution.vtk"), points, triangles, fields)

        compliance = fem.assemble_scalar(fem.form(ufl.action(rhs, displacement)))
        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        return SolverResult(
            kind="mesh2d",
            stats={
                "cells": float(len(triangles)),
                "dofs": float(V.dofmap.index_map.size_global * V.dofmap.index_map_bs),
            },
            metrics={"compliance": float(compliance)},
            # A direct LU solve either succeeded or raised; there is no iteration to report,
            # and claiming a residual of zero would be a number nobody measured.
            converged=True,
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "points": points.tolist(),
                "triangles": triangles.tolist(),
                "point_fields": {name: value.tolist() for name, value in fields.items()},
                "point_vector_fields": {"u": vectors.tolist()},
            },
        )
