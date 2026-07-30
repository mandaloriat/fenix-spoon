"""One-dimensional results: surface distributions, sweeps, convergence histories (#69).

Protocol 1.2 had two result kinds and both were *fields over a 2-D domain*. A great many
engineering answers are not that shape — they are an ordered list of (x, y) pairs with a name
and a unit:

| The question | The curve |
|---|---|
| What is this profile doing? | `C_p(x/c)` along the upper and lower surface |
| How does it respond? | a parameter sweep — `C_L(alpha)`, `T_max(fin count)` |
| Do I believe it? | a convergence history — a metric against mesh size |
| Where does it resonate? | a frequency sweep, or a list of modal frequencies |

Before this module the protocol-legal homes for such a curve were all wrong in a different
way. `stats` is `dict[str, float]`, so a 200-point curve became 200 keys — and `stats` is
what the solve *cost*, which #46 had just finished separating from what it answered. A
`grid2d` with `ny = 1` lied about the topology, dragged a `mask` and `bounds` along, and
rendered as a one-pixel-tall picture. An artifact worked, and put the *compact* half of the
answer behind a second round trip and outside every typed model, so each application invented
its own JSON for the same thing.

Four decisions worth stating, because each had a defensible alternative:

**Units travel on the wire, unlike the 2-D kinds.** A curve is plotted with axis labels and
the client has no other way to know what the axis is; a field is coloured, and the viewer
takes its colourbar caption from the page. So :class:`SeriesAxis` and :class:`SeriesTrace`
both carry a `unit`, `"1"` when dimensionless.

**A shared abscissa, with a per-trace override.** Shared covers the common cases — a sweep,
and a surface distribution resampled to a common `x/c`. Per-trace is needed the moment two
traces are genuinely sampled differently, which is the upper and lower surface of an airfoil
if nobody resampled them. Making `x` shared *and* overridable costs one optional field and
avoids repeating an identical abscissa once per trace.

**Complex values are two real traces, not a complex type.** Harmonic problems have a
magnitude and a phase, and that is what gets plotted — two curves with different units on
different axes. A complex scalar type would need every consumer to decide which of the two it
was drawing anyway, and JSON has no complex number.

**Length is bounded, at three levels.** :data:`MAX_SERIES_POINTS` per trace,
:data:`MAX_SERIES_TRACES` per collection, and :data:`MAX_SERIES_TOTAL_POINTS` across a whole
result — the last because the first two multiply out to far more than either intends. Together
they are what stops a "curve" being used to smuggle a full field into a payload that promises
not to carry one. A few thousand points is already more than a plot can resolve.
"""

from pydantic import BaseModel, Field, model_validator

#: Hard ceiling on the number of points in one trace, and on one axis.
#:
#: What this is for: a curve must not be able to *become* the field arrays under a different
#: key. A 512x341 grid is 174,592 numbers, and a "series" that could carry them would undo the
#: separation #46 spent a protocol version establishing. 4096 is generous for anything
#: plottable — a 4K display has fewer horizontal pixels — and far short of a field.
#:
#: It is **not** what keeps the compact default small. That is the `series` level not being in
#: :data:`~fenixspoon.core.results.DEFAULT_LEVELS`: a curve is bounded, but it is still a
#: numeric array, and #46's acceptance criterion for the default answer is that it carries
#: none. Asking for the curve costs a level name on the same request, not a second one.
MAX_SERIES_POINTS = 4096

#: Ceiling on how many traces one :class:`Series1DData` may hold, and on how many curve sets
#: a single result may carry. Bounded for the same reason as the point count: thirty-two
#: traces of four thousand points is a field, however small each one is on its own.
MAX_SERIES_TRACES = 32

#: Ceiling on the total points across every trace of every series on one result.
#:
#: The per-trace and per-collection limits multiply out to far more than either intends, which
#: is the gap an adapter would fall through — thirty-two legal traces of four thousand legal
#: points is a field. This is the number that actually closes it, and it is an order of
#: magnitude below the field it must not become.
MAX_SERIES_TOTAL_POINTS = 8192


class SeriesAxis(BaseModel):
    """The abscissa of a curve: what is on the x axis, in what unit, at which samples."""

    name: str = Field(description="Axis label, e.g. `x/c`, `alpha`, `cells`.")
    unit: str = Field(
        description='Unit as a display string — `"deg"`, `"m"`, `"Hz"`; `"1"` if dimensionless.'
    )
    values: list[float] = Field(
        description=(
            "Sample positions, in the order they should be drawn. Monotonic for a sweep or a "
            "convergence history; **not** for a surface distribution, which traverses a "
            "closed contour and revisits x."
        )
    )

    @model_validator(mode="after")
    def _check(self) -> "SeriesAxis":
        if not self.values:
            raise ValueError(f"axis {self.name!r} has no values")
        if len(self.values) > MAX_SERIES_POINTS:
            raise ValueError(
                f"axis {self.name!r} has {len(self.values)} points, over the "
                f"{MAX_SERIES_POINTS} a series may carry; a curve that long is a field"
            )
        return self


class SeriesTrace(BaseModel):
    """One curve. `values` line up with the shared abscissa unless it brings its own `x`."""

    name: str = Field(description="Identifier for this curve, e.g. `cp_upper`.")
    unit: str = Field(description='Unit as a display string; `"1"` if dimensionless.')
    values: list[float] = Field(description="One ordinate per abscissa sample, in that order.")
    x: SeriesAxis | None = Field(
        default=None,
        description=(
            "This trace's own abscissa, when it is not sampled like its siblings. Null means "
            "it uses the series-level `x`, which is the common case."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> "SeriesTrace":
        if len(self.values) > MAX_SERIES_POINTS:
            raise ValueError(
                f"trace {self.name!r} has {len(self.values)} points, over the "
                f"{MAX_SERIES_POINTS} a series may carry"
            )
        if self.x is not None and len(self.x.values) != len(self.values):
            raise ValueError(
                f"trace {self.name!r} has {len(self.values)} values for "
                f"{len(self.x.values)} abscissa samples"
            )
        return self


class Series1DData(BaseModel):
    """A named set of curves sharing an abscissa: the `series1d` payload, or one entry of a
    field result's `series` list.

    `name` is required even for a lone curve set, because the moment a result carries two of
    them (a surface distribution *and* a convergence history) a consumer has to tell them
    apart, and positional identity is not something a caller should have to rely on.
    """

    name: str = Field(description="Identifier for this curve set, e.g. `surface_cp`.")
    description: str = Field(
        default="",
        description="One line on what the curves are, for a legend or a caption.",
    )
    x: SeriesAxis | None = Field(
        default=None,
        description=(
            "Abscissa shared by every trace that does not carry its own. May be null only "
            "when all of them do."
        ),
    )
    traces: list[SeriesTrace] = Field(description="The curves, at least one.")

    @model_validator(mode="after")
    def _check(self) -> "Series1DData":
        if not self.traces:
            raise ValueError(f"series {self.name!r} carries no traces")
        if len(self.traces) > MAX_SERIES_TRACES:
            raise ValueError(
                f"series {self.name!r} has {len(self.traces)} traces, over the "
                f"{MAX_SERIES_TRACES} a series may carry"
            )
        names = [trace.name for trace in self.traces]
        if len(names) != len(set(names)):
            raise ValueError(f"series {self.name!r} names a trace twice: {names}")
        for trace in self.traces:
            if trace.x is not None:
                continue
            if self.x is None:
                raise ValueError(
                    f"series {self.name!r} trace {trace.name!r} has no abscissa: the series "
                    f"declares no shared `x` and the trace declares none of its own"
                )
            if len(trace.values) != len(self.x.values):
                raise ValueError(
                    f"series {self.name!r} trace {trace.name!r} has {len(trace.values)} "
                    f"values for {len(self.x.values)} shared abscissa samples"
                )
        return self


def check_series(series: list[Series1DData]) -> None:
    """Validate a result's whole `series` list: unique names, and a total-size ceiling.

    The per-series models bound one curve set; this bounds the collection. Without it a
    result could carry any number of legal series and still be the field arrays under
    another name, which is the one thing :data:`MAX_SERIES_POINTS` exists to prevent.
    """
    names = [entry.name for entry in series]
    if len(names) != len(set(names)):
        raise ValueError(f"a result names a series twice: {names}")
    if len(series) > MAX_SERIES_TRACES:
        raise ValueError(
            f"a result carries {len(series)} series, over the {MAX_SERIES_TRACES} allowed"
        )
    points = sum(len(trace.values) for entry in series for trace in entry.traces)
    if points > MAX_SERIES_TOTAL_POINTS:
        raise ValueError(
            f"the series on this result total {points} points, over the "
            f"{MAX_SERIES_TOTAL_POINTS} allowed; a curve that large is a field, and a field "
            f"belongs in `data` or in an artifact"
        )


__all__ = [
    "MAX_SERIES_POINTS",
    "MAX_SERIES_TOTAL_POINTS",
    "MAX_SERIES_TRACES",
    "Series1DData",
    "SeriesAxis",
    "SeriesTrace",
    "check_series",
]
