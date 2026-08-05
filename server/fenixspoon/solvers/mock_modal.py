"""Mock solver: natural frequencies and mode shapes of a plane structure (issue #101).

Solves the **generalised** eigenproblem of free vibration,

    K phi = omega^2 M phi ,      f = omega / 2 pi ,

on the same constant-strain triangulation :mod:`fenixspoon.solvers.mock_elasticity` builds,
with the same element stiffness — this is that adapter's structure asked a different question.
Every helper the two share is imported rather than repeated, so a disagreement between them
would have to be a disagreement about the *question* rather than about the material, the
element or the mesh.

## Why this is a third kind of question

Every other adapter in this package is a **forward** solve: given a load, what does the
structure do. An eigensolve is given nothing and asks what the structure does *by itself* —
so there is no right-hand side, no load case that pushes, and an answer that is a spectrum
rather than a field. What it needs from the protocol turned out to be almost nothing, which
is the finding #101 predicted: the frequencies are a `series1d` (#69 built that shape and
listed this exact use in its own table), and the mode shapes are an index over artifacts
(#86 built that too, for time). The one thing it needed was a name for an ordering that is
not time — see :mod:`fenixspoon.modes`.

## Free-free must work, and is the interesting case

An unrestrained structure floats: three rigid-body motions (two translations and a rotation)
cost no strain energy, so **K is singular**, and a solver that assumed otherwise would return
garbage or fail. They are not filtered out — they are reported, counted, and used as a check.
A plane structure with anything other than three of them is not the model its author
described, and `rigid_body_modes` is how a caller sees that.

The transformation this adapter uses is what makes the singular case ordinary: with a lumped
(diagonal) mass matrix, `M^-1/2 K M^-1/2` is a *standard* symmetric eigenproblem with the
same eigenvalues, so `numpy.linalg.eigh` answers it — no shift, no factorisation of K, and no
SciPy, which is the constraint every mock in this package is written under. Degenerate pairs
need no special handling either: `eigh` returns an orthogonal basis of each eigenspace, and
the mirror-symmetric structures where degeneracy shows up are exactly the common case.

**Lumped mass is a modelling choice with a direction**: it under-predicts frequencies where
the constant-strain element over-predicts them by being stiff in bending, so the two errors
do not add. Both are declared in `discretised_mass`, and both shrink with resolution.

## The cost, and why the resolution ceiling is lower here

`eigh` is dense and cubic in the number of degrees of freedom. Every other mock is iterative
and linear-ish, so this one carries a tighter `resolution` cap and says why: a grid that is
merely slow for a Laplace solve is minutes of factorisation here.

Material keys read from a `regions2d` geometry (everything else ignored):

- ``youngs_modulus`` — E in Pa (default: the `youngs_modulus` parameter)
- ``poisson_ratio`` — nu (default: the `poisson_ratio` parameter)
- ``density`` — rho in kg/m^3 (default: the `density` parameter)
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Domain2D, Regions2D
from ..series import Series1DData, SeriesAxis, SeriesTrace
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import MODAL_ASSUMPTIONS, MODAL_CONDITIONS, MODAL_METRICS, MODE_ARTIFACT
from .mock_elasticity import (
    constitutive,
    edge_nodes,
    sample_material,
    strain_displacement,
)
from .mock_laplace import _grid_shape, grid_to_mesh2d, polygon_mask, write_vtk_structured_points
from .registry import register

#: A mode is rigid-body when its frequency is below this fraction of the **spectral radius**.
#:
#: Relative, and relative to the *top* of the spectrum rather than to anything physical,
#: because that is where the arithmetic's own noise floor is: a null eigenvalue comes back as
#: round-off against the largest one, at about `eps * lambda_max`, so in frequency terms it
#: lands near `1e-8` of the highest. The lowest elastic mode of a slender structure sits
#: around `1e-3` of it. The threshold below is three orders clear of each — a wide valley, not
#: a cliff — and it needs no knowledge of the material, the units or the shape to be there.
RIGID_BODY_FRACTION = 1e-5


def lumped_mass(
    triangles: np.ndarray, area: np.ndarray, density: np.ndarray, n_dof: int
) -> np.ndarray:
    """The diagonal mass matrix: each triangle's mass shared equally between its three nodes.

    Row-summing a consistent mass matrix gives the same thing for a constant-strain triangle,
    which is why this is the standard lumping rather than an approximation invented here. Both
    displacement components of a node carry the same mass, since a node is a point with mass
    and no preferred direction.

    Diagonal is what makes the eigenproblem tractable without SciPy: it is invertible by
    element-wise arithmetic, so the generalised problem becomes a standard symmetric one
    exactly rather than iteratively.
    """
    nodal = np.bincount(
        triangles.ravel(), weights=np.repeat(density * area / 3.0, 3), minlength=n_dof // 2
    )
    return np.repeat(nodal, 2)


def natural_frequencies(
    stiffness: np.ndarray, mass: np.ndarray, free: np.ndarray, count: int
) -> tuple[np.ndarray, np.ndarray, float]:
    """The lowest `count` natural frequencies in Hz, and their mode shapes.

    The generalised problem `K phi = lambda M phi` becomes the standard symmetric problem
    `(M^-1/2 K M^-1/2) psi = lambda psi` with `phi = M^-1/2 psi`, which is exact for a
    diagonal M and leaves the problem symmetric — so `eigh` applies, eigenvalues come back
    sorted and real, and a degenerate pair comes back as an orthogonal basis of its
    eigenspace rather than as one vector twice.

    Restrained degrees of freedom are **removed** rather than penalised. Holding them with a
    large diagonal stiffness is the other common trick and it puts a family of spurious
    high-frequency modes into the spectrum, which the caller then has to know to ignore.

    Shapes come back **mass-normalised** (`phi^T M phi = 1`), the convention a modal model is
    built in, and are returned in the full degree-of-freedom numbering with zeros where the
    structure is held.

    The third return value is the **spectral radius in hertz** — the highest frequency the
    discretisation carries. It is nobody's physics (it is a property of the smallest element)
    and it is exactly the reference a numerically-zero eigenvalue has to be judged against, so
    it is returned rather than recomputed by the caller from something less suitable.
    """
    kept = np.nonzero(free > 0)[0]
    if len(kept) < count:
        raise ValueError(
            f"this mesh has {len(kept)} free degrees of freedom, fewer than the {count} modes "
            "asked for; raise `resolution` or lower `n_modes`"
        )
    scale = 1.0 / np.sqrt(mass[kept])
    reduced = stiffness[np.ix_(kept, kept)] * scale[:, None] * scale[None, :]
    # Symmetrised against round-off: the assembly is symmetric by construction, and `eigh`
    # reads only one triangle, so an asymmetric drift would be silently resolved one way.
    eigenvalues, vectors = np.linalg.eigh(0.5 * (reduced + reduced.T))

    # Negative eigenvalues are round-off about zero on the rigid-body modes, not physics:
    # clipping is what keeps `sqrt` real, and their magnitude is checked by the count of
    # rigid-body modes rather than hidden by the clip.
    omega = np.sqrt(np.maximum(eigenvalues[:count], 0.0))
    shapes = np.zeros((count, len(free)))
    shapes[:, kept] = (vectors[:, :count] * scale[:, None]).T
    radius = float(np.sqrt(max(eigenvalues[-1], 0.0)) / (2.0 * np.pi))
    return omega / (2.0 * np.pi), shapes, radius


def count_rigid_body_modes(frequencies: np.ndarray, spectral_radius: float) -> int:
    """How many of these are motions that cost no strain energy.

    Judged against the top of the spectrum, for the reason :data:`RIGID_BODY_FRACTION`
    gives: a null mode's frequency is the arithmetic's noise, and noise is measured against
    the largest number in the problem. A fully restrained structure has none, and the same
    test says so without a special case.
    """
    if spectral_radius <= 0.0:
        return 0
    return int(np.sum(frequencies <= RIGID_BODY_FRACTION * spectral_radius))


@register
class MockModal2D(Solver):
    name = "mock.modal2d"
    title = "Natural frequencies (mock, NumPy)"
    description = (
        "Free-vibration modes of a plane structure: the generalised eigenproblem K phi = "
        "omega^2 M phi on constant-strain triangles with a lumped mass matrix, solved densely "
        "with NumPy. Returns the spectrum as a curve and each mode shape as an artifact. "
        "Unrestrained structures are supported and their rigid-body modes are reported rather "
        "than filtered. Development stand-in that runs anywhere NumPy does."
    )
    geometry_types = ["domain2d", "regions2d"]
    physics = "elasticity-modal"
    availability = "mock"
    metrics = MODAL_METRICS
    assumptions = MODAL_ASSUMPTIONS
    conditions = MODAL_CONDITIONS
    #: A dense symmetric eigensolve with no randomness: same inputs, same spectrum. Safe to
    #: cache (#47), and the load case is part of the key — a clamped structure and a free one
    #: are two different questions about one shape.
    deterministic = True
    artifacts = [MODE_ARTIFACT]
    examples = [
        CapabilityExample(
            title="free-free",
            description=(
                "Nothing restrained, which is the case an eigensolve has to get right: three "
                "rigid-body modes at zero, then the elastic ones. `mode: 4` puts the first "
                "elastic shape in the payload rather than a translation."
            ),
            params={"resolution": 48, "n_modes": 8, "mode": 4},
        ),
        CapabilityExample(
            title="cantilever",
            description=(
                "Clamped at xmin. The first frequency is checkable by hand against the "
                "Euler-Bernoulli beam formula, and this element being stiff in bending puts "
                "the answer a few percent above it."
            ),
            params={"resolution": 64, "fixed_edge": "xmin", "n_modes": 6},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=48,
            ge=16,
            le=128,
            description=(
                "Grid points along the longer domain edge. **Capped lower than the other "
                "mocks**: this solve is a dense eigendecomposition, so its cost grows with "
                "the cube of the degrees of freedom rather than linearly."
            ),
        )
        n_modes: int = Field(
            default=6,
            ge=1,
            le=50,
            description=(
                "How many modes to return, counting from the lowest frequency and **including "
                "rigid-body modes**. Numbering follows the spectrum rather than the physics, "
                "so mode 1 of an unrestrained structure is a translation; `rigid_body_modes` "
                "says how many to skip."
            ),
        )
        mode: int = Field(
            default=1,
            ge=1,
            description=(
                "Which mode's shape comes back in the result payload, 1-based. The others are "
                "artifacts. Must not exceed `n_modes`."
            ),
        )
        youngs_modulus: float = Field(
            default=2.1e11, gt=0.0, description="E in Pa; steel by default. Overridden per region."
        )
        poisson_ratio: float = Field(
            default=0.3, ge=0.0, lt=0.5, description="nu; 0.5 is incompressible and excluded."
        )
        density: float = Field(
            default=7850.0,
            gt=0.0,
            description=(
                "rho in kg/m^3; steel by default, overridden per region by a `density` "
                "material key. Frequencies scale as 1/sqrt(rho), so this is not a detail."
            ),
        )
        plane: Literal["stress", "strain"] = Field(
            default="stress",
            description=(
                "`stress` for a thin plate, `strain` for a long prismatic body — the same "
                "choice the elasticity pair offers, and it moves every frequency."
            ),
        )
        fixed_edge: Literal["none", "xmin", "xmax", "ymin", "ymax"] = Field(
            default="none",
            description=(
                "Edge of the bounding rectangle clamped in both directions, as a shorthand. "
                "**`none` is the default and is a real answer**, not a missing one: an "
                "unrestrained structure has natural frequencies, and three of them are zero. "
                "Ignored entirely when a load case is supplied (#85)."
            ),
        )
        write_vtk: bool = Field(
            default=True, description="Attach each computed mode shape as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry, params: "MockModal2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    def solve(
        self, geometry: Domain2D | Regions2D, params: "MockModal2D.Params", ctx: SolverContext
    ) -> SolverResult:
        if params.mode > params.n_modes:
            raise ValueError(
                f"`mode` is {params.mode} but only {params.n_modes} modes are being computed; "
                "raise `n_modes` or ask for a lower mode"
            )

        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)

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

        ctx.progress(ProgressEvent(iteration=0, total=3, message="assembling"))
        centroids = points[triangles].mean(axis=1)
        modulus, poisson = sample_material(
            geometry, centroids, params.youngs_modulus, params.poisson_ratio
        )
        density = _sample_density(geometry, centroids, params.density)
        b_matrix, area = strain_displacement(points, triangles)
        d_matrix = constitutive(modulus, poisson, params.plane)

        k_local = area[:, None, None] * np.einsum("eki,ekl,elj->eij", b_matrix, d_matrix, b_matrix)
        dofs = np.repeat(triangles * 2, 2, axis=1)
        dofs[:, 1::2] += 1
        n_dof = 2 * len(points)

        # Dense rather than matrix-free, unlike its elasticity twin: an eigendecomposition
        # needs the matrix itself, which is the whole reason this adapter's resolution ceiling
        # is lower than everything else here.
        stiffness = np.zeros((n_dof, n_dof))
        rows = np.repeat(dofs, 6, axis=1).ravel()
        cols = np.tile(dofs, (1, 6)).ravel()
        np.add.at(stiffness, (rows, cols), k_local.ravel())
        mass = lumped_mass(triangles, area, density, n_dof)

        free = self._free_mask(geometry, params, points, ctx, n_dof)

        ctx.check_cancelled()
        ctx.progress(
            ProgressEvent(iteration=1, total=3, message=f"eigensolve ({int(free.sum())} dofs)")
        )
        frequencies, shapes, radius = natural_frequencies(stiffness, mass, free, params.n_modes)
        rigid = count_rigid_body_modes(frequencies, radius)
        elastic = frequencies[rigid:]

        ctx.check_cancelled()
        ctx.progress(ProgressEvent(iteration=2, total=3, message="writing mode shapes"))
        keep = ~hole
        chosen = shapes[params.mode - 1].reshape(-1, 2)
        grids = {"u_mag": np.zeros((ny, nx))}
        grids["u_mag"][keep] = np.hypot(chosen[:, 0], chosen[:, 1])
        vectors = np.zeros((ny, nx, 2))
        vectors[keep] = chosen

        if params.write_vtk:
            for index in range(params.n_modes):
                shape = shapes[index].reshape(-1, 2)
                field = np.zeros((ny, nx))
                field[keep] = np.hypot(shape[:, 0], shape[:, 1])
                write_vtk_structured_points(
                    ctx.artifact(f"mode_{index + 1}.vtk", mode=index + 1), x, y, {"u_mag": field}
                )

        ctx.progress(ProgressEvent(iteration=3, total=3, message="done"))
        return SolverResult(
            kind="mesh2d",
            stats={
                "cells": float(len(triangles)),
                "dofs": float(n_dof),
                "free_dofs": float(int(free.sum())),
                "modes": float(params.n_modes),
            },
            metrics={
                "rigid_body_modes": float(rigid),
                # Absent rather than zero when every computed mode is a rigid-body one: the
                # caller asked for too few to reach the elastic spectrum, and reporting 0 Hz
                # as the first elastic frequency would be reporting a translation as a mode.
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
            # A direct eigendecomposition does not iterate toward a tolerance.
            converged=True,
            series=[spectrum_series(frequencies, rigid)],
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                **grid_to_mesh2d(x, y, hole, grids, {"u": vectors}),
            },
        )

    def _free_mask(self, geometry, params, points, ctx, n_dof: int) -> np.ndarray:
        """Which degrees of freedom the structure is free to move in.

        A load case wins outright over `fixed_edge`, exactly as it does for the elasticity
        pair — and unlike that pair, *nothing restrained at all* is a legitimate answer here
        rather than a singular problem to refuse.
        """
        from ..boundaries import predicate

        free = np.ones(n_dof)
        if ctx.conditions:
            coords = np.ascontiguousarray(points.T)
            for name, values in ctx.conditions.items():
                nodes = np.nonzero(np.asarray(predicate(geometry, name)(coords), dtype=bool))[0]
                if nodes.size == 0:
                    raise ValueError(
                        f"the boundary {name!r} selects no node of this mesh; raise "
                        "`resolution` or widen the selector, because the restraint on it "
                        "would apply to nothing"
                    )
                if values.get("fixed"):
                    free[2 * nodes] = 0.0
                    free[2 * nodes + 1] = 0.0
                if values.get("fixed_x"):
                    free[2 * nodes] = 0.0
                if values.get("fixed_y"):
                    free[2 * nodes + 1] = 0.0
            return free

        if params.fixed_edge != "none":
            tolerance = 1e-9 * min(
                geometry.bounds[2] - geometry.bounds[0], geometry.bounds[3] - geometry.bounds[1]
            )
            clamped = edge_nodes(points, geometry.bounds, params.fixed_edge, tolerance)
            free[2 * clamped] = 0.0
            free[2 * clamped + 1] = 0.0
        return free


def _sample_density(geometry, centroids: np.ndarray, default: float) -> np.ndarray:
    """Per-element density, from the region a centroid falls in, else from the parameter.

    Not part of `sample_material`, which the elasticity pair shares: a static solve has no use
    for mass, and adding a third return value there would have every caller of it unpack a
    number it does not need.
    """
    out = np.full(len(centroids), default)
    if isinstance(geometry, Regions2D):
        out[:] = float(geometry.background.get("density", default))
        for region in geometry.regions:  # painter's order, later regions win
            inside = polygon_mask(
                np.asarray(region.shape.points, dtype=float), centroids[:, 0], centroids[:, 1]
            )
            out[inside] = float(region.material.get("density", default))
    return out


def spectrum_series(frequencies, rigid: int) -> Series1DData:
    """The spectrum as a curve, which is the shape #69 built for exactly this question.

    Shared with the FEniCSx half rather than written twice: the pair is cross-validated on
    these numbers, and two builders of one curve is how the two would come to disagree about
    what the abscissa means.

    Two traces over one abscissa: the frequency of each mode, and a 0/1 flag marking the
    rigid-body ones. The flag is a trace rather than prose because a plot of the spectrum
    should be able to draw the distinction, and because a caller reading the curve alone can
    then find the first elastic mode without re-deriving the threshold.

    The abscissa is the **mode number**, which is the same number the mode-shape artifacts
    carry — so a caller joins a frequency to a shape by that number rather than by position
    in two lists.
    """
    modes = [float(index + 1) for index in range(len(frequencies))]
    return Series1DData(
        name="spectrum",
        description="Natural frequencies by mode number; `rigid_body` marks the zero modes.",
        x=SeriesAxis(name="mode", unit="1", values=modes),
        traces=[
            SeriesTrace(name="frequency", unit="Hz", values=[float(f) for f in frequencies]),
            SeriesTrace(
                name="rigid_body",
                unit="1",
                values=[1.0 if index < rigid else 0.0 for index in range(len(frequencies))],
            ),
        ],
    )


__all__ = [
    "MockModal2D",
    "count_rigid_body_modes",
    "lumped_mass",
    "natural_frequencies",
    "spectrum_series",
]
