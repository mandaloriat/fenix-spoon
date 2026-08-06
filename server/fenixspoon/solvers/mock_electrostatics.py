"""Mock solver: axisymmetric electrostatics on a meridian (r, z) section.

Solves Laplace's equation for the potential with a piecewise-constant permittivity,

    div( eps grad V ) = 0 ,      E = -grad V ,      W = 1/2 * integral eps |E|^2 dV ,

on the ``axisymmetric2d`` geometry kind (#100). **The one thing that makes it
axisymmetric is a factor of r**, and it is everywhere it belongs: the radial face of a
control volume at r + dr/2 is larger than the one at r - dr/2, and the volume element is
``2*pi*r dr dz`` rather than ``dr dz``. Drop that factor and this is a plane slice — which
is precisely the mistake the geometry kind exists to make unrepresentable, since a
``regions2d`` payload with bounds starting at zero *looks* the same and means something
else.

Because the volume element is the revolved one, the energy is the energy of the whole body
of revolution and the capacitance comes back in **farads**, not farads per metre.

Two ways to say where a potential is held, and both are needed by the cases this was
written for:

- **A region with a ``voltage`` material key** is an electrode: a metal body in the section,
  held at that potential throughout. This is how the capacitive sensor is written — an
  annular electrode and the mirror shell behind the gap.
- **A named boundary with a ``voltage`` condition** (#85) is a face of the modelled window
  held at a potential — the inner and outer conductors of a coaxial section, where the
  metal is outside the part being modelled.

Everywhere else the boundary is **natural**: zero flux, which is exact on the axis of
revolution and on a symmetry plane, and a truncation approximation on the rest (see the
``truncated_domain`` assumption).

Material keys read from the geometry (everything else ignored):

- ``eps_r`` — relative permittivity (default 1.0)
- ``voltage`` — potential in volts; its presence is what marks a region as an electrode

A development stand-in that runs anywhere NumPy does: the physics is textbook, the
discretization first-order finite volume on a uniform grid, and a staircased electrode edge
is the accuracy limit. The FEniCSx adapter of the same name meshes the outline instead.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..boundaries import predicate
from ..geometry import Axisymmetric2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import (
    ELECTROSTATICS_ASSUMPTIONS,
    ELECTROSTATICS_CONDITIONS,
    ELECTROSTATICS_CONVERGENCE,
    ELECTROSTATICS_METRICS,
    VTK_ARTIFACT,
)
from .mock_laplace import _grid_shape, grid_to_mesh2d, polygon_mask, write_vtk_structured_points
from .mock_magnetostatics import _harmonic_mean, rasterize_regions
from .registry import register

#: Vacuum permittivity, F/m. Here rather than imported from a constants module for the same
#: reason ``MU0`` sits in the magnetostatics mock: one number, used by both halves of one
#: adapter pair, and a dependency for it would be a dependency for nothing else.
EPS0 = 8.8541878128e-12


def probe_points(geometry: Axisymmetric2D, rr: np.ndarray, dr: float) -> np.ndarray:
    """The radii region membership is tested at: the grid's own, nudged off the axis.

    A region bounded by r = 0 — a solid shaft, the commonest axisymmetric shape there is —
    has its outline lying exactly on the first grid column, and an even-odd test against an
    outline that passes through the query point is a coin flip. It comes up "outside" here,
    which would leave a column of background running up the middle of the metal.

    So the first column is tested a millionth of a cell to the right of the axis. That cannot
    change the answer anywhere else, and it is the rasteriser's problem alone: the *radii*
    used for the face areas and the volume element stay exactly what the grid says, because
    those are physics rather than point location.
    """
    if not geometry.reaches_axis:
        return rr
    probe = rr.copy()
    probe[:, 0] += 1e-6 * dr
    return probe


def fixed_potentials(
    geometry: Axisymmetric2D,
    conditions: dict[str, dict[str, float]],
    rr: np.ndarray,
    zz: np.ndarray,
    dr: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Which grid points are held at a potential, and at what.

    Regions first, in painter's order, so a nested electrode wins over the one it sits in —
    the same precedence the schema documents for materials. Boundary conditions afterwards,
    because a condition names a face of the window and a region names a body inside it; where
    both reach the same point the caller has said the face is metal, and the face is the
    later statement.

    Regions are located with :func:`probe_points`; a boundary predicate is given the grid's
    real coordinates, because `near` at r = 0 means the axis and nudging it off would be
    resolving a selector against a geometry nobody sent.
    """
    values = np.zeros(rr.shape)
    fixed = np.zeros(rr.shape, dtype=bool)
    probe = probe_points(geometry, rr, dr)

    for region in geometry.regions:
        if "voltage" not in region.material:
            continue
        mask = polygon_mask(np.asarray(region.shape.points, dtype=float), probe, zz)
        values[mask] = float(region.material["voltage"])
        fixed |= mask

    coords = np.stack([rr.ravel(), zz.ravel()])
    for name, applied in conditions.items():
        if "voltage" not in applied:
            continue
        mask = np.asarray(predicate(geometry, name)(coords)).reshape(rr.shape)
        values[mask] = float(applied["voltage"])
        fixed |= mask

    return fixed, values


def _shift(values: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Neighbour values. Off-grid neighbours wrap, and are multiplied by a zero coefficient."""
    return np.roll(np.roll(values, rows, axis=0), cols, axis=1)


def face_coefficients(
    eps: np.ndarray, rr: np.ndarray, dr: float, dz: float
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """The r-weighted finite-volume coefficients, and the diagonal they sum into.

    Each entry is ``eps_face * area_face / spacing``, with the areas of a cylindrical control
    volume: the radial faces at ``r +/- dr/2`` carry that radius, and the two axial faces
    carry ``r`` itself. Permittivity at a face is the harmonic mean of the two cells meeting
    there — the standard treatment for a discontinuous coefficient, and the reason a
    dielectric interface lands where the geometry puts it instead of being smeared.

    **The axis falls out rather than being special-cased.** At r = 0 the inner radial face
    has zero area and the two axial faces have zero radius, so the only surviving coupling is
    outward: an axis node takes the value of its neighbour, which is the discrete form of
    ``dV/dr = 0``. Nothing here has to ask whether the section reaches the axis.

    Off-grid neighbours get a zero coefficient, which is the natural (zero-flux) condition
    everywhere no potential is imposed.
    """
    inner = np.maximum(rr - 0.5 * dr, 0.0)
    outer = rr + 0.5 * dr
    coefficients = {
        "e": _harmonic_mean(eps, _shift(eps, 0, -1)) * outer / dr**2,
        "w": _harmonic_mean(eps, _shift(eps, 0, 1)) * inner / dr**2,
        "n": _harmonic_mean(eps, _shift(eps, -1, 0)) * rr / dz**2,
        "s": _harmonic_mean(eps, _shift(eps, 1, 0)) * rr / dz**2,
    }
    coefficients["e"][:, -1] = 0.0
    coefficients["w"][:, 0] = 0.0
    coefficients["n"][-1, :] = 0.0
    coefficients["s"][0, :] = 0.0
    diagonal = sum(coefficients.values())
    return coefficients, diagonal


def field_energy(
    potential: np.ndarray, eps: np.ndarray, r: np.ndarray, dr: float, dz: float
) -> float:
    """``W = 1/2 * integral eps |grad V|^2 * 2*pi*r dr dz``, summed over cells.

    Per cell rather than per node, and that is not a detail: a node-centred sum counts the
    boundary strip at full weight, and for a section whose electrodes reach towards the
    truncation that is a percent or two of the capacitance. Each cell takes the mean gradient
    of its four corners, the mean permittivity, and the radius of its centre — which is also
    where the ``r`` weight enters the answer rather than merely the operator.
    """
    d_dr = 0.5 * ((potential[:, 1:] - potential[:, :-1])[:-1, :]
                  + (potential[:, 1:] - potential[:, :-1])[1:, :]) / dr
    d_dz = 0.5 * ((potential[1:, :] - potential[:-1, :])[:, :-1]
                  + (potential[1:, :] - potential[:-1, :])[:, 1:]) / dz
    cell_eps = 0.25 * (eps[:-1, :-1] + eps[:-1, 1:] + eps[1:, :-1] + eps[1:, 1:])
    cell_r = 0.5 * (r[:-1] + r[1:])[None, :]
    return float(
        0.5 * np.sum(cell_eps * (d_dr**2 + d_dz**2) * 2.0 * np.pi * cell_r) * dr * dz
    )


@register
class MockElectrostaticsAxi2D(Solver):
    name = "mock.electrostatics_axi2d"
    title = "Axisymmetric electrostatics (mock, NumPy)"
    description = (
        "Electrostatics on a meridian (r, z) half-section of a body of revolution: potential "
        "on a Cartesian grid in (r, z) with the cylindrical r weight in every face area and "
        "in the energy integral, per-region permittivity, and electrodes given either as "
        "regions with a `voltage` material key or as named boundaries. Reports the "
        "capacitance in farads for the whole revolved body. Development stand-in that runs "
        "anywhere NumPy does."
    )
    geometry_types = ["axisymmetric2d"]
    physics = "electrostatics"
    availability = "mock"
    convergence = ELECTROSTATICS_CONVERGENCE
    metrics = ELECTROSTATICS_METRICS
    assumptions = ELECTROSTATICS_ASSUMPTIONS
    conditions = ELECTROSTATICS_CONDITIONS
    #: Pure NumPy with a fixed sweep count and no randomness anywhere: the same inputs give
    #: the same array, bit for bit, on the same numpy. Safe to cache (#47).
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="fast preview",
            description=(
                "Coarse and short. Enough to see where the field crowds; not enough to quote "
                "a capacitance, which needs the gap resolved across several cells."
            ),
            params={"resolution": 96, "iterations": 1500, "write_vtk": False},
        ),
        CapabilityExample(
            title="resolved",
            description=(
                "What a capacitance is read from. The resolution is the one that matters "
                "here: a fringe field is a feature of the electrode *edge*, so a grid that "
                "staircases the edge coarsely under-reports exactly the part a parallel-plate "
                "estimate already misses."
            ),
            params={"resolution": 256, "iterations": 12000},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=160, ge=16, le=512, description="Grid points along the longer domain edge"
        )
        iterations: int = Field(
            default=6000, ge=10, le=40000, description="Maximum relaxation sweeps"
        )
        relaxation: float = Field(
            default=1.9,
            gt=0.0,
            lt=2.0,
            description=(
                "Over-relaxation factor for the red-black sweep. 1.0 is plain Gauss-Seidel; "
                "values near 2 converge fastest but can oscillate."
            ),
        )
        report_every: int = Field(default=100, ge=1, description="Sweeps between progress events")
        output: Literal["grid2d", "mesh2d"] = Field(
            default="grid2d", description="Result kind to return"
        )
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(
        cls, geometry: Axisymmetric2D, params: "MockElectrostaticsAxi2D.Params"
    ) -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx

    def solve(
        self,
        geometry: Axisymmetric2D,
        params: "MockElectrostaticsAxi2D.Params",
        ctx: SolverContext,
    ) -> SolverResult:
        rmin, zmin, rmax, zmax = geometry.bounds
        nz, nr = _grid_shape(geometry.bounds, params.resolution)

        r = np.linspace(rmin, rmax, nr)
        z = np.linspace(zmin, zmax, nz)
        rr, zz = np.meshgrid(r, z)
        dr = float(r[1] - r[0])
        dz = float(z[1] - z[0])

        eps_r = np.maximum(
            rasterize_regions(geometry, probe_points(geometry, rr, dr), zz, "eps_r", 1.0), 1e-12
        )
        eps = EPS0 * eps_r

        fixed, potential = fixed_potentials(geometry, ctx.conditions, rr, zz, dr)
        if not fixed.any():
            # Refused rather than solved. With nothing pinned the problem is singular and
            # every constant is a solution, so a relaxation would return whatever it started
            # from — a field of zeros, a capacitance of zero, and no sign anything was wrong.
            raise ValueError(
                "no electrode: give a region a `voltage` material key, or name a boundary and "
                "put a `voltage` condition on it. A potential has to be pinned somewhere — "
                "with nothing held, every constant field solves this problem equally well"
            )

        levels = np.unique(potential[fixed])
        span = float(levels.max() - levels.min())
        # Start from the mean of what is held rather than from zero: the slow mode of a
        # relaxation is the whole domain drifting to its level, and that is the one mode a
        # constant initial guess can remove for free.
        potential = np.where(fixed, potential, float(levels.mean()))

        coefficients, diagonal = face_coefficients(eps, rr, dr, dz)
        safe_diagonal = np.maximum(diagonal, 1e-300)

        # Red-black Gauss-Seidel with over-relaxation, as in `mock.heat2d` and for the same
        # reason: plain Jacobi needs about N^2 sweeps to move information across N cells, and
        # a fringe field is precisely a long-range rearrangement.
        rows, cols = np.indices((nz, nr))
        colours = [((rows + cols) % 2 == parity) & ~fixed for parity in (0, 1)]
        tolerance = 1e-12 * max(span, 1.0)

        residual = float("inf")
        for it in range(1, params.iterations + 1):
            ctx.check_cancelled()
            residual = 0.0
            for update in colours:
                neighbours = (
                    coefficients["e"] * _shift(potential, 0, -1)
                    + coefficients["w"] * _shift(potential, 0, 1)
                    + coefficients["n"] * _shift(potential, -1, 0)
                    + coefficients["s"] * _shift(potential, 1, 0)
                )
                change = params.relaxation * (neighbours / safe_diagonal - potential)
                residual = max(residual, float(np.max(np.abs(change[update]), initial=0.0)))
                potential = np.where(update, potential + change, potential)

            if it % params.report_every == 0 or it == params.iterations:
                ctx.progress(
                    ProgressEvent(iteration=it, total=params.iterations, residual=residual)
                )
            if residual < tolerance:
                break

        e_r = -np.gradient(potential, dr, axis=1)
        e_z = -np.gradient(potential, dz, axis=0)
        e_mag = np.hypot(e_r, e_z)

        energy = field_energy(potential, eps, r, dr, dz)
        metrics = {"energy": energy}
        warnings: list[str] = []
        if len(levels) >= 2:
            metrics["capacitance"] = 2.0 * energy / span**2
        else:
            # Declared and absent, which is the honest pair. One potential everywhere means a
            # uniform field-free solution: there is no second electrode for a capacitance to
            # be between, and reporting the zero that 2W/V^2 evaluates to would answer a
            # question the caller did not ask.
            warnings.append(
                "only one potential is held in this model, so there is no capacitance to "
                "report: give a second region or boundary a different `voltage`"
            )

        converged = residual < tolerance
        if not converged:
            warnings.append(
                f"stopped at the iteration cap ({params.iterations}) with residual "
                f"{residual:.3g}; the capacitance may not be converged"
            )

        fields = {"V": potential, "E": e_mag, "eps_r": eps_r}
        vector_fields = {"E_vec": np.stack([e_r, e_z], axis=-1)}
        stats = {
            "cells": float(nr * nz),
            "iterations": float(it),
            "fixed_cells": float(fixed.sum()),
        }
        diagnostics = {
            "metrics": metrics,
            "converged": converged,
            "residual": residual,
            "iterations": it,
            "warnings": warnings,
        }
        if params.write_vtk:
            write_vtk_structured_points(ctx.artifact("solution.vtk"), r, z, fields)

        if params.output == "mesh2d":
            return SolverResult(
                kind="mesh2d",
                stats=stats,
                **diagnostics,
                data={
                    "bounds": [rmin, zmin, rmax, zmax],
                    **grid_to_mesh2d(r, z, np.zeros((nz, nr), dtype=bool), fields, vector_fields),
                },
            )

        return SolverResult(
            kind="grid2d",
            stats=stats,
            **diagnostics,
            data={
                "bounds": [rmin, zmin, rmax, zmax],
                "shape": [nz, nr],
                "fields": {name: values.ravel().tolist() for name, values in fields.items()},
                "vector_fields": {
                    name: values.reshape(-1, 2).tolist()
                    for name, values in vector_fields.items()
                },
                # Every cell is solved: a conductor is part of the model rather than a hole
                # in it, so nothing is masked out.
                "mask": np.zeros(nz * nr, dtype=np.uint8).tolist(),
            },
        )
