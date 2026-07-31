"""Mock solver: the thermal *start-up* of a convectively cooled solid (issue #82).

The steady heat adapters answer "how hot does it get". This one answers "when", which is
the question their `steady_state` assumption explicitly puts out of reach — its `excludes`
list names `thermal_time_constant`, `transient_temperature` and `duty_cycle_rise`, and this
capability exists to supply exactly those.

    rho_cp dT/dt = div( k grad T ) + q ,   -k dT/dn = h (T - T_ambient)

The spatial operator is `mock.heat2d`'s, imported rather than restated: the same finite
volumes, the same harmonic-mean interface conductivity, the same convective faces. Only the
time derivative is new, discretised with **backward Euler** — unconditionally stable, so the
step size is chosen for the accuracy the curve needs rather than to avoid divergence.

## What it can and cannot return, which is the point of #82

The protocol has no time dimension, so this adapter answers in the shapes that *do* exist:

- **the curves are the answer.** `T_max(t)` and `T_mean(t)` come back as a `series1d`
  (#69) beside the field. Most transient questions — does it reach 80 °C, and after how
  long — are answered there without a single field crossing the wire;
- **the field is the final instant**, and nothing else. That is a real limitation and it
  shows up in a place worth noticing: a declared metric is a reduction of *the payload*, so
  `t_final_max` can be declared and the peak over all time cannot. `t_peak` is computed
  here instead, and the two differ whenever the solve overshoots;
- **the field history has nowhere to go.** Not an artifact either: a legacy-VTK file holds
  one instant. Issue #82 predicted this gap in the abstract; this adapter is the concrete
  case to design the answer against.

Material keys read from the geometry (everything else ignored):

- ``k`` — thermal conductivity in W/(m·K) (default 1.0)
- ``q`` — volumetric heat source in W/m³ (default 0.0)
- ``rho_cp`` — volumetric heat capacity in J/(m³·K) (default: the `rho_cp` parameter)
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..geometry import Regions2D
from ..series import Series1DData, SeriesAxis, SeriesTrace
from .base import CapabilityExample, ProgressEvent, Solver, SolverContext, SolverResult
from .declarations import TRANSIENT_HEAT_ASSUMPTIONS, TRANSIENT_HEAT_METRICS, VTK_ARTIFACT
from .mock_heat import (
    _ambient_flux,
    _assemble,
    _balanced_level,
    _neighbour_is_solid,
    _shift,
    rasterize_regions,
    solid_mask,
)
from .mock_laplace import _grid_shape, grid_to_mesh2d, write_vtk_structured_points
from .registry import register

#: Aluminium, which is what the heat-sink example is made of: 2700 kg/m^3 x 897 J/(kg K).
ALUMINIUM_RHO_CP = 2.42e6


def lumped_time_constant(
    solid: np.ndarray, capacity: np.ndarray, h: float, dx: float, dy: float
) -> float:
    """`rho_cp V / (h A)`: the time constant of a solid too conductive to have gradients.

    Exact in the limit where conduction is infinitely fast compared with convection, which
    for aluminium in air is very nearly true — the Biot number of the reference heat sink is
    of order 0.001. Reported as a diagnostic and used by the tests as the closed form this
    solver has to reproduce.
    """
    stored = float(np.sum(np.where(solid, capacity, 0.0)) * dx * dy)
    conductance = 0.0
    for rows, cols, face in ((0, -1, dy), (0, 1, dy), (-1, 0, dx), (1, 0, dx)):
        exposed = solid & ~_neighbour_is_solid(solid, rows, cols)
        conductance += float(exposed.sum()) * h * face
    return stored / conductance if conductance > 0.0 else float("inf")


def crossing_time(times: np.ndarray, values: np.ndarray, level: float) -> float | None:
    """The first time a rising curve reaches `level`, linearly interpolated between samples.

    Interpolated rather than snapped to a sample, because the answer would otherwise be a
    property of the step size — a caller halving `steps` would see the reported time-to-90%
    jump, and conclude the physics had changed.
    """
    above = np.nonzero(values >= level)[0]
    if len(above) == 0:
        return None
    index = int(above[0])
    if index == 0:
        return float(times[0])
    previous, current = values[index - 1], values[index]
    if current == previous:
        return float(times[index])
    fraction = (level - previous) / (current - previous)
    return float(times[index - 1] + fraction * (times[index] - times[index - 1]))


@register
class MockTransientHeat2D(Solver):
    name = "mock.transient_heat2d"
    title = "Transient heat conduction (mock, NumPy)"
    description = (
        "Thermal start-up of a convectively cooled solid: how the temperature climbs from "
        "an initial state towards equilibrium, and how long that takes. Backward Euler in "
        "time over `mock.heat2d`'s finite volumes. The compact answer is a pair of curves; "
        "the field is the final instant."
    )
    geometry_types = ["regions2d"]
    #: Deliberately not `heat-conduction`. The steady adapters answer how hot, this one
    #: answers when, and the two do not share a metric vocabulary — a caller filtering on
    #: physics is asking which question a capability can answer, not which equation it
    #: discretises.
    physics = "heat-conduction-transient"
    availability = "mock"
    metrics = TRANSIENT_HEAT_METRICS
    assumptions = TRANSIENT_HEAT_ASSUMPTIONS
    #: Fixed step count, fixed sweep count, no randomness: same inputs, same arrays (#47).
    deterministic = True
    artifacts = [VTK_ARTIFACT]
    examples = [
        CapabilityExample(
            title="start-up from ambient",
            description=(
                "The heat sink switched on cold. Run for a few time constants and read "
                "`time_to_90pc` — that is the number the steady solver cannot give you."
            ),
            params={"resolution": 96, "duration": 600.0, "steps": 60},
        ),
        CapabilityExample(
            title="fast preview",
            description="Coarse in space and time: the shape of the curve, not its value.",
            params={"resolution": 48, "duration": 600.0, "steps": 20, "write_vtk": False},
        ),
    ]

    class Params(BaseModel):
        resolution: int = Field(
            default=96, ge=16, le=384, description="Grid points along the longer domain edge"
        )
        duration: float = Field(
            default=600.0, gt=0.0, description="Simulated time in seconds, from t = 0."
        )
        steps: int = Field(
            default=60, ge=2, le=2000,
            description=(
                "Backward Euler steps. Unconditionally stable, so this sets accuracy and "
                "how finely the returned curves are sampled, not whether the solve survives."
            ),
        )
        sweeps: int = Field(
            default=4000, ge=1, le=60000,
            description=(
                "Cap on relaxation sweeps per time step. A step normally stops on its "
                "tolerance long before this; the cap exists so a badly conditioned geometry "
                "fails loudly rather than running forever. Hitting it is reported."
            ),
        )
        h: float = Field(
            default=25.0, gt=0.0, le=10000.0,
            description="Convection coefficient in W/(m²·K) at solid surfaces.",
        )
        t_ambient: float = Field(
            default=20.0, ge=-273.15, description="Ambient fluid temperature in °C"
        )
        t_initial: float | None = Field(
            default=None,
            description=(
                "Uniform initial temperature in °C. Defaults to ambient — the device "
                "switched on cold, which is the case `time_to_90pc` describes."
            ),
        )
        rho_cp: float = Field(
            default=ALUMINIUM_RHO_CP, gt=0.0,
            description=(
                "Volumetric heat capacity in J/(m³·K), used where a region declares none. "
                "This is the only property a steady solve does not need, and the one that "
                "sets the time scale."
            ),
        )
        relaxation: float = Field(default=1.9, gt=0.0, lt=2.0)
        output: Literal["grid2d", "mesh2d"] = "grid2d"
        write_vtk: bool = Field(
            default=True, description="Attach the final instant as a legacy-VTK artifact"
        )

    @classmethod
    def estimate_cells(cls, geometry: Regions2D, params: "MockTransientHeat2D.Params") -> int:
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        # Cells times steps, because that is what the solve costs. `estimate_cells` names a
        # count of cells and a transient spends that count once per step — the admission
        # budget is about work, and a transient that reported only its mesh size would slip
        # a hundredfold past it. Noted in #82 as a thing the estimate has to learn.
        return ny * nx * params.steps

    def solve(
        self, geometry: Regions2D, params: "MockTransientHeat2D.Params", ctx: SolverContext
    ) -> SolverResult:
        xmin, ymin, xmax, ymax = geometry.bounds
        ny, nx = _grid_shape(geometry.bounds, params.resolution)
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)
        dx, dy = float(x[1] - x[0]), float(y[1] - y[0])

        solid = solid_mask(geometry, xx, yy)
        if not solid.any():
            raise ValueError("no solid cells: every region fell between grid points")
        conductivity = np.maximum(rasterize_regions(geometry, xx, yy, "k", 1.0), 1e-12)
        source = rasterize_regions(geometry, xx, yy, "q", 0.0)
        capacity = np.maximum(
            rasterize_regions(geometry, xx, yy, "rho_cp", params.rho_cp), 1e-12
        )

        coefficients, diagonal = _assemble(solid, conductivity, params.h, dx, dy)
        if not np.all(diagonal[solid] > 0):
            raise ValueError("some solid cells have no conductive or convective coupling")

        step = params.duration / params.steps
        # Backward Euler: the mass term joins the diagonal and carries the previous step on
        # the right. Unconditionally stable, which is why `steps` is an accuracy knob.
        inertia = capacity / step
        steady_rhs = np.where(solid, source + _ambient_flux(
            solid, params.h, params.t_ambient, dx, dy
        ), 0.0)
        safe_diagonal = np.maximum(diagonal + inertia, 1e-30)

        initial = params.t_ambient if params.t_initial is None else params.t_initial
        temperature = np.full((ny, nx), float(initial))
        rows, cols = np.indices((ny, nx))
        colours = [(rows + cols) % 2 == parity for parity in (0, 1)]

        times = [0.0]
        peak = [float(temperature[solid].max())]
        mean = [float(temperature[solid].mean())]
        residual = 0.0
        capped: list[int] = []

        # Each backward-Euler step is solved to a tolerance *relative to the temperature
        # scale*, not to an absolute one. An absolute 1e-10 on a 400 K field asks for
        # sixteen significant figures and never arrives; the first version of this solver
        # used a fixed 200 sweeps instead and stopped short every step, which made the
        # reported curve 1.8x slower than the physics — a wrong answer with no symptom.
        # That is why hitting the cap is now recorded and reported rather than absorbed.
        scale = max(abs(_balanced_level(
            solid, source, params.h, params.t_ambient, dx, dy
        ) - float(initial)), 1.0)
        # 1e-7 of the rise: about 40 microkelvin on a 400 K start-up. Tight enough that
        # the curve is the physics and not the relaxation, loose enough that a step stops in
        # tens of sweeps rather than thousands.
        tolerance = 1e-7 * scale

        before = temperature
        for index in range(1, params.steps + 1):
            ctx.check_cancelled()
            previous = temperature
            rhs = steady_rhs + np.where(solid, inertia * previous, 0.0)
            # Start from a linear extrapolation of the last two states rather than from the
            # last one: the solution is moving in a consistent direction, and handing the
            # relaxation the place it is heading roughly halves the sweeps it needs.
            if len(times) >= 2:
                temperature = np.where(solid, temperature + (temperature - before), temperature)
            before = previous
            reached_tolerance = False
            for _sweep in range(params.sweeps):
                residual = 0.0
                for colour in colours:
                    neighbours = (
                        coefficients["e"] * _shift(temperature, 0, -1)
                        + coefficients["w"] * _shift(temperature, 0, 1)
                        + coefficients["n"] * _shift(temperature, -1, 0)
                        + coefficients["s"] * _shift(temperature, 1, 0)
                    )
                    target = (neighbours + rhs) / safe_diagonal
                    update = colour & solid
                    change = params.relaxation * (target - temperature)
                    residual = max(residual, float(np.max(np.abs(change[update]), initial=0.0)))
                    temperature = np.where(update, temperature + change, temperature)
                if residual < tolerance:
                    reached_tolerance = True
                    break
            if not reached_tolerance:
                capped.append(index)

            times.append(index * step)
            peak.append(float(temperature[solid].max()))
            mean.append(float(temperature[solid].mean()))
            ctx.progress(
                ProgressEvent(
                    iteration=index,
                    total=params.steps,
                    residual=residual,
                    message=f"t = {index * step:.3g} s",
                )
            )

        temperature = np.where(solid, temperature, params.t_ambient)
        flux = np.hypot(
            -conductivity * np.gradient(temperature, dx, axis=1),
            -conductivity * np.gradient(temperature, dy, axis=0),
        ) * solid

        history = np.asarray(peak)
        rise = history[-1] - initial
        # 90% of the rise *achieved*, not of the rise to equilibrium: a run stopped early
        # has not seen equilibrium, and reporting a fraction of a number the solve never
        # reached would be an extrapolation dressed as a measurement. `settled` says which
        # of the two this is.
        settled = abs(history[-1] - history[-2]) < 0.01 * max(abs(rise), 1e-12)
        reached = crossing_time(np.asarray(times), history, initial + 0.9 * rise)

        curves = Series1DData(
            name="temperature_history",
            description=(
                "Peak and mean solid temperature against time. The compact answer to a "
                "transient: the field below is only the final instant."
            ),
            x=SeriesAxis(name="time", unit="s", values=times),
            traces=[
                SeriesTrace(name="t_max", unit="degC", values=peak),
                SeriesTrace(name="t_mean", unit="degC", values=mean),
            ],
        )

        fields = {"T": temperature, "flux": flux, "k": np.where(solid, conductivity, 0.0)}
        if params.write_vtk:
            write_vtk_structured_points(ctx.artifact("solution.vtk"), x, y, fields)

        metrics = {
            "t_rise": float(temperature[solid].max() - params.t_ambient),
            # Over the whole history, which is why it cannot be a declared reduction: the
            # payload holds one instant and a declaration can only speak about the payload.
            "t_peak": float(history.max()),
            "time_constant": lumped_time_constant(solid, capacity, params.h, dx, dy),
        }
        if reached is not None:
            metrics["time_to_90pc"] = reached

        warnings = []
        if capped:
            warnings.append(
                f"{len(capped)} of {params.steps} time steps hit the sweep cap "
                f"({params.sweeps}) without reaching tolerance {tolerance:.3g} K, first at "
                f"step {capped[0]}; the curve is slower than the physics, not the physics"
            )
        if not settled:
            warnings.append(
                f"still rising at t = {params.duration:g} s: the run stopped before "
                "equilibrium, so `time_to_90pc` is 90% of the rise achieved so far and not "
                "of the final one"
            )

        payload = {
            "kind": params.output,
            "stats": {
                "cells": float(nx * ny),
                "solid_cells": float(solid.sum()),
                "steps": float(params.steps),
                "dt": step,
            },
            "metrics": metrics,
            "series": [curves],
            # Every step, not just the last: one unconverged step early on bends the whole
            # curve, and reporting only the final residual would hide it behind a step that
            # happened to be easy.
            "converged": not capped,
            "residual": residual,
            "warnings": warnings,
        }
        if params.output == "mesh2d":
            return SolverResult(
                **payload,
                data={
                    "bounds": [xmin, ymin, xmax, ymax],
                    **grid_to_mesh2d(x, y, ~solid, fields),
                },
            )
        return SolverResult(
            **payload,
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [ny, nx],
                "fields": {name: values.ravel().tolist() for name, values in fields.items()},
                "mask": (~solid).astype(np.uint8).ravel().tolist(),
            },
        )
