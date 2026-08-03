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

Two routes, the same two the mock has, with the same total precedence between them.

**A load case** (#85), when one is supplied. This is where the predicate shape chosen in
:mod:`fenixspoon.boundaries` pays for itself: `f(x) -> bool` over `(3, N)` points is exactly
what `locate_entities_boundary` and `locate_dofs_geometrical` take, so a named boundary
becomes a restraint or a loaded facet set with no translation step in between. Tractions go
through **facet tags** here rather than the `conditional` the edge shorthand uses, because
several boundaries can now be loaded differently and one expression cannot carry that.

**The `fixed_edge` / `load_edge` parameters** otherwise, exactly as before: the loaded edge
is selected inside the form with a `conditional` on the spatial coordinate rather than with
facet tags — one expression, no tag bookkeeping, and it cannot fall out of step with the
locator used for the clamp.
"""

from typing import Literal

import numpy as np
import ufl
from dolfinx import default_scalar_type, fem
from dolfinx.fem.petsc import LinearProblem
from pydantic import BaseModel, Field, model_validator

from ..geometry import Domain2D, Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import (
    ELASTICITY_ASSUMPTIONS,
    ELASTICITY_CONDITIONS,
    ELASTICITY_METRICS,
    VTK_ARTIFACT,
)
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
    OVERLAPPING_TRACTIONS,
    Edge,
    constitutive,
    nodal_average,
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


def restraints(msh, space, geometry, conditions) -> list:
    """The Dirichlet conditions a load case asks for, as dolfinx `dirichletbc` objects.

    ``fixed`` clamps the whole vector and needs no sub-space; ``fixed_x`` / ``fixed_y``
    constrain one component, which in dolfinx means locating dofs on the *collapsed* sub-space
    and imposing the condition through `V.sub(i)`. That is more machinery than a full clamp,
    and it is the difference between modelling a symmetry plane and modelling a weld: holding
    both components on a symmetry plane suppresses the Poisson contraction along it and
    reports a structure stiffer than the one the caller described.
    """
    from ..boundaries import predicate

    out = []
    for name, values in conditions.items():
        where = predicate(geometry, name)
        if values.get("fixed"):
            dofs = fem.locate_dofs_geometrical(space, where)
            if len(dofs) == 0:
                raise ValueError(_selects_nothing(name))
            out.append(
                fem.dirichletbc(
                    fem.Constant(msh, np.zeros(2, dtype=default_scalar_type)), dofs, space
                )
            )
        for axis, key in ((0, "fixed_x"), (1, "fixed_y")):
            if not values.get(key):
                continue
            component, _ = space.sub(axis).collapse()
            dofs = fem.locate_dofs_geometrical((space.sub(axis), component), where)
            if len(dofs[0]) == 0:
                raise ValueError(_selects_nothing(name))
            held = fem.Function(component)
            held.x.array[:] = 0.0
            out.append(fem.dirichletbc(held, dofs, space.sub(axis)))
    return out


def traction_measure(msh, geometry, conditions) -> tuple[ufl.Measure, dict[str, int]]:
    """A `ds` measure tagging each loaded boundary, and the tag each one got.

    Facet tags rather than the shorthand's `conditional`, because a load case may load two
    boundaries with two different tractions and a single indicator expression cannot say
    that. Overlap is refused rather than resolved by tag order: a facet claimed by two
    loaded boundaries has two tractions on it, and picking one silently is the class of
    quiet wrong answer #85 exists to remove.
    """
    from dolfinx import mesh as dolfinx_mesh

    from ..boundaries import predicate

    dimension = msh.topology.dim - 1
    found: list[np.ndarray] = []
    marks: list[np.ndarray] = []
    tags: dict[str, int] = {}
    for name, values in conditions.items():
        if not (values.get("traction_x") or values.get("traction_y")):
            continue
        facets = dolfinx_mesh.locate_entities_boundary(
            msh, dimension, predicate(geometry, name)
        )
        if len(facets) == 0:
            raise ValueError(
                f"the boundary {name!r} carries a traction but no facet of this mesh lies "
                "on it; refine `mesh_size` or widen the selector"
            )
        tags[name] = len(tags) + 1
        found.append(facets)
        marks.append(np.full(len(facets), tags[name], dtype=np.int32))

    if not found:
        raise ValueError(
            "this load case restrains the body but never loads it; the answer would be "
            "zero everywhere, which reads as a structure that is fine"
        )

    indices = np.concatenate(found)
    order = np.argsort(indices)
    indices, values_array = indices[order], np.concatenate(marks)[order]
    if len(indices) > 1 and np.any(indices[1:] == indices[:-1]):
        # The same refusal the mock makes, in the same words, from the same constant: a pair
        # that says no to one input two different ways is a pair a caller has to learn twice.
        raise ValueError(OVERLAPPING_TRACTIONS)
    meshtags = dolfinx_mesh.meshtags(
        msh, dimension, indices.astype(np.int32), values_array
    )
    return ufl.Measure("ds", domain=msh, subdomain_data=meshtags), tags


def _selects_nothing(name: str) -> str:
    return (
        f"the boundary {name!r} selects no degree of freedom of this mesh, so the condition "
        "on it would apply to nothing; refine `mesh_size` or widen the selector"
    )


@register
class DolfinxElasticity2D(Solver):
    name = "dolfinx.elasticity2d"
    title = "Linear elasticity (FEniCSx, unstructured mesh)"
    description = (
        "2D linear elasticity of a plate: displacement and von Mises stress under a uniform "
        "traction with a clamp, on a Gmsh mesh. Restraints and loads go on boundaries the "
        "geometry names — part of an edge, a hole, an interior outline — or on edges of the "
        "bounding rectangle as a shorthand. Plane stress or plane strain, "
        "per-region material, quadratic elements by default so bending is resolved rather "
        "than merely represented."
    )
    geometry_types = ["domain2d", "regions2d"]
    physics = "elasticity"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh", "mpi4py", "petsc4py"]
    metrics = ELASTICITY_METRICS
    assumptions = ELASTICITY_ASSUMPTIONS
    conditions = ELASTICITY_CONDITIONS
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
        fixed_edge: Edge = Field(
            default="xmin",
            description=(
                "Edge clamped in both directions. **Ignored when a load case is supplied** "
                "(#85): conditions on named boundaries replace this and `load_edge` entirely."
            ),
        )
        load_edge: Edge = Field(
            default="xmax",
            description="Edge the traction is applied to. Ignored when a load case is supplied.",
        )
        traction: tuple[float, float] = Field(
            default=(0.0, -1.0e6),
            description=(
                "Uniform traction on `load_edge` as [tx, ty] in Pa. Ignored when a load case "
                "is supplied; use `traction_x` / `traction_y` there."
            ),
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

        a = ufl.inner(sigma(u), epsilon(v)) * ufl.dx
        if ctx.conditions:
            # Total precedence, not a merge: see the module docstring. `fixed_edge` and
            # `load_edge` keep their defaults and go unread.
            bcs = restraints(msh, V, geometry, ctx.conditions)
            measure, tags = traction_measure(msh, geometry, ctx.conditions)
            rhs = sum(
                ufl.dot(
                    fem.Constant(
                        msh,
                        np.array(
                            [
                                ctx.conditions[name].get("traction_x", 0.0),
                                ctx.conditions[name].get("traction_y", 0.0),
                            ],
                            dtype=default_scalar_type,
                        ),
                    ),
                    v,
                )
                * measure(tag)
                for name, tag in tags.items()
            )
        else:
            traction = fem.Constant(
                msh, np.asarray(params.traction, dtype=default_scalar_type)
            )
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
            bcs = [
                fem.dirichletbc(
                    fem.Constant(msh, np.zeros(2, dtype=default_scalar_type)), clamped, V
                )
            ]

        # Applies to either route. Without a restraint the stiffness matrix is singular, and
        # the LU factorisation of a singular matrix does not raise — it returns something.
        if not bcs:
            raise ValueError(
                "nothing is restrained, so the problem is singular and only a rigid-body "
                "motion satisfies it; fix at least one boundary"
            )

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="solving (LU)"))
        petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
        try:
            problem = LinearProblem(
                a, rhs, bcs=bcs,
                petsc_options=petsc_options,
                petsc_options_prefix="fenixspoon_elasticity_",
            )
        except TypeError:  # older dolfinx without the prefix keyword
            problem = LinearProblem(a, rhs, bcs=bcs, petsc_options=petsc_options)
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
        b_matrix, area = strain_displacement(points, triangles)
        d_matrix = constitutive(modulus, poisson, params.plane)
        flat = vectors.reshape(-1)
        dofs = np.repeat(triangles * 2, 2, axis=1)
        dofs[:, 1::2] += 1
        strain = np.einsum("eij,ej->ei", b_matrix, flat[dofs])
        equivalent = von_mises(np.einsum("eij,ej->ei", d_matrix, strain), poisson, params.plane)

        # Area-weighted, and here the weighting matters: a Gmsh mesh is not uniform, so a
        # plain average would let a sliver count for as much as the cell beside it.
        nodal_stress = nodal_average(triangles, equivalent, area, len(points))

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
