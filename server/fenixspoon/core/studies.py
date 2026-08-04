"""Studies: one specification for the shape every iterative workflow shares (#48, #21).

Parameter sweeps, mesh-convergence ladders, material comparisons and load-case comparisons
are the same thing wearing different words — vary something about a design, run the jobs,
collect compact results. Written once here so that each of them is not a bespoke driver, and
so that #21's sweep/DOE API builds on this rather than introducing a second abstraction.

**Two kinds are implemented.** `mesh_convergence` (#48) is a ladder of one parameter, read for
where its metrics stop moving. `sweep` (#21) is a grid of one or more, read for what the
metrics *do* across it. The second was the reason the first was written this way, and the
promise is worth checking against the diff: adding it needed no new operation, no new
transport binding, no second job path and no change to the ladder — `study.run` and
`study.get` answer both, over HTTP's absence and JSON-RPC, the CLI, Python and MCP alike.

What differs between them is only what the question and the answer look like: one value per
rung against several parameters per point, a convergence read-out against a family of
response curves. Everything in between — resolving the design, freezing the revisions,
submitting through the same admission control, finding a variation's job by content address —
is shared and did not move.

## What a study is, and what it is not

A study **enumerates** a variation space. It does not *choose* the next point — that is an
optimizer, it is M5 ([#22](https://github.com/mandaloriat/fenix-spoon/issues/22)), and the
design draft leaves the exact boundary open. This implementation draws it at a concrete place:
**a study's job list is a pure function of its object revision.** Given `study:s-1@2` you can
say which solves it implies without running any of them. An optimizer cannot promise that —
its second point depends on the first one's answer — so the two are different things rather
than the same thing with a smarter scheduler.

That is also what keeps a study reproducible under the workspace's rules.

## Why a study may override a design's parameters when `job.submit` may not

Issue #44 refused parameter overrides on `job.submit`, and gave the reason: an override
produces a job whose inputs are not fully described by any object revision, and "what exactly
was solved" is the question the workspace exists to answer.

A study overrides parameters, and does not break that rule. The override is not caller-supplied
per job — it is `values[i]` applied to `parameter`, and both are frozen in the study revision.
So a job's parameters are a pure function of *(study revision, rung index)*, exactly as
reproducible as a design revision would be, and both are recorded in the job's `inputs`. The
alternative — writing one design revision per rung — would answer the same question while
filling a design's history with machine-generated revisions nobody authored.

## Where the answer lives

Nowhere new. A study object describes the variation space; the **jobs are the answer**; and the
relation between them is *queried* rather than stored, the same way #47 made
`design → job → result` readable backwards. There is no per-run record to keep in step with
reality, because there is no per-run record.

Resolving a rung back to its job takes two paths, and the second exists because of a real
collision with the result cache. A rung's job is normally found by its **cache key**, which is
deterministic and works even when the rung was answered by a solve somebody ran standalone
last week — which is the whole point of reusing the cache, and which no `inputs` lookup could
find, since that job's inputs never mentioned this study. When there is no key — caching off,
or an adapter that does not declare itself deterministic — the fallback is the `inputs` the
study recorded. Neither path stores anything the other could contradict.
"""

from itertools import product
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from ..series import Series1DData, SeriesAxis, SeriesTrace
from . import errors

#: Study kinds this server implements. Two: the convergence ladder #48 shipped, and the
#: parameter sweep #21 asked for — which is this machinery over a different axis, as that
#: issue's entry in the roadmap predicted and as the module docstring above promised.
KINDS = ("mesh_convergence", "sweep")

#: The most points one sweep may enumerate.
#:
#: A bound on the **answer**, not on the queue. Every point goes through `job.submit`
#: individually, so the cell budget and the quota already govern how much compute a caller
#: can spend; what they do not govern is a *grid*, which multiplies — four axes of four
#: values is 256 solves from a body that fits on one line, and the typo that adds a fifth
#: axis is invisible. Sixty-four is one axis of sixty-four values or four of four, which is
#: more than any shipped adapter is worth running from a single call at its cost per solve,
#: and it keeps the report a table a caller reads in one response rather than a paginated
#: resource — which a study deliberately is not.
#:
#: A caller who wants four hundred points wants a driver loop and should write one. The
#: study's promise is that its job list is a pure function of its revision, not that it is a
#: batch queue.
MAX_SWEEP_POINTS = 64


class _StudySpec(BaseModel):
    """What every study kind says regardless of what it varies: which design, which metrics.

    A base class rather than a repeated pair of fields, so that a third kind cannot spell
    `design` differently or forget that omitting `metrics` means *all* of them.
    """

    design: str = Field(
        description=(
            "Reference to the `design` this study varies. Pinned or not — an unpinned "
            "reference resolves to the head when the study runs and is frozen in each job's "
            "provenance, exactly as `job.submit` does."
        )
    )
    metrics: list[str] | None = Field(
        default=None,
        description=(
            "Which declared metrics to tabulate. Omit for all of them — a capability that "
            "declares six reports six, which is what makes the table worth reading."
        ),
    )


class MeshConvergenceBody(_StudySpec):
    """The `study` object type, which #44 left thin and #48 defined.

    Everything needed to say which solves the study implies, and nothing about their results:
    a study object is a *question*, and the jobs are the answer.
    """

    kind: Literal["mesh_convergence"] = Field(
        description="A ladder of one parameter, read for where its metrics stop moving."
    )
    parameter: str = Field(
        description=(
            "Which of the capability's parameters the ladder varies — `resolution` for the "
            "mock solvers, `mesh_size` for the FEniCSx ones. Named rather than inferred: no "
            "capability declares which of its parameters controls the mesh, and guessing "
            "from the name would be wrong for the first adapter that spells it differently."
        )
    )
    values: list[float] = Field(
        description="The rungs, in increasing order of refinement effort."
    )
    tolerance: float = Field(
        default=0.01,
        gt=0.0,
        description=(
            "Relative change below which a metric counts as settled, as a fraction. Part of "
            "the specification rather than a display option: 'converged to 1%' and 'converged "
            "to 0.1%' are different claims, and the study revision should say which was made."
        ),
    )

    @model_validator(mode="after")
    def _a_ladder_not_a_list(self) -> "MeshConvergenceBody":
        if len(self.values) < 2:
            # One rung cannot converge to anything. Refused rather than run, because a
            # single-rung "study" would report `settled_at` for a claim it never tested.
            raise ValueError("a convergence study needs at least two values to compare")
        if len(set(self.values)) != len(self.values):
            raise ValueError("values must be distinct; a repeated rung is the same solve twice")
        if sorted(self.values) != self.values:
            # Ascending is not cosmetic: the convergence read-out compares each rung with the
            # previous one, and "settled at" means nothing over a shuffled ladder.
            raise ValueError("values must be in increasing order")
        return self

    def variations(self) -> list[dict[str, float]]:
        """The parameter overrides this study implies, one per solve, in report order."""
        return [{self.parameter: value} for value in self.values]

    def parameters(self) -> list[str]:
        """Which of the capability's parameters this study writes to."""
        return [self.parameter]


class SweepAxis(BaseModel):
    """One parameter and the values it takes — an edge of the grid, not a point on it."""

    parameter: str = Field(
        description="Which of the capability's parameters this axis varies."
    )
    values: list[float] = Field(
        description=(
            "Its values, ascending and distinct. The first axis is also the abscissa of the "
            "response curves, and `SeriesAxis` documents a sweep's abscissa as monotonic — "
            "so the ordering is the specification's, not a display preference."
        )
    )

    @model_validator(mode="after")
    def _an_axis_not_a_bag(self) -> "SweepAxis":
        if not self.values:
            raise ValueError(f"axis {self.parameter!r} has no values")
        if len(set(self.values)) != len(self.values):
            raise ValueError(
                f"axis {self.parameter!r} repeats a value; the same point twice is the same "
                "solve twice"
            )
        if sorted(self.values) != self.values:
            raise ValueError(f"axis {self.parameter!r} must list its values in increasing order")
        return self


class SweepBody(_StudySpec):
    """A parameter sweep — roadmap M5, [#21](https://github.com/mandaloriat/fenix-spoon/issues/21).

    The same machinery as the ladder above over a different axis, which is what #48 built the
    study abstraction for and said so in as many words. What is genuinely new is that a sweep
    varies **several** parameters at once, and that its answer wants to be drawn.

    ## Two ways to say where the points are, and why both

    A **grid** (`axes`) is the full factorial: every combination, last axis varying fastest.
    It is the only design a server can generate without making a choice on the caller's
    behalf — there is exactly one full factorial over a given set of axes.

    **Explicit `points`** are how every other design of experiments gets in: Latin hypercube,
    Sobol, Box–Behnken, or a list somebody wrote by hand. Deliberately *not* generated here,
    and the reason is not a reluctance to add a dependency. **A randomized design generated
    server-side would break the property that defines a study** — that its job list is a pure
    function of its object revision — unless the seed were frozen in the body, at which point
    the caller is specifying the design anyway and may as well specify the points. So the
    caller generates, the revision freezes, and the purity rule survives contact with a DOE.

    ## What it does not do

    It does not choose the next point. That is an optimizer, it is
    [#22](https://github.com/mandaloriat/fenix-spoon/issues/22), and the line is the one #48
    drew: given `study:s-1@2` you can say which solves a sweep implies without running any of
    them, and an optimizer cannot promise that.
    """

    kind: Literal["sweep"] = Field(
        description=(
            "A grid or an explicit set of points, tabulated and drawn against the first axis."
        )
    )
    axes: list[SweepAxis] = Field(
        default=[],
        description=(
            "The grid, as one entry per parameter varied. Mutually exclusive with `points`. "
            "The **first axis is the abscissa** of every response curve and the rest become "
            "one trace per combination — which is how a two-parameter sweep is drawn on "
            "paper, a family of curves, and why the order is worth choosing."
        ),
    )
    points: list[dict[str, float]] = Field(
        default=[],
        description=(
            "The points, given outright, for a design a grid cannot express. Every point "
            "must set the same parameters: a ragged set makes a column meaning 'unset here, "
            "3.0 there', and the two are not comparable. No response curves come back from "
            "this form — there is no axis to draw against, because the caller chose the "
            "points rather than an ordering."
        ),
    )

    @model_validator(mode="after")
    def _a_sweep_not_a_guess(self) -> "SweepBody":
        if bool(self.axes) == bool(self.points):
            raise ValueError(
                "a sweep is a grid (`axes`) or a list of points (`points`), exactly one; "
                "given both, neither is the specification"
            )
        if self.axes:
            named = [axis.parameter for axis in self.axes]
            if len(set(named)) != len(named):
                raise ValueError(
                    f"two axes vary {sorted({n for n in named if named.count(n) > 1})}; "
                    "a grid has one axis per parameter"
                )
            if len(self.axes[0].values) < 2:
                # The first axis is the abscissa. A curve needs two points, and this also
                # bounds the trace count: with at least two values on the abscissa, the
                # remaining combinations cannot exceed MAX_SWEEP_POINTS / 2 — which is
                # exactly `series.MAX_SERIES_TRACES`, so a response curve built from a legal
                # sweep cannot fail the series ceiling. Dropping this rule would let one
                # value on the first axis and sixty-four on the second produce a series the
                # protocol refuses.
                raise ValueError(
                    f"the first axis ({self.axes[0].parameter!r}) is the abscissa of the "
                    "response curves and needs at least two values"
                )
        else:
            if len(self.points) < 2:
                raise ValueError("a sweep of one point is a solve; submit it as a job")
            keys = {frozenset(point) for point in self.points}
            if len(keys) != 1:
                raise ValueError(
                    "every point must set the same parameters; "
                    f"these differ: {sorted(sorted(k) for k in keys)}"
                )
            if not next(iter(keys)):
                raise ValueError("a point that sets no parameter is the design itself")
            seen = [tuple(sorted(point.items())) for point in self.points]
            if len(set(seen)) != len(seen):
                raise ValueError(
                    "points must be distinct; a repeated point is the same solve twice"
                )
        total = len(self.variations())
        if total > MAX_SWEEP_POINTS:
            # Spelled out as a product, because the whole failure mode is that a grid's size
            # is not visible in the body that asks for it.
            shape = " x ".join(str(len(axis.values)) for axis in self.axes) if self.axes else total
            raise ValueError(
                f"this sweep enumerates {total} points ({shape}), over the "
                f"{MAX_SWEEP_POINTS} one study may carry"
            )
        return self

    def variations(self) -> list[dict[str, float]]:
        """The parameter overrides this sweep implies, one per solve, in report order.

        For a grid this is the full factorial with the **last axis varying fastest**, which
        is the odometer order and the one that makes consecutive points differ in the axis a
        curve is drawn along. For explicit points it is what the caller wrote, unchanged —
        reordering them would be this service deciding something the caller already decided.
        """
        if not self.axes:
            return [dict(point) for point in self.points]
        names = [axis.parameter for axis in self.axes]
        return [
            dict(zip(names, combination, strict=True))
            for combination in product(*(axis.values for axis in self.axes))
        ]

    def parameters(self) -> list[str]:
        """Which of the capability's parameters this sweep writes to."""
        if self.axes:
            return [axis.parameter for axis in self.axes]
        return sorted(self.points[0])


#: The `study` object body: a discriminated union on `kind`, so a new kind cannot break a
#: caller that never asks for one — the same rule the wire protocol applies to geometry and
#: result kinds, applied to the one object type whose body *is* a choice of behaviour.
StudyBody = Annotated[MeshConvergenceBody | SweepBody, Field(discriminator="kind")]

STUDY_BODY: TypeAdapter[Any] = TypeAdapter(StudyBody)


class VariationResult(BaseModel):
    """What came back for one variation, whatever kind of study named it.

    Shared by the ladder's rung and the sweep's point because the *answer* half is identical
    — a job reference, a status, whether the cache served it, the metrics, and why not — and
    only the question half differs. Two copies of this would be two places for "cached" to
    stop meaning the same thing.
    """

    job_id: str | None = Field(
        default=None, description="The job that answered it. Null if the submission was refused."
    )
    status: str = Field(
        description="`queued`/`running`/`done`/`failed`/`cancelled`, or `refused`."
    )
    cached: bool = Field(
        default=False,
        description="True when this rung was answered by a solve that had already run.",
    )
    metrics: dict[str, float] = Field(
        default={}, description="The tabulated metrics. Empty until the job finishes."
    )
    error: str | None = Field(
        default=None,
        description=(
            "Why this rung has no answer — a cell-budget refusal, a quota refusal, a failed "
            "solve. Per-rung rather than per-study: rung 4 exceeding the budget does not make "
            "rungs 1–3 unanswered, and reporting the whole study as failed would throw away "
            "work that succeeded."
        ),
    )


class StudyRung(VariationResult):
    """One rung of a ladder: the value it was solved at, beside what came back."""

    value: float = Field(description="The parameter value for this rung.")


class SweepPoint(VariationResult):
    """One point of a sweep: every parameter it set, beside what came back.

    A map rather than a positional tuple, because a point is read against a table whose
    columns are parameter *names* — and a tuple would make the reader count commas against a
    header defined somewhere else in the document.
    """

    values: dict[str, float] = Field(
        description="The parameters this point set, and the values it set them to."
    )


class MetricConvergence(BaseModel):
    """How one metric behaved up the ladder — the reason a convergence study exists.

    A table of numbers is what the issue asks for; this is the sentence a caller would
    otherwise have to derive from it, and deriving it server-side is what keeps the answer
    inside a screen of text.
    """

    metric: str = Field(description="Declared metric name.")
    values: list[float | None] = Field(
        description="Its value at each rung, aligned with the study's `values`. Null where the "
        "rung has no answer yet."
    )
    relative_change: list[float | None] = Field(
        description=(
            "|Δ| / |previous| between consecutive rungs, so one shorter than `values`. Null "
            "where either rung is missing, or where the previous value is zero and a relative "
            "change is undefined rather than infinite."
        )
    )
    settled_at: float | None = Field(
        default=None,
        description=(
            "The first parameter value from which every subsequent change stays under the "
            "study's tolerance. Null when it never settles — including when the ladder simply "
            "has not finished, which is why `status` is worth reading beside it."
        ),
    )


class ConvergenceReport(BaseModel):
    """What `study.get` returns for a ladder: the table, and what it means.

    No field arrays anywhere, by construction — a rung carries metrics, which are scalars, and
    a job reference for the caller that wants more. The acceptance criterion is that this fits
    in a screen of text, and the thing that would break it is a result payload, not a rung.
    """

    study: str = Field(description="The study revision this describes, pinned.")
    kind: Literal["mesh_convergence"] = Field(description="Study kind.")
    design: str = Field(description="The design revision the rungs were solved from, pinned.")
    parameter: str = Field(description="Which parameter the ladder varied.")
    solver: str = Field(description="Capability the rungs ran on.")
    rungs: list[StudyRung] = Field(description="One per value, in the study's order.")
    convergence: list[MetricConvergence] = Field(
        default=[], description="One per tabulated metric."
    )
    complete: bool = Field(
        description="True when every rung has reached a terminal state, answered or refused."
    )


class SweepReport(BaseModel):
    """What `study.get` returns for a sweep: the table, and the curves through it.

    Same rule as the ladder's report — scalars and job references, never a payload — with one
    addition that is not a new idea but an old one reused. **A sweep's answer is a curve, and
    the protocol has carried curves since 1.5.** So the response comes back as
    :class:`~fenixspoon.series.Series1DData`, the same model a `series1d` result uses — so
    anything that already draws a curve draws this one, `<fs-plot>` included, and the SDK
    types it without an addition. A parallel "response" model would have been a second way to
    say the same thing, one of them undrawable.

    *Which is a claim about the shape, not about reach.* A study has no HTTP binding — #44's
    decision, unchanged — so a browser cannot fetch one of these today. What the shared model
    buys is that when it can, there is nothing to write.

    **The curves repeat numbers the table already carries, deliberately.** A table reader and
    a plot are different consumers and neither should have to pivot the other's shape; both
    are built from one pass in one function, so they cannot disagree about a value, and the
    repetition is bounded by :data:`MAX_SWEEP_POINTS` — a curve here is at most sixty-four
    numbers, which is the compact form of a sixty-four-row table rather than an expansion of
    it. That is also the one deliberate exception to "a study report carries no arrays": the
    rule exists to keep field payloads out, and a response curve is the answer, not the field.
    """

    study: str = Field(description="The study revision this describes, pinned.")
    kind: Literal["sweep"] = Field(description="Study kind.")
    design: str = Field(description="The design revision the points were solved from, pinned.")
    parameters: list[str] = Field(
        description="Which parameters the sweep varied, in axis order where it has axes."
    )
    solver: str = Field(description="Capability the points ran on.")
    points: list[SweepPoint] = Field(
        description="One per point, in the study's order — the grid's odometer order, or the "
        "order the caller listed."
    )
    curves: list[Series1DData] = Field(
        default=[],
        description=(
            "One curve set per tabulated metric, against the first axis. Empty for a sweep "
            "given as explicit points, which has no axis to draw against, and empty for a "
            "metric no point has answered yet."
        ),
    )
    complete: bool = Field(
        description="True when every point has reached a terminal state, answered or refused."
    )


#: What `study.get` answers with, discriminated on the same `kind` the study object declares.
#: A ladder's report and a sweep's are different shapes because they answer different
#: questions — "where does this stop moving" and "what does this do across a range" — and one
#: model with both halves optional would make every caller check which half it got.
StudyReport = Annotated[ConvergenceReport | SweepReport, Field(discriminator="kind")]


class StudyRun(BaseModel):
    """The reply to `study.run` — what was started, and what did not need starting.

    Deliberately not the report. Submitting is fast and solving is not, so `study.run` answers
    as soon as the work is accepted and `study.get` is where the table appears. `reused` is on
    this reply because it is the one moment it is interesting: it says how much of the ladder
    the cache just made free.
    """

    study: str = Field(description="The study revision that ran, pinned.")
    jobs: list[str] = Field(description="Job ids, in rung order. Refused rungs are absent.")
    submitted: int = Field(description="Rungs that started a solve.")
    reused: int = Field(description="Rungs answered by a solve that had already run.")
    refused: int = Field(description="Rungs the server would not accept; see `study.get`.")


def relative_changes(values: list[float | None]) -> list[float | None]:
    """|Δ| / |previous| between consecutive rungs — one shorter than the input."""
    changes: list[float | None] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous is None or current is None or previous == 0:
            # Zero previous is not a division to guard against and move past: a relative
            # change against zero is undefined, and reporting `inf` would make a metric that
            # happened to start at zero look like the least converged thing in the study.
            changes.append(None)
        else:
            changes.append(abs(current - previous) / abs(previous))
    return changes


def settled_value(
    ladder: list[float], values: list[float | None], tolerance: float
) -> float | None:
    """The first ladder value from which every later change stays under ``tolerance``.

    "From which every later change stays" rather than "where the change first drops below":
    a metric that dips under the tolerance once and then moves again has not settled, and
    reporting the dip would be reporting noise as a result. It also requires the ladder to be
    complete from that point up — an unfinished rung higher up means the claim cannot be made
    yet, which is different from "it does not converge".
    """
    changes = relative_changes(values)
    for index, _ in enumerate(changes):
        tail = changes[index:]
        if any(change is None for change in tail):
            continue
        if all(change <= tolerance for change in tail):  # type: ignore[operator]
            # `changes[index]` is the step between rung `index` and `index + 1`, so the value
            # the metric has settled *at* is the later of the two.
            return ladder[index + 1]
    return None


def convergence_of(
    ladder: list[float], per_metric: dict[str, list[float | None]], tolerance: float
) -> list[MetricConvergence]:
    """Assemble the read-out for every tabulated metric."""
    return [
        MetricConvergence(
            metric=metric,
            values=values,
            relative_change=relative_changes(values),
            settled_at=settled_value(ladder, values, tolerance),
        )
        for metric, values in per_metric.items()
    ]


def tabulated_metrics(body: _StudySpec, declared: list[str]) -> list[str]:
    """Which metrics to put in the table, refusing the ones that do not exist.

    An unknown metric name is an error rather than a column of nulls, for the reason
    `capability.describe` refuses an unknown section and `result.get` refuses an unknown
    level: a caller that asks for `c_1` when the name is `c_l` must not be handed something
    that looks like an answer.

    A column of nulls is *worse* than a missing column, which is why the first implementation
    of this — returning the names unchecked, while this docstring already claimed otherwise —
    was not a harmless gap. A missing column at least prompts "did I spell that right"; a
    column of nulls reads as "this capability reports `c_1` and the solve did not produce
    it", which is a specific, confident and wrong conclusion. Raised in review of #48.

    Raised from here rather than checked by the caller so the rule lives with the sentence
    that states it — that mismatch is exactly what went wrong the first time.
    """
    if body.metrics is None:
        return list(declared)
    unknown = [name for name in body.metrics if name not in declared]
    if unknown:
        raise errors.InvalidObject(
            "study",
            [
                {
                    "type": "unknown_metric",
                    "loc": ["metrics"],
                    "msg": (
                        f"this capability does not declare {unknown}; "
                        f"it declares {sorted(declared)}"
                    ),
                }
            ],
        )
    return list(body.metrics)


def variation_inputs(
    study_ref: str, index: int, variation: dict[str, float], resolved: Any
) -> dict[str, Any]:
    """What a variation's job records about where it came from.

    The study revision *and* the design revision, because they answer different questions:
    the design says what was solved, the study says why this particular variation of it
    exists. Both are pinned, so the pair is enough to reconstruct the job's parameters
    without consulting anything mutable.

    `variation` is the whole override map rather than the single number a ladder used to
    record. A grid point is not one value, and provenance that named the value without naming
    the parameter was already a half-sentence when only one kind of study existed.
    """
    return {
        "study": study_ref,
        "variation_index": index,
        "variation": dict(variation),
        "design": resolved.design,
        "geometry": resolved.geometry,
        **({"materials": list(resolved.materials)} if resolved.materials else {}),
    }


def response_curves(
    body: SweepBody, points: list[SweepPoint], metrics: list[str], units: dict[str, str]
) -> list[Series1DData]:
    """Draw each tabulated metric against the sweep's first axis.

    One curve set per metric; within it, one trace per combination of the *other* axes, which
    is how a two-parameter sweep is drawn on paper — a family of `c_l(alpha)` curves, one per
    Reynolds number — and why the axis order is part of the specification rather than a
    display choice made here.

    **Every trace carries its own abscissa.** The shared `x` is the tempting encoding and it
    is wrong the moment one point has no answer: a trace's values must line up with the axis
    it is drawn against, and a refused or unfinished point has no value to line up. Per-trace
    abscissae give the identical drawing when the sweep is complete and the only correct one
    while it is not — the same reason the airfoil's two surfaces carry theirs.

    The axis unit is the **empty string, not `"1"`**. A capability declares units for its
    metrics and not for its parameters, so the unit of `alpha` is genuinely unknown here;
    `"1"` would claim it is dimensionless, and reading "degrees" off the name is the class of
    inference [ADR 0001](../../docs/adr/0001-explorable-viewer.md) records the viewer
    refusing. Both render as a bare axis label, so the honest one costs nothing.
    """
    if not body.axes:
        # Explicit points: the caller chose a design, not an ordering, and there is no axis to
        # draw against. Returning a curve over the points' index would be inventing an
        # abscissa nobody specified.
        return []

    abscissa, rest = body.axes[0], body.axes[1:]
    curves: list[Series1DData] = []
    for metric in metrics:
        traces: list[SeriesTrace] = []
        for combination in product(*(axis.values for axis in rest)):
            held = dict(zip((axis.parameter for axis in rest), combination, strict=True))
            drawn = [
                (point.values[abscissa.parameter], point.metrics[metric])
                for point in points
                if metric in point.metrics
                and all(point.values.get(name) == value for name, value in held.items())
            ]
            if not drawn:
                continue  # nothing answered on this line of the grid, yet or ever
            traces.append(
                SeriesTrace(
                    name=metric
                    if not held
                    else f"{metric} @ " + ", ".join(f"{k}={v:g}" for k, v in held.items()),
                    unit=units.get(metric, ""),
                    values=[value for _, value in drawn],
                    x=SeriesAxis(
                        name=abscissa.parameter, unit="", values=[at for at, _ in drawn]
                    ),
                )
            )
        if traces:
            # A curve set with no traces is not an empty drawing, it is an invalid one — the
            # series model refuses it, and rightly: "this metric has no answers" is said by
            # the metric's absence, not by a frame around nothing.
            curves.append(
                Series1DData(
                    name=metric,
                    description=f"{metric} against {abscissa.parameter}",
                    traces=traces,
                )
            )
    return curves
