"""The transient heat adapter, and what it demonstrates about #82.

The assertions that matter here are cross-checks against something that already exists,
because a transient has no closed form on this geometry:

- **the long-time limit is the steady solver's answer.** Two adapters, two discretizations
  of time — one of which has none — must agree on where the device ends up. That is the
  strongest statement available about a time-stepping scheme, and it fails on a sign error,
  a mis-scaled mass term, or a mishandled boundary condition;
- **the time scale is the lumped one.** `rho_cp V / (h A)` is exact for a body too
  conductive to hold a gradient, and aluminium in air is nearly that, so the measured
  time-to-90% must be a small multiple of it rather than an unrelated number;
- **an unconverged step is reported.** This is a regression test. The first version of the
  adapter ran a fixed 200 sweeps per step, stopped short every time, and returned a curve
  1.8x slower than the physics with nothing in the payload to say so.
"""

import json

import pytest
from geometries import rect

from fenixspoon.geometry import Region2D, Regions2D
from fenixspoon.solvers import fill_declared_metrics
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_heat import MockHeat2D
from fenixspoon.solvers.mock_transient_heat import MockTransientHeat2D

#: A chip on an aluminium base. Small enough to solve in a test, and carrying the one
#: property a steady solve never needs: `rho_cp`, which is what sets the time scale.
SINK = Regions2D(
    bounds=(-0.05, -0.02, 0.05, 0.06),
    regions=[
        Region2D(
            name="base",
            shape=rect(-0.03, 0.0, 0.03, 0.006),
            material={"k": 205.0, "rho_cp": 2.42e6},
        ),
        Region2D(
            name="chip",
            shape=rect(-0.01, -0.004, 0.01, 0.0),
            material={"k": 150.0, "q": 2.0e7, "rho_cp": 1.6e6},
        ),
    ],
    background={},
)


def run(solver_cls, **params):
    import tempfile
    from pathlib import Path

    ctx = SolverContext(progress_cb=lambda event: None, artifact_dir=Path(tempfile.mkdtemp()))
    result = solver_cls().solve(SINK, solver_cls.Params(write_vtk=False, **params), ctx)
    fill_declared_metrics(solver_cls, result)
    return result


def test_the_long_time_limit_is_the_steady_answer():
    """Run it long enough and the transient must land where the steady solver already is.

    The two share a spatial operator but nothing else: one relaxes to equilibrium directly,
    the other walks there in twenty-five backward-Euler steps. Agreement to a fraction of a
    kelvin says the mass term is scaled right and the convective boundary survived the time
    discretisation — the two things most likely to be quietly wrong.
    """
    steady = run(MockHeat2D, resolution=48)
    transient = run(MockTransientHeat2D, resolution=48, duration=4000.0, steps=25)

    assert transient.converged, transient.warnings
    assert transient.metrics["t_final_max"] == pytest.approx(steady.metrics["t_max"], abs=0.5)


def test_the_time_scale_is_the_lumped_one():
    """`time_to_90pc` must be a small multiple of `rho_cp V / (h A)`.

    Not equal to it: the sink is not a single lump — a chip on a base has an internal
    gradient, and a run that stops short of equilibrium measures 90% of a smaller rise. The
    assertion is therefore about the *order*, which is what catches a heat capacity out by a
    factor of a thousand or a time step interpreted as milliseconds.
    """
    result = run(MockTransientHeat2D, resolution=48, duration=2000.0, steps=20)
    ratio = result.metrics["time_to_90pc"] / result.metrics["time_constant"]
    assert 1.5 < ratio < 4.0, f"time to 90% is {ratio:.2f} time constants, which is not physical"


def test_the_curve_is_the_compact_answer():
    """A transient answers in `series1d`, and the answer stays small.

    This is the whole reason the adapter is useful before the protocol grows a time
    dimension: "does it reach 80 °C, and when" is answered by a curve of a few dozen points,
    not by a field per step.
    """
    result = run(MockTransientHeat2D, resolution=48, duration=2000.0, steps=20)

    assert len(result.series) == 1
    curve = result.series[0]
    assert len(curve.x.values) == 21, "one sample per step plus the initial state"
    assert [trace.name for trace in curve.traces] == ["t_max", "t_mean"]
    assert curve.x.unit == "s"

    peak = curve.traces[0].values
    assert all(later >= earlier - 1e-9 for earlier, later in zip(peak, peak[1:], strict=False)), (
        "the start-up cooled down somewhere, which this load cannot do"
    )
    compact = {"metrics": result.metrics, "series": [curve.model_dump()]}
    assert len(json.dumps(compact)) < 4096, "the compact answer stopped being compact"


def test_an_unconverged_step_is_reported_rather_than_absorbed():
    """The regression test for the bug this adapter shipped with internally.

    One sweep per step cannot solve anything, so every step hits the cap. What must not
    happen is what happened the first time: a plausible curve, no warning, `converged` true
    because the *last* step's residual happened to look small.
    """
    result = run(MockTransientHeat2D, resolution=48, duration=2000.0, steps=6, sweeps=1)

    assert result.converged is False
    assert any("sweep cap" in warning for warning in result.warnings), result.warnings


def test_the_peak_over_time_cannot_be_a_declared_reduction():
    """#82's first concrete consequence, asserted rather than described.

    A declared metric is a reduction of the payload, and the payload is one instant — so
    `t_final_max` is declared with a field and `t_peak` cannot be. If someone ever "tidies"
    the declaration by giving `t_peak` a field and a reduction, it would silently start
    reporting the final instant instead of the maximum over the run, and the two agree on
    every monotonic case anyone would test it with.
    """
    declared = {metric.name: metric for metric in MockTransientHeat2D.metrics}
    assert declared["t_final_max"].field == "T"
    assert declared["t_final_max"].over == "payload"
    assert declared["t_peak"].field is None, (
        "t_peak became a reduction of the payload, which holds one instant, not the history"
    )
    # Since #86 the distinction is declared rather than implied by the names, and the
    # constructor refuses the combination — so this is now checkable by a caller, not only
    # by whoever reads the source.
    assert declared["t_peak"].over == "run"

    result = run(MockTransientHeat2D, resolution=48, duration=2000.0, steps=20)
    assert result.metrics["t_peak"] >= result.metrics["t_final_max"] - 1e-9
