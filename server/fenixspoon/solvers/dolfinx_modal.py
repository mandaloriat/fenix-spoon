"""FEniCSx adapter: natural frequencies of a plane structure, with SLEPc (issue #101).

Same question as ``mock.modal2d`` — the generalised eigenproblem of free vibration,

    K phi = omega^2 M phi ,      f = omega / 2 pi

— on a Gmsh mesh with the **consistent** mass matrix and a Krylov-Schur eigensolver, where
the mock lumps the mass and factorises densely. The two therefore bracket rather than agree:
lumping under-predicts, the consistent matrix over-predicts, and both errors shrink with the
mesh. That is declared in `discretised_mass` and it is what makes cross-validating the pair
informative instead of circular.

## Shift-and-invert, and why the shift is not zero

An unrestrained structure has a singular stiffness matrix — three rigid-body motions in the
plane cost no strain energy — so the factorisation shift-and-invert needs cannot be taken at
zero. It is taken **just below** it instead: `K - sigma M` with `sigma < 0` is positive
definite for any negative shift, and the transformed problem still returns the eigenvalues
nearest zero first, which are the ones a modal model is built from.

How far below matters. Too close and the factorisation inherits the singularity; too far and
the wanted end of the spectrum is no longer the nearest. The shift here is a fixed small
fraction of the structure's own characteristic eigenvalue, `(c/L)^2` with `c = sqrt(E/rho)` —
a quantity that scales with the problem, so a MEMS resonator and a bridge deck get the same
conditioning rather than the same number.

## The restrained degrees of freedom are given an infinite frequency, not a large one

Dirichlet rows get a 1 on the stiffness diagonal and a **0** on the mass diagonal, so the
pencil has `lambda = 1/0` there: those modes sit at infinity, which shift-and-invert maps to
the far end of the transformed spectrum where they cannot be confused with anything wanted.
Penalising them with a large stiffness instead — the other common trick — would put a family
of spurious high-frequency modes into the answer, and the caller would have to know to
discard them.

Registers only when ``dolfinx``, ``gmsh``, ``mpi4py`` and ``slepc4py`` import cleanly. Tests
live in ``tests/test_dolfinx_modal.py`` (``pytest -m fenics``).
"""

import numpy as np
import ufl
from dolfinx import default_scalar_type, fem
from dolfinx.fem import petsc as fem_petsc
from pydantic import BaseModel, Field
from slepc4py import SLEPc

from ..geometry import Domain2D, Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import MODAL_ASSUMPTIONS, MODAL_CONDITIONS, MODAL_METRICS, MODE_ARTIFACT
from .dolfinx_elasticity import edge_locator, lame, restraints
from .dolfinx_magnetostatics import _build_tagged_mesh
from .dolfinx_poisson import _build_mesh as _build_holed_mesh
from .dolfinx_poisson import _p1_mesh_data, _write_vtk_unstructured, estimate_triangles
from .mock_modal import count_rigid_body_modes, spectrum_series
from .registry import register

#: The shift, as a fraction of the structure's characteristic eigenvalue `(c/L)^2`.
#:
#: Small enough that the eigenvalues nearest it are the ones nearest zero — the wanted end —
#: and large enough that `K - sigma M` is comfortably factorisable for a floating structure.
#: A fraction rather than a number because the eigenvalue itself spans twenty orders of
#: magnitude across the structures this might see, and a fixed shift would be either useless
#: or dominant on most of them.
SHIFT_FRACTION = 1e-4


def _assemble_with_diagonal(form, bcs, value: float):
    """Assemble a matrix, putting `value` on the rows a Dirichlet condition holds.

    Two spellings because dolfinx has had two: the keyword was `diagonal` up to 0.7 and is
    `diag` from 0.8, and the second is what 0.11 — the version this repository pins — accepts.
    Handled the way this package already handles `LinearProblem`'s options prefix, by trying
    and falling back, so an adapter works across the versions a user might have installed
    rather than only the one CI runs.

    The value matters here in a way it does not for a linear solve: a **zero** mass diagonal
    is what sends a held degree of freedom to an infinite eigenvalue instead of putting a
    spurious finite mode in the middle of the spectrum.
    """
    try:
        return fem_petsc.assemble_matrix(form, bcs=bcs, diag=value)
    except TypeError:  # dolfinx <= 0.7 spelled it `diagonal`
        return fem_petsc.assemble_matrix(form, bcs=bcs, diagonal=value)


def characteristic_frequency(geometry, youngs_modulus: float, density: float) -> float:
    """A frequency the structure's own material and size imply, in hertz.

    Not a physical mode — it is `c / L`, the time a wave takes to cross the body — and that is
    exactly what makes it the right yardstick for two things that need one and have no better:
    the eigensolver's shift, and the floor below which a computed frequency is arithmetic
    rather than motion.
    """
    xmin, ymin, xmax, ymax = geometry.bounds
    span = max(xmax - xmin, ymax - ymin)
    return float(np.sqrt(youngs_modulus / density) / span)


def _materials(geometry, params) -> tuple[float, float, float]:
    """The stiffest, lightest material in play, for scaling decisions only.

    Region materials would each give a different characteristic frequency, and the shift needs
    one number. The extreme is the safe pick for both uses: it puts the shift below every
    region's fundamental rather than inside some region's spectrum.
    """
    modulus = float(params.youngs_modulus)
    density = float(params.density)
    if isinstance(geometry, Regions2D):
        candidates = [geometry.background, *(region.material for region in geometry.regions)]
        modulus = max(float(entry.get("youngs_modulus", modulus)) for entry in candidates)
        density = min(float(entry.get("density", density)) for entry in candidates)
    return modulus, density, float(params.poisson_ratio)


@register
class DolfinxModal2D(Solver):
    name = "dolfinx.modal2d"
    title = "Natural frequencies (FEniCSx, SLEPc)"
    description = (
        "Free-vibration modes of a plane structure on a Gmsh mesh: the generalised "
        "eigenproblem K phi = omega^2 M phi with a consistent mass matrix, solved by SLEPc "
        "with shift-and-invert about a small negative shift so that unrestrained structures "
        "work. Returns the spectrum as a curve and each mode shape as an artifact, with "
        "rigid-body modes reported rather than filtered."
    )
    geometry_types = ["domain2d", "regions2d"]
    physics = "elasticity-modal"
    availability = "fenicsx"
    requires = ["dolfinx", "gmsh", "slepc4py"]
    metrics = MODAL_METRICS
    assumptions = MODAL_ASSUMPTIONS
    conditions = MODAL_CONDITIONS
    #: Single-rank meshing, a direct factorisation inside the spectral transform, and a
    #: deterministic Krylov-Schur restart: the same inputs give the same spectrum. The
    #: eigen*vectors* of a degenerate pair are only determined up to a rotation within their
    #: eigenspace, which is a property of the mathematics rather than of this implementation —
    #: the frequencies a caller reads, and the cache key they are served under, are stable.
    deterministic = True
    artifacts = [MODE_ARTIFACT]
    examples = [
        CapabilityExample(
            title="free-free",
            description=(
                "Nothing restrained: three rigid-body modes, then the elastic ones. This is "
                "the case the negative shift exists for."
            ),
            params={"mesh_size": 0.02, "n_modes": 8, "mode": 4},
        ),
        CapabilityExample(
            title="cantilever",
            description=(
                "Clamped at xmin, and comparable with the Euler-Bernoulli beam formula. A "
                "quadratic element earns its keep here: bending is what these elements are "
                "worst at, and the first mode is bending."
            ),
            params={"mesh_size": 0.02, "degree": 2, "fixed_edge": "xmin", "n_modes": 6},
        ),
    ]

    class Params(BaseModel):
        mesh_size: float = Field(default=0.03, gt=0.0, description="Target element size")
        degree: int = Field(
            default=1,
            ge=1,
            le=2,
            description=(
                "Lagrange element degree. 2 resolves bending far better than 1 for the same "
                "element count, which matters more here than for a static solve: the lowest "
                "modes of most structures are bending modes."
            ),
        )
        n_modes: int = Field(
            default=6,
            ge=1,
            le=50,
            description=(
                "How many modes to return, counting from the lowest frequency and **including "
                "rigid-body modes**; `rigid_body_modes` says how many of them there were."
            ),
        )
        mode: int = Field(
            default=1,
            ge=1,
            description="Which mode's shape comes back in the result payload, 1-based.",
        )
        youngs_modulus: float = Field(default=2.1e11, gt=0.0, description="E in Pa; steel.")
        poisson_ratio: float = Field(default=0.3, ge=0.0, lt=0.5, description="nu.")
        density: float = Field(
            default=7850.0, gt=0.0, description="rho in kg/m^3; overridden per region."
        )
        plane: str = Field(
            default="stress", description="`stress` for a thin plate, `strain` for a long body."
        )
        fixed_edge: str = Field(
            default="none",
            description=(
                "Edge of the bounding rectangle clamped in both directions, or `none`. "
                "**`none` is a real answer**: an unrestrained structure has modes, and three "
                "of them are rigid-body motions."
            ),
        )
        write_vtk: bool = Field(
            default=True, description="Attach each computed mode shape as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry, params: "DolfinxModal2D.Params") -> int:
        return estimate_triangles(geometry.bounds, params.mesh_size)

    def solve(
        self, geometry: Domain2D | Regions2D, params: "DolfinxModal2D.Params", ctx: SolverContext
    ) -> SolverResult:
        if params.mode > params.n_modes:
            raise ValueError(
                f"`mode` is {params.mode} but only {params.n_modes} modes are being computed; "
                "raise `n_modes` or ask for a lower mode"
            )

        ctx.progress(ProgressEvent(iteration=0, total=4, message="meshing with Gmsh"))
        if isinstance(geometry, Domain2D):
            msh, cell_tags = _build_holed_mesh(geometry, params.mesh_size), None
        else:
            msh, cell_tags = _build_tagged_mesh(geometry, params.mesh_size)

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=1, total=4, message="assembling K and M"))

        constants = fem.functionspace(msh, ("DG", 0))
        mu_field, lmbda_field, rho_field = (fem.Function(constants) for _ in range(3))
        mu, lmbda = lame(params.youngs_modulus, params.poisson_ratio, params.plane)
        rho = float(params.density)
        if isinstance(geometry, Regions2D):
            mu, lmbda = lame(
                float(geometry.background.get("youngs_modulus", params.youngs_modulus)),
                float(geometry.background.get("poisson_ratio", params.poisson_ratio)),
                params.plane,
            )
            rho = float(geometry.background.get("density", params.density))
        mu_field.x.array[:] = mu
        lmbda_field.x.array[:] = lmbda
        rho_field.x.array[:] = rho
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
                rho_field.x.array[dofs] = float(region.material.get("density", params.density))

        V = fem.functionspace(msh, ("Lagrange", params.degree, (2,)))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)

        def epsilon(w):
            return ufl.sym(ufl.grad(w))

        def sigma(w):
            return 2.0 * mu_field * epsilon(w) + lmbda_field * ufl.tr(epsilon(w)) * ufl.Identity(2)

        stiffness_form = fem.form(ufl.inner(sigma(u), epsilon(v)) * ufl.dx)
        mass_form = fem.form(rho_field * ufl.inner(u, v) * ufl.dx)

        xmin, ymin, xmax, ymax = geometry.bounds
        eps = 1e-9 * min(xmax - xmin, ymax - ymin)
        if ctx.conditions:
            bcs = restraints(msh, V, geometry, ctx.conditions)
        elif params.fixed_edge != "none":
            clamped = fem.locate_dofs_geometrical(
                V, edge_locator(geometry.bounds, params.fixed_edge, eps)
            )
            if len(clamped) == 0:
                raise ValueError(
                    f"no mesh node lies on the {params.fixed_edge!r} edge, so nothing is held "
                    "there; with `regions2d` the material must reach the bounding rectangle"
                )
            bcs = [
                fem.dirichletbc(
                    fem.Constant(msh, np.zeros(2, dtype=default_scalar_type)), clamped, V
                )
            ]
        else:
            # No restraint at all, and no refusal — unlike the static twin, where this is a
            # singular problem. Here it is the *free-free* case, which is the one an
            # eigensolve most needs to get right.
            bcs = []

        # A 1 on the stiffness diagonal and a 0 on the mass diagonal sends every held degree
        # of freedom to an infinite eigenvalue, out of the way of the wanted end.
        stiffness = fem_petsc.assemble_matrix(stiffness_form, bcs=bcs)
        stiffness.assemble()
        mass = _assemble_with_diagonal(mass_form, bcs, 0.0)
        mass.assemble()

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=4, message="eigensolve (SLEPc)"))
        modulus, density, _ = _materials(geometry, params)
        reference = characteristic_frequency(geometry, modulus, density)
        shift = -SHIFT_FRACTION * (2.0 * np.pi * reference) ** 2

        solver = SLEPc.EPS().create(msh.comm)
        solver.setOperators(stiffness, mass)
        solver.setProblemType(SLEPc.EPS.ProblemType.GHEP)
        solver.setDimensions(nev=params.n_modes, ncv=min(4 * params.n_modes + 20, 200))
        solver.setWhichEigenpairs(SLEPc.EPS.Which.TARGET_MAGNITUDE)
        solver.setTarget(shift)
        transform = solver.getST()
        transform.setType(SLEPc.ST.Type.SINVERT)
        transform.setShift(shift)
        ksp = transform.getKSP()
        ksp.setType("preonly")
        ksp.getPC().setType("lu")
        solver.setTolerances(tol=1e-9, max_it=1000)
        solver.solve()

        converged = solver.getConverged()
        if converged < params.n_modes:
            raise ValueError(
                f"SLEPc converged {converged} of the {params.n_modes} modes asked for; ask "
                "for fewer modes, or refine the mesh so the wanted end of the spectrum is "
                "better separated"
            )

        vector = stiffness.createVecRight()
        found = []
        for index in range(converged):
            eigenvalue = solver.getEigenpair(index, vector).real
            found.append((max(float(eigenvalue), 0.0), vector.array_r.copy()))
        found.sort(key=lambda pair: pair[0])
        found = found[: params.n_modes]

        frequencies = np.array([np.sqrt(value) / (2.0 * np.pi) for value, _ in found])
        rigid = count_rigid_body_modes(frequencies, reference)
        elastic = frequencies[rigid:]

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=3, total=4, message="post-processing"))
        linear = fem.functionspace(msh, ("Lagrange", 1))
        points, triangles, _ = _p1_mesh_data(linear, fem.Function(linear))
        nodal_space = fem.functionspace(msh, ("Lagrange", 1, (2,)))

        shape_of = fem.Function(V)
        nodal = fem.Function(nodal_space)

        def nodal_shape(index: int) -> np.ndarray:
            """One mode as P1 vertex displacements — what a `mesh2d` payload carries."""
            shape_of.x.array[:] = found[index][1]
            nodal.interpolate(shape_of)
            return np.asarray(nodal.x.array.real, dtype=float).reshape(-1, 2)[: len(points)]

        chosen = nodal_shape(params.mode - 1)
        fields = {"u_mag": np.hypot(chosen[:, 0], chosen[:, 1])}

        if params.write_vtk:
            for index in range(len(found)):
                vectors = nodal_shape(index)
                _write_vtk_unstructured(
                    ctx.artifact(f"mode_{index + 1}.vtk", mode=index + 1),
                    points,
                    triangles,
                    {"u_mag": np.hypot(vectors[:, 0], vectors[:, 1])},
                )

        ctx.progress(ProgressEvent(iteration=4, total=4, message="done"))
        return SolverResult(
            kind="mesh2d",
            stats={
                "cells": float(len(triangles)),
                "dofs": float(V.dofmap.index_map.size_global * V.dofmap.index_map_bs),
                "modes": float(len(found)),
            },
            metrics={
                "rigid_body_modes": float(rigid),
                **({"f_1": float(elastic[0])} if elastic.size else {}),
            },
            warnings=(
                []
                if elastic.size
                else [
                    f"all {params.n_modes} computed modes are rigid-body motions, so no "
                    "elastic frequency was reached; raise `n_modes`"
                ]
            ),
            converged=True,
            series=[spectrum_series(frequencies, rigid)],
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "points": points.tolist(),
                "triangles": triangles.tolist(),
                "point_fields": {name: value.tolist() for name, value in fields.items()},
                "point_vector_fields": {"u": chosen.tolist()},
            },
        )
