"""Mock solver: 2D linear elasticity of a loaded plate (issue #81).

Solves the equilibrium of a linear-elastic body,

    div(sigma) = 0 ,   sigma = D : eps(u) ,   eps = sym(grad u) ,

restrained and loaded on boundaries the geometry names, or — as a shorthand — on edges of the
bounding rectangle. The unknown is a **displacement vector** `u = (u_x, u_y)` — the first
vector unknown in this repository, which is most of why #81 chose this physics.

## Discretization

Constant-strain triangles (CST) on the same structured triangulation
:func:`grid_to_mesh2d` already builds for every other mock: each grid cell whose four
corners survive the hole mask contributes two triangles. The element matrices are assembled
once and the system is solved **matrix-free** by preconditioned conjugate gradients, so
nothing here needs SciPy — the same constraint every mock in this package is written under.

CST is exact for a constant strain field and **stiff in bending**: a cantilever comes out
too rigid until the mesh is fine, by 6% at `resolution=64` and under 2% by 128. That is a
property of the element, not a bug, and the tests are written to match it rather than around
it — uniform tension is checked against the closed form, bending against `PL^3/3EI` with a
tolerance that tightens as the mesh refines.

One honest detail the tests had to be written around: clamping an edge in *both* directions
prevents the Poisson contraction there, so a tension case is not uniform near the clamp. The
far field is exact to seven figures; the peak stress sits 7% above it at the clamped corners.
That is Saint-Venant, not discretization error, and it is why `sigma_vm_max` is checked away
from the constraint.

## Where the boundary conditions come from

Two routes, and the second replaces the first rather than adding to it.

**A load case** (#85), when one is supplied: `ctx.conditions` maps a boundary the geometry
*names* to the scalars in force there, and this adapter turns each name into a predicate
with :func:`fenixspoon.boundaries.predicate`. That reaches part of an edge, the hole
boundary, or any outline the geometry declares — which is what #81 could not express and
what the `edge_aligned_boundary_conditions` assumption used to exclude.

**The `fixed_edge` / `load_edge` parameters** otherwise. Kept rather than deleted, because
they are the shorthand for the common case and a caller with a bare rectangle should not
have to author a geometry with named boundaries to clamp one end of it.

The precedence is total on purpose: with a load case the two parameters are **not consulted
at all**. Merging them would mean a caller who named every boundary explicitly still got an
invisible clamp on `xmin` from a default it never set.

Material keys read from a `regions2d` geometry (everything else ignored):

- ``youngs_modulus`` — E in Pa (default: the `youngs_modulus` parameter)
- ``poisson_ratio`` — nu (default: the `poisson_ratio` parameter)

A ``domain2d`` geometry carries no materials at all, so there both come from the parameters.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, model_validator

from ..geometry import Domain2D, Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import (
    ELASTICITY_ASSUMPTIONS,
    ELASTICITY_CONDITIONS,
    ELASTICITY_METRICS,
    VTK_ARTIFACT,
)
from .mock_laplace import _grid_shape, grid_to_mesh2d, polygon_mask, write_vtk_structured_points
from .registry import register

#: Which edge of the bounding rectangle a boundary condition applies to.
Edge = Literal["xmin", "xmax", "ymin", "ymax"]

#: Refused by both adapters, in the same words, when two loaded boundaries of one load case
#: claim the same piece of the boundary.
#:
#: Shared as a constant rather than written twice, for the reason the metric declarations are
#: shared: a pair that refuses the same input with two different sentences is a pair a caller
#: has to learn twice. The FEniCSx half imports this.
#:
#: Refused rather than summed. Superposition is a real thing to want and this is not it — a
#: caller who names two boundaries that happen to overlap gets a load on the shared stretch
#: that is the sum of two it never asked to add, and nothing anywhere says so. The intent
#: (`traction_x` on this stretch) has one value, so two is a contradiction rather than a
#: total. Found by review of #85, where the mock summed and the FEniCSx twin already refused.
OVERLAPPING_TRACTIONS = (
    "two loaded boundaries of this load case claim the same piece of the boundary; a "
    "surface cannot carry two tractions, so name them so they do not overlap"
)


def constitutive(e: np.ndarray, nu: np.ndarray, plane: str) -> np.ndarray:
    """The 3x3 stress-strain matrix per element, in Voigt order (xx, yy, xy).

    Both cases, because the difference is not cosmetic: a thin plate loaded in its plane is
    plane *stress* and a long prismatic body is plane *strain*, and using the wrong one is a
    silent error of order `nu` in every stress it reports.
    """
    d = np.zeros((*e.shape, 3, 3))
    if plane == "stress":
        factor = e / (1.0 - nu**2)
        d[..., 0, 0] = d[..., 1, 1] = factor
        d[..., 0, 1] = d[..., 1, 0] = factor * nu
        d[..., 2, 2] = factor * (1.0 - nu) / 2.0
    else:
        factor = e / ((1.0 + nu) * (1.0 - 2.0 * nu))
        d[..., 0, 0] = d[..., 1, 1] = factor * (1.0 - nu)
        d[..., 0, 1] = d[..., 1, 0] = factor * nu
        d[..., 2, 2] = factor * (1.0 - 2.0 * nu) / 2.0
    return d


def strain_displacement(points: np.ndarray, triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The CST `B` matrix (ne, 3, 6) and element area (ne,), vectorised over all triangles."""
    p = points[triangles]  # (ne, 3, 2)
    x1, y1 = p[:, 0, 0], p[:, 0, 1]
    x2, y2 = p[:, 1, 0], p[:, 1, 1]
    x3, y3 = p[:, 2, 0], p[:, 2, 1]

    twice_area = x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)
    b = np.stack([y2 - y3, y3 - y1, y1 - y2], axis=1) / twice_area[:, None]
    c = np.stack([x3 - x2, x1 - x3, x2 - x1], axis=1) / twice_area[:, None]

    mat = np.zeros((len(triangles), 3, 6))
    mat[:, 0, 0::2] = b
    mat[:, 1, 1::2] = c
    mat[:, 2, 0::2] = c
    mat[:, 2, 1::2] = b
    return mat, np.abs(twice_area) / 2.0


def von_mises(sigma: np.ndarray, nu: np.ndarray, plane: str) -> np.ndarray:
    """Equivalent stress from the in-plane components.

    Plane strain carries a non-zero `sigma_zz = nu (sigma_xx + sigma_yy)`; leaving it out
    would under-report the equivalent stress by a factor that grows with `nu`, which is
    exactly the kind of quiet error the `plane` parameter exists to make explicit.
    """
    sxx, syy, sxy = sigma[:, 0], sigma[:, 1], sigma[:, 2]
    szz = np.zeros_like(sxx) if plane == "stress" else nu * (sxx + syy)
    return np.sqrt(
        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2) + 3.0 * sxy**2
    )


def sample_material(
    geometry, centroids: np.ndarray, youngs_modulus: float, poisson_ratio: float
) -> tuple[np.ndarray, np.ndarray]:
    """Per-element E and nu: from the region a centroid falls in, else from the parameters.

    Shared with the FEniCSx adapter, which post-processes stresses through the same helpers
    so a disagreement between the pair is a disagreement about the solve rather than about
    which material each of them thought it was using.
    """
    modulus = np.full(len(centroids), youngs_modulus)
    poisson = np.full(len(centroids), poisson_ratio)
    if isinstance(geometry, Regions2D):
        cx, cy = centroids[:, 0], centroids[:, 1]
        for key, target, fallback in (
            ("youngs_modulus", modulus, youngs_modulus),
            ("poisson_ratio", poisson, poisson_ratio),
        ):
            target[:] = float(geometry.background.get(key, fallback))
            for region in geometry.regions:  # painter's order, later regions win
                inside = polygon_mask(np.asarray(region.shape.points, dtype=float), cx, cy)
                target[inside] = float(region.material.get(key, fallback))
    return modulus, poisson


def nodal_average(
    triangles: np.ndarray, cell_values: np.ndarray, area: np.ndarray, n_points: int
) -> np.ndarray:
    """Area-weighted average of a per-element quantity at each node.

    Weighted rather than plain, to match `_nodal_speed` in the potential-flow adapter and
    because it is the local L2 representative of a piecewise-constant field: a node's value
    should not swing on how many slivers happen to touch it.

    On the mock's structured grid every triangle has the same area, so the weights are
    uniform and this is arithmetic averaging — the weighting earns its keep on the Gmsh mesh
    the FEniCSx adapter shares this helper with.

    It smooths, and a peak is what it smooths most: `sigma_vm_max` is under-reported on a
    coarse mesh, which is stated in the metric's own description rather than left for a
    caller to discover.
    """
    weighted = np.bincount(
        triangles.ravel(), weights=np.repeat(cell_values * area, 3), minlength=n_points
    )
    total = np.bincount(triangles.ravel(), weights=np.repeat(area, 3), minlength=n_points)
    return weighted / np.maximum(total, 1e-300)


def edge_nodes(points: np.ndarray, bounds, edge: str, tol: float) -> np.ndarray:
    """Indices of the nodes lying on one edge of the bounding rectangle, ordered along it."""
    xmin, ymin, xmax, ymax = bounds
    coordinate, target, along = {
        "xmin": (0, xmin, 1),
        "xmax": (0, xmax, 1),
        "ymin": (1, ymin, 0),
        "ymax": (1, ymax, 0),
    }[edge]
    on_edge = np.nonzero(np.abs(points[:, coordinate] - target) < tol)[0]
    return on_edge[np.argsort(points[on_edge, along])]


def boundary_segments(triangles: np.ndarray) -> np.ndarray:
    """The edges of the triangulation that belong to exactly one triangle, as `(ns, 2)`.

    That is the whole boundary — the outer rectangle *and* the rim of any hole — found by
    counting rather than by geometry, so it needs no tolerance and does not care what shape
    the obstacle is. It is what a traction is integrated over once the loaded boundary can
    be an arbitrary named piece rather than one side of the bounding box.
    """
    edges = np.concatenate(
        [triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]], axis=0
    )
    unique, counts = np.unique(np.sort(edges, axis=1), axis=0, return_counts=True)
    return unique[counts == 1]


def apply_conditions(
    geometry, conditions: dict[str, dict[str, float]], points, triangles, n_dof: int
) -> tuple[np.ndarray, np.ndarray]:
    """A load case as the two arrays the solve needs: the free mask, and the load vector.

    One boundary at a time, each resolved to a node mask through the geometry's own
    selector. Restraints zero the mask; tractions are integrated over the boundary segments
    whose **both** endpoints are selected, each carrying `traction * length` split evenly
    between its ends — the exact consistent load for a linear element, and the reason a
    refined mesh converges to the same total force rather than a different one.

    Both endpoints rather than either, deliberately: a segment with one end inside the
    selection and one outside straddles the edge of the named boundary, and counting it
    would apply the load slightly beyond where the caller put it.

    A condition that selects nothing raises rather than contributing nothing. It is the
    failure #85 exists to prevent, one level down from the boundary-name check: the name
    resolved, the predicate was fine, and the mesh is simply too coarse to have a node
    there — which produces a solve that runs and answers a different problem.

    Two *loaded* boundaries claiming one segment raise as well; see
    :data:`OVERLAPPING_TRACTIONS`. Restraints need no such check because they do not
    accumulate: clamping a node twice clamps it once.
    """
    from ..boundaries import predicate

    free = np.ones(n_dof)
    rhs = np.zeros(n_dof)
    segments = boundary_segments(triangles)
    coords = np.ascontiguousarray(points.T)
    # Which boundary has already loaded each segment, so an overlap is caught rather than
    # summed. Selectors are not required to be disjoint — `near` and `part` will happily
    # both find a corner — so this is a case a caller reaches by accident.
    claimed = np.full(len(segments), -1, dtype=np.int64)
    loaded_by: list[str] = []

    for name, values in conditions.items():
        selected = np.asarray(predicate(geometry, name)(coords), dtype=bool)
        nodes = np.nonzero(selected)[0]
        if nodes.size == 0:
            raise ValueError(
                f"the boundary {name!r} selects no node of this mesh; raise `resolution` or "
                "widen the selector, because the condition on it would apply to nothing"
            )
        if values.get("fixed"):
            free[2 * nodes] = 0.0
            free[2 * nodes + 1] = 0.0
        if values.get("fixed_x"):
            free[2 * nodes] = 0.0
        if values.get("fixed_y"):
            free[2 * nodes + 1] = 0.0

        traction = (values.get("traction_x", 0.0), values.get("traction_y", 0.0))
        if not any(traction):
            continue
        spans = selected[segments[:, 0]] & selected[segments[:, 1]]
        if not spans.any():
            raise ValueError(
                f"the boundary {name!r} carries a traction but spans no edge of this mesh; "
                "it selects isolated nodes, and a traction is a load per unit length"
            )
        overlap = np.nonzero(spans & (claimed >= 0))[0]
        if overlap.size:
            other = loaded_by[claimed[overlap[0]]]
            raise ValueError(f"{OVERLAPPING_TRACTIONS} ({other!r} and {name!r} do)")
        claimed[spans] = len(loaded_by)
        loaded_by.append(name)

        loaded = segments[spans]
        length = np.hypot(*(points[loaded[:, 1]] - points[loaded[:, 0]]).T)
        np.add.at(rhs, 2 * loaded, (traction[0] * length / 2.0)[:, None])
        np.add.at(rhs, 2 * loaded + 1, (traction[1] * length / 2.0)[:, None])

    return free, rhs


def conjugate_gradients(apply, rhs, diagonal, free, ctx, iterations, report_every):
    """Jacobi-preconditioned CG on the free degrees of freedom.

    Hand-rolled for the reason every mock here is: NumPy is the only numeric dependency the
    base install has, and adding SciPy for one solver would make `pip install fenix-spoon`
    heavier for everybody who never runs it. CG is the right method anyway — the stiffness
    matrix is symmetric positive definite once the fixed degrees of freedom are removed.
    """
    u = np.zeros_like(rhs)
    r = rhs * free
    inverse_diagonal = np.where(diagonal > 0, 1.0 / np.maximum(diagonal, 1e-300), 0.0) * free
    z = r * inverse_diagonal
    p = z.copy()
    rz = float(r @ z)
    scale = max(float(np.max(np.abs(rhs))), 1e-300)

    residual = float(np.max(np.abs(r))) / scale
    iteration = 0
    for iteration in range(1, iterations + 1):
        ctx.check_cancelled()
        ap = apply(p) * free
        denominator = float(p @ ap)
        if denominator <= 0.0:
            break  # a lost cause rather than a silent wrong answer; reported as not converged
        alpha = rz / denominator
        u += alpha * p
        r -= alpha * ap
        residual = float(np.max(np.abs(r))) / scale
        if iteration % report_every == 0 or iteration == iterations:
            ctx.progress(ProgressEvent(iteration=iteration, total=iterations, residual=residual))
        if residual < 1e-12:
            break
        z = r * inverse_diagonal
        rz_next = float(r @ z)
        p = z + (rz_next / rz) * p
        rz = rz_next
    return u, residual, iteration


@register
class MockElasticity2D(Solver):
    name = "mock.elasticity2d"
    title = "Linear elasticity (mock, NumPy)"
    description = (
        "2D linear elasticity of a plate: displacement and von Mises stress under a uniform "
        "traction, with a clamp. Restraints and loads go on boundaries the geometry names, "
        "or on edges of the bounding rectangle as a shorthand. Constant-strain triangles solved by "
        "preconditioned conjugate gradients. Development stand-in that runs anywhere NumPy "
        "does; exact for uniform tension and stiff in bending, as CST always is."
    )
    geometry_types = ["domain2d", "regions2d"]
    physics = "elasticity"
    availability = "mock"
    metrics = ELASTICITY_METRICS
    assumptions = ELASTICITY_ASSUMPTIONS
    conditions = ELASTICITY_CONDITIONS
    #: CG to a fixed tolerance with no randomness: same inputs, same array. Safe to cache (#47).
    #: The load case is part of the key (#85), so two load cases on one shape are two
    #: entries rather than one answer served to both.
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="cantilever under end shear",
            description=(
                "The textbook case: clamped at xmin, sheared down at xmax. Compare the tip "
                "displacement with PL^3/3EI — and expect this element to come out stiff."
            ),
            params={"resolution": 160, "fixed_edge": "xmin", "load_edge": "xmax",
                    "traction": [0.0, -1.0e6]},
        ),
        CapabilityExample(
            title="plate in tension",
            description=(
                "Pulled along x. With a hole in the geometry this is the stress-concentration "
                "case; without one it is uniform, and CST reproduces it exactly."
            ),
            params={"resolution": 192, "fixed_edge": "xmin", "load_edge": "xmax",
                    "traction": [1.0e6, 0.0]},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=128, ge=16, le=384, description="Grid points along the longer domain edge"
        )
        youngs_modulus: float = Field(
            default=2.1e11, gt=0.0, description="E in Pa; steel by default. Overridden per region."
        )
        poisson_ratio: float = Field(
            default=0.3, ge=0.0, lt=0.5, description="nu; 0.5 is incompressible and excluded."
        )
        plane: Literal["stress", "strain"] = Field(
            default="stress",
            description=(
                "`stress` for a thin plate loaded in its plane, `strain` for a long prismatic "
                "body. The choice changes every stress this reports."
            ),
        )
        fixed_edge: Edge = Field(
            default="xmin",
            description=(
                "Edge of the bounding rectangle clamped in both directions. **Ignored when a "
                "load case is supplied** (#85): conditions on named boundaries replace this "
                "and `load_edge` entirely rather than adding to them."
            ),
        )
        load_edge: Edge = Field(
            default="xmax",
            description=(
                "Edge the traction is applied to; must differ from `fixed_edge`. Ignored "
                "when a load case is supplied."
            ),
        )
        traction: tuple[float, float] = Field(
            default=(0.0, -1.0e6),
            description=(
                "Uniform traction on `load_edge` as [tx, ty] in Pa (force per unit area). "
                "Ignored when a load case is supplied; use `traction_x` / `traction_y` there."
            ),
        )
        iterations: int = Field(default=4000, ge=10, le=60000)
        report_every: int = Field(default=100, ge=1)
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

        @model_validator(mode="after")
        def _check_edges(self) -> "MockElasticity2D.Params":
            if self.fixed_edge == self.load_edge:
                raise ValueError("fixed_edge and load_edge must be different edges")
            return self

    @classmethod
    def estimate_cells(cls, geometry, params: "MockElasticity2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    def solve(
        self, geometry: Domain2D | Regions2D, params: "MockElasticity2D.Params", ctx: SolverContext
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)

        # A `domain2d` obstacle is a hole in the plate — the stress-concentration case. A
        # `regions2d` is filled, so nothing is removed and the material varies instead.
        hole = (
            polygon_mask(np.asarray(geometry.obstacle.points, dtype=float), xx, yy)
            if isinstance(geometry, Domain2D)
            else np.zeros((ny, nx), dtype=bool)
        )

        mesh = grid_to_mesh2d(x, y, hole, {})
        points = np.asarray(mesh["points"], dtype=float)
        triangles = np.asarray(mesh["triangles"], dtype=np.int64)
        if len(triangles) == 0:
            raise ValueError("the obstacle removed every cell; nothing is left to solve")

        centroids = points[triangles].mean(axis=1)
        modulus, poisson = sample_material(
            geometry, centroids, params.youngs_modulus, params.poisson_ratio
        )
        b_matrix, area = strain_displacement(points, triangles)
        d_matrix = constitutive(modulus, poisson, params.plane)

        # K_e = A * B^T D B, thickness one: everything here is per unit depth in z, which is
        # what `two_dimensional` promises.
        k_local = area[:, None, None] * np.einsum("eki,ekl,elj->eij", b_matrix, d_matrix, b_matrix)
        dofs = np.repeat(triangles * 2, 2, axis=1)
        dofs[:, 1::2] += 1
        n_dof = 2 * len(points)

        def apply(vector: np.ndarray) -> np.ndarray:
            local = np.einsum("eij,ej->ei", k_local, vector[dofs])
            return np.bincount(dofs.ravel(), weights=local.ravel(), minlength=n_dof)

        diagonal = np.bincount(
            dofs.ravel(),
            weights=np.einsum("eii->ei", k_local).ravel(),
            minlength=n_dof,
        )

        tolerance = 1e-9 * min(xmax - xmin, ymax - ymin)
        if ctx.conditions:
            # Total precedence, not a merge: see the module docstring. The two edge
            # parameters keep their defaults and go unread.
            free, rhs = apply_conditions(
                geometry, ctx.conditions, points, triangles, n_dof
            )
        else:
            free = np.ones(n_dof)
            clamped = edge_nodes(points, geometry.bounds, params.fixed_edge, tolerance)
            free[2 * clamped] = 0.0
            free[2 * clamped + 1] = 0.0
            rhs = self._edge_traction(points, geometry.bounds, params, tolerance, n_dof)

        # Both checks apply to either route, and both catch a well-posedness failure that
        # would otherwise come back as a plausible-looking field. Without a restraint the
        # stiffness matrix is singular and CG wanders off along a rigid-body mode; without a
        # load the answer is zero everywhere, which reads as "this structure is fine".
        if np.all(free > 0):
            raise ValueError(
                "nothing is restrained, so the problem is singular and only a rigid-body "
                "motion satisfies it; fix at least one boundary"
            )
        if not np.any(rhs[free > 0]):
            raise ValueError(
                "the traction produced no load on any free node; check the loaded boundary "
                "and its traction"
            )

        solution, residual, iterations = conjugate_gradients(
            apply, rhs, diagonal, free, ctx, params.iterations, params.report_every
        )

        displacement = solution.reshape(-1, 2)
        strain = np.einsum("eij,ej->ei", b_matrix, solution[dofs])
        stress = np.einsum("eij,ej->ei", d_matrix, strain)
        equivalent = von_mises(stress, poisson, params.plane)

        # Element stresses averaged to the nodes, which is what a viewer draws.
        nodal_stress = nodal_average(triangles, equivalent, area, len(points))

        keep = ~hole
        grids = {name: np.zeros((ny, nx)) for name in ("u_mag", "sigma_vm")}
        grids["u_mag"][keep] = np.hypot(displacement[:, 0], displacement[:, 1])
        grids["sigma_vm"][keep] = nodal_stress
        vectors = np.zeros((ny, nx, 2))
        vectors[keep] = displacement

        converged = residual < 1e-12
        if params.write_vtk:
            write_vtk_structured_points(ctx.artifact("solution.vtk"), x, y, grids)

        return SolverResult(
            kind="mesh2d",
            stats={
                "cells": float(len(triangles)),
                "dofs": float(n_dof),
                "iterations": float(iterations),
            },
            # `u_max` and `sigma_vm_max` are declared reductions of fields in the payload, so
            # the runtime fills them. Compliance needs the load vector, which is not in the
            # payload, so it is computed here.
            metrics={"compliance": float(rhs @ solution)},
            converged=converged,
            residual=residual,
            warnings=(
                []
                if converged
                else [
                    f"conjugate gradients stopped at {iterations} iterations with relative "
                    f"residual {residual:.3g}; the displacement is not converged"
                ]
            ),
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                **grid_to_mesh2d(x, y, hole, grids, {"u": vectors}),
            },
        )

    @staticmethod
    def _edge_traction(points, bounds, params, tolerance: float, n_dof: int) -> np.ndarray:
        """Consistent nodal forces for a uniform traction on one edge.

        Each segment between adjacent edge nodes carries `traction * length`, split evenly
        between its two ends — the exact consistent load for a linear element, and the reason
        a refined mesh converges to the same total force rather than a different one.
        """
        rhs = np.zeros(n_dof)
        loaded = edge_nodes(points, bounds, params.load_edge, tolerance)
        along = 1 if params.load_edge in ("xmin", "xmax") else 0
        for start, end in zip(loaded[:-1], loaded[1:], strict=False):
            length = abs(points[end, along] - points[start, along])
            for node in (start, end):
                rhs[2 * node] += params.traction[0] * length / 2.0
                rhs[2 * node + 1] += params.traction[1] * length / 2.0
        return rhs
