"""Mock solver: steady conduction where the conductivity depends on the temperature (#107).

    -div( k(T) grad T ) = 0 ,   k(T) = k0 * (1 + beta * (T - T_ref))

The first capability in this repository whose operator depends on its own solution, and it
exists as much for that as for the physics: seven adapter pairs shipped before anything was
nonlinear, so the convergence vocabulary on a result — `converged`, `residual`, a warning —
had never been under load. `HEAT_ASSUMPTIONS` names the assumption this removes, and calls
it out as a limitation: *"poor across a phase change or for a material whose k varies by tens
of percent over the range the solve spans"*. That is the case here.

**The boundary model is two fixed faces and insulation elsewhere**, not the convective
surface the linear heat pair uses, and the reason is that it buys an exact answer to check
against. With `k(T) = k0(1 + beta*(T - T_ref))` across a slab, the Kirchhoff transform

    psi(T) = integral of k dT = k0 * [ (T - T_ref) + beta * (T - T_ref)^2 / 2 ]

turns the nonlinear equation into Laplace's, so `psi` is *linear in x* and `T(x)` is the root
of a quadratic — a closed form neither half of the pair can accidentally agree with.

Material keys read from the geometry (everything else ignored):

- ``k`` — conductivity at the reference temperature, W/(m·K) (default 1.0)
- ``beta`` — relative change per kelvin, 1/K (default 0.0, i.e. the linear problem)
- ``t_ref`` — reference temperature for both of the above, °C (default 20.0)

The outer iteration is **Picard**: freeze `k` at the current temperature, solve the linear
problem, repeat. Robust and slow, which is the honest trade for a stand-in — the FEniCSx twin
uses Newton, so the pair reaches one fixed point by two routes.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import (
    NONLINEAR_HEAT_ASSUMPTIONS,
    NONLINEAR_HEAT_METRICS,
    PICARD_CONVERGENCE,
    VTK_ARTIFACT,
)
from .mock_heat import _shift
from .mock_laplace import _grid_shape, grid_to_mesh2d, write_vtk_structured_points
from .mock_magnetostatics import _harmonic_mean, rasterize_regions
from .registry import register

#: Edges a fixed temperature may be applied to. The same closed set the elasticity and modal
#: adapters use, and typed for the reason review of #101 gave: a bare string reaches the
#: locator and raises mid-solve, where an enum is a 422 at submit with the choices listed.
Edge = Literal["xmin", "xmax", "ymin", "ymax"]

#: Below this the material would conduct backwards, which is not a physical regime and not
#: one Picard recovers from — the linear solve inside the loop would have a negative
#: diagonal. Clamped rather than raised on, because it is reached transiently by an early
#: iterate long before the converged answer goes anywhere near it.
MIN_CONDUCTIVITY = 1e-9


def conductivity_of(k0: np.ndarray, beta: np.ndarray, t_ref: np.ndarray, t: np.ndarray):
    """`k(T)`, clamped positive. One function so the metric and the operator cannot differ."""
    return np.maximum(k0 * (1.0 + beta * (t - t_ref)), MIN_CONDUCTIVITY)


def slab_temperature(x, length: float, k0: float, beta: float, t_ref: float, t1: float, t2: float):
    """The closed form this adapter is checked against, via the Kirchhoff transform.

    `psi = k0[(T - T_ref) + beta (T - T_ref)^2 / 2]` satisfies Laplace's equation, so it is
    linear between the two faces; inverting the quadratic gives `T`. At `beta = 0` it reduces
    to the straight line, which is what makes the linear case a corner of this test rather
    than a separate one.
    """

    def psi(t: float) -> float:
        return k0 * ((t - t_ref) + 0.5 * beta * (t - t_ref) ** 2)

    blend = psi(t1) + (psi(t2) - psi(t1)) * (np.asarray(x, dtype=float) / length)
    if beta == 0.0:
        return t_ref + blend / k0
    return t_ref + (-1.0 + np.sqrt(1.0 + 2.0 * beta * blend / k0)) / beta


@register
class MockNonlinearHeat2D(Solver):
    name = "mock.heat_nonlinear2d"
    title = "Temperature-dependent conduction (mock, NumPy)"
    description = (
        "Steady conduction where each region's conductivity varies linearly with "
        "temperature, k(T) = k0 (1 + beta (T - T_ref)). Two faces are held at fixed "
        "temperatures and the rest are insulated. Solved by successive substitution: "
        "freeze k, solve, repeat until the temperature stops moving. Development "
        "stand-in that runs anywhere NumPy does."
    )
    geometry_types = ["regions2d"]
    physics = "heat-conduction-nonlinear"
    availability = "mock"
    convergence = PICARD_CONVERGENCE
    metrics = NONLINEAR_HEAT_METRICS
    assumptions = NONLINEAR_HEAT_ASSUMPTIONS
    #: Fixed sweep counts, no randomness: the same inputs give the same array (#47).
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="linear corner",
            description=(
                "beta = 0 everywhere, so this is the straight-line answer. Worth running "
                "first: it is the case whose answer you already know, and the outer loop "
                "should report a single iteration."
            ),
            params={"resolution": 80, "t_hot": 300.0, "write_vtk": False},
        ),
        CapabilityExample(
            title="steel over a wide range",
            description=(
                "beta = -4e-4 /K is roughly carbon steel, whose conductivity falls as it "
                "heats. Over a 280 K span that is a 12% change in k, which moves the "
                "profile visibly away from the straight line the linear adapter would draw."
            ),
            params={"resolution": 160, "t_hot": 300.0, "outer_iterations": 60},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=120, ge=16, le=512, description="Grid points along the longer domain edge"
        )
        outer_iterations: int = Field(
            default=40, ge=1, le=500, description="Maximum Picard (freeze-k) iterations"
        )
        inner_iterations: int = Field(
            default=4000, ge=10, le=40000, description="Maximum relaxation sweeps per Picard step"
        )
        tolerance: float = Field(
            default=1e-6,
            gt=0.0,
            le=1e-1,
            description=(
                "Outer tolerance on the relative temperature change. The capability's "
                "`convergence` declaration says what happens when it is not reached."
            ),
        )
        hot_edge: Edge = Field(default="xmin", description="Edge held at `t_hot`")
        cold_edge: Edge = Field(default="xmax", description="Edge held at `t_cold`")
        t_hot: float = Field(default=200.0, description="Temperature of the hot edge, °C")
        t_cold: float = Field(default=20.0, description="Temperature of the cold edge, °C")
        relaxation: float = Field(
            default=1.9, gt=0.0, lt=2.0, description="Over-relaxation factor for the inner sweep"
        )
        output: Literal["grid2d", "mesh2d"] = Field(
            default="grid2d", description="Result kind to return"
        )
        write_vtk: bool = Field(
            default=True, description="Attach the solution as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "MockNonlinearHeat2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        return ny * nx * params.outer_iterations

    def solve(
        self, geometry: Regions2D, params: "MockNonlinearHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        if params.hot_edge == params.cold_edge:
            raise ValueError(
                f"hot_edge and cold_edge are both {params.hot_edge!r}; one face cannot be "
                "held at two temperatures, and with only one fixed face the problem has no "
                "unique solution"
            )
        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)
        dx, dy = float(x[1] - x[0]), float(y[1] - y[0])

        k0 = np.maximum(rasterize_regions(geometry, xx, yy, "k", 1.0), MIN_CONDUCTIVITY)
        beta = rasterize_regions(geometry, xx, yy, "beta", 0.0)
        t_ref = rasterize_regions(geometry, xx, yy, "t_ref", 20.0)

        fixed = np.zeros((ny, nx), dtype=bool)
        temperature = np.full((ny, nx), 0.5 * (params.t_hot + params.t_cold), dtype=float)
        for edge, value in ((params.hot_edge, params.t_hot), (params.cold_edge, params.t_cold)):
            face = _edge_mask(ny, nx, edge)
            fixed |= face
            temperature[face] = value
        free = ~fixed

        span = max(abs(params.t_hot - params.t_cold), 1.0)
        rows, cols = np.indices((ny, nx))
        colours = [(rows + cols) % 2 == parity for parity in (0, 1)]

        residual = float("inf")
        outer = 0
        history: list[float] = []
        for outer in range(1, params.outer_iterations + 1):
            ctx.check_cancelled()
            previous = temperature.copy()
            conductivity = conductivity_of(k0, beta, t_ref, temperature)
            coefficients, diagonal = _assemble_insulated(conductivity, dx, dy)
            safe_diagonal = np.maximum(diagonal, 1e-30)

            # The inner solve is the same red-black over-relaxation the linear heat adapter
            # uses; only the coefficients change between outer steps. Its own convergence is
            # not reported: what a caller is told about is the *outer* loop, because that is
            # the one whose fixed point is the answer.
            for _ in range(params.inner_iterations):
                inner_change = 0.0
                for colour in colours:
                    neighbours = (
                        coefficients["e"] * _shift(temperature, 0, -1)
                        + coefficients["w"] * _shift(temperature, 0, 1)
                        + coefficients["n"] * _shift(temperature, -1, 0)
                        + coefficients["s"] * _shift(temperature, 1, 0)
                    )
                    update = colour & free
                    change = params.relaxation * (neighbours / safe_diagonal - temperature)
                    inner_change = max(
                        inner_change, float(np.max(np.abs(change[update]), initial=0.0))
                    )
                    temperature = np.where(update, temperature + change, temperature)
                if inner_change < 1e-10 * span:
                    break

            residual = float(np.max(np.abs(temperature - previous))) / span
            history.append(residual)
            ctx.progress(
                ProgressEvent(iteration=outer, total=params.outer_iterations, residual=residual)
            )
            if residual < params.tolerance:
                break

        conductivity = conductivity_of(k0, beta, t_ref, temperature)
        flux = np.hypot(
            -conductivity * np.gradient(temperature, dx, axis=1),
            -conductivity * np.gradient(temperature, dy, axis=0),
        )
        fields = {"T": temperature, "flux": flux, "k": conductivity}
        if params.write_vtk:
            write_vtk_structured_points(ctx.artifact("solution.vtk"), x, y, fields)

        data = (
            grid_to_mesh2d(geometry.bounds, fields, np.zeros((ny, nx), dtype=int))
            if params.output == "mesh2d"
            else {
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {name: values.ravel().tolist() for name, values in fields.items()},
                # 0 marks a point that was solved; there is no excluded region here, since
                # the whole rectangle is material. (1 means "outside", as in the
                # other grid adapters — inverted, this reports no metrics at all.)
                "mask": np.zeros(ny * nx, dtype=int).tolist(),
            }
        )
        return SolverResult(
            kind=params.output,
            data=data,
            stats={"cells": float(nx * ny)},
            metrics={"k_ratio": float(conductivity.max() / conductivity.min())},
            converged=residual < params.tolerance,
            residual=residual,
            iterations=outer,
            series=[_convergence_curve(history)],
        )


def _edge_mask(ny: int, nx: int, edge: Edge) -> np.ndarray:
    mask = np.zeros((ny, nx), dtype=bool)
    {"xmin": lambda: mask.__setitem__((slice(None), 0), True),
     "xmax": lambda: mask.__setitem__((slice(None), -1), True),
     "ymin": lambda: mask.__setitem__((0, slice(None)), True),
     "ymax": lambda: mask.__setitem__((-1, slice(None)), True)}[edge]()
    return mask


def _assemble_insulated(conductivity: np.ndarray, dx: float, dy: float):
    """Face conductances for a domain insulated everywhere the temperature is not fixed.

    A face onto the outside contributes nothing — that is what a zero-flux condition *is* in
    a finite-volume assembly, so it needs no term rather than a term set to zero. The
    harmonic mean across a solid-solid face is the linear adapter's treatment, kept because
    the discontinuity between two regions is exactly as sharp here.
    """
    coefficients: dict[str, np.ndarray] = {}
    diagonal = np.zeros_like(conductivity)
    for key, (rows, cols, spacing) in {
        "e": (0, -1, dx),
        "w": (0, 1, dx),
        "n": (-1, 0, dy),
        "s": (1, 0, dy),
    }.items():
        inside = np.ones_like(conductivity, dtype=bool)
        if rows:
            inside[0 if rows < 0 else -1, :] = False
        if cols:
            inside[:, 0 if cols < 0 else -1] = False
        conduction = _harmonic_mean(conductivity, _shift(conductivity, rows, cols)) / spacing**2
        coefficients[key] = np.where(inside, conduction, 0.0)
        diagonal += coefficients[key]
    return coefficients, diagonal


def _convergence_curve(history: list[float]):
    """The outer residual per iteration, as a `series1d`.

    Returned rather than only summarised because *how* it converged is the question this
    capability's caller is most likely to have next: a Picard sequence that halves each step
    is healthy, and one that stalls says the material is too nonlinear for freezing `k`.
    """
    from ..series import Series1DData, SeriesAxis, SeriesTrace

    return Series1DData(
        name="convergence",
        description="Relative temperature change per Picard iteration.",
        x=SeriesAxis(
            name="iteration", unit="1", values=[float(i) for i in range(1, len(history) + 1)]
        ),
        traces=[SeriesTrace(name="residual", unit="1", values=[float(v) for v in history])],
    )
