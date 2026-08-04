"""Optimization: choosing the next point, which is what a study may not do (#22).

#48 drew the study/optimizer boundary at a property rather than a description: **a study's
job list is a pure function of its object revision** — given `study:s-1@2` you can say which
solves it implies without running any of them. This module is the other side of that line.
A golden-section search's third point depends on what the first two *answered*, so nothing
can enumerate it in advance, and no amount of scheduling cleverness would make a study of it.

## Why there is still no run record

The obvious conclusion from the above is that an optimizer needs to *store* its trajectory,
where a study does not. It does not, and the reason is worth being precise about, because the
two properties are easy to conflate:

- **Predictable** — you can say what it will solve before it runs. A study is; an optimizer is
  not.
- **Reproducible** — running it twice walks the same path. A study is, and *so is an
  optimizer*, as long as the method is deterministic and the objective is.

Reproducibility is the one storage depends on. The method here is a **pure function from the
history to the next point**, so re-running an optimization replays the identical sequence, and
every evaluation on the second pass is a content-addressed cache hit. The jobs are still the
answer, and `optimize.get` recovers the trajectory by replaying the method and resolving each
point by cache key — the same two-path lookup a study report uses, for the same reason.

Where the objective is *not* reproducible — an adapter that does not declare itself
deterministic, so there is no cache key — the replay is no longer free and the recorded job
`inputs` answer instead, exactly as they do for a study. What is lost is the free second run,
not the record.

## Ask–tell, and why not `scipy.optimize`

`scipy.optimize.minimize_scalar` takes a callable and calls it. That inverts control against a
job service which is asynchronous by construction: satisfying it means either blocking a
thread per optimization while it waits for solves, or pretending an evaluation is cheap. It
would also put the trajectory inside somebody else's stack frame, where neither `optimize.get`
nor a caller polling mid-run can see it.

An **ask–tell** loop keeps the sequence in the open: the method says where to look next, the
service submits it exactly as it submits every other job, and the answer goes back in. The
method never touches a solver, a job or a workspace, which is why it is testable with a list
of numbers and why the whole trajectory is a replay rather than a recording.

That is the argument against the dependency, not a preference for writing one's own. It would
be a different argument for a method with real breadth behind it — a gradient method with a
line search, or the adjoint half of #22 — and if that day comes, the seam is this module's
:func:`next_point`, which those would implement rather than replace.
"""

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..series import Series1DData, SeriesAxis, SeriesTrace

#: Methods this server implements. One, and bounded scalar, which is what the first objective
#: anybody wants from these adapters needs — "which angle of attack gives this lift" is a
#: one-parameter question. Multi-parameter and gradient methods are the rest of #22.
METHODS = ("golden_section",)

#: The most solves one optimization may spend.
#:
#: Unlike a sweep's cap this is not a bound on the *answer* — a trajectory is a handful of
#: rows either way — it is a bound on **compute the caller cannot see**. A sweep's body says
#: how many solves it wants; an optimization's says a tolerance, and how many evaluations that
#: implies depends on the bracket and on the answers. Thirty-two golden-section evaluations
#: shrink a bracket to about 4e-7 of its width, which is past the point where any of these
#: adapters' own discretisation is the limit.
MAX_EVALUATIONS = 32

#: 1/φ. The ratio that lets each iteration after the first reuse one of the two interior
#: points, so a bracket costs one solve to shrink rather than two.
INVPHI = (math.sqrt(5.0) - 1.0) / 2.0


class Objective(BaseModel):
    """What "better" means, in terms the capability already declares.

    A declared metric rather than an expression over several: an objective built from an
    expression would need an expression language on the wire, which is the argument
    [the wire protocol](../../docs/04-wire-protocol.md) settles against UFL in general. A
    caller who wants a combination of metrics wants a capability that declares it.
    """

    metric: str = Field(
        description=(
            "The declared metric to optimise. Refused if the capability does not declare it, "
            "for the reason a study refuses an unknown column: a caller who asks for `c_1` "
            "must not be handed something that looks like an answer."
        )
    )
    sense: Literal["minimize", "maximize", "target"] = Field(
        description=(
            "`minimize`, `maximize`, or `target` to hit a value. All three become a "
            "minimisation internally — negated, or squared distance from the target — so the "
            "method never learns what the caller meant, which is why there is one method."
        )
    )
    target: float | None = Field(
        default=None,
        description="The value to hit. Required by `target`, and refused by the other two.",
    )

    @model_validator(mode="after")
    def _a_target_is_not_decoration(self) -> "Objective":
        if self.sense == "target" and self.target is None:
            raise ValueError("sense 'target' needs the value to hit")
        if self.sense != "target" and self.target is not None:
            # The 1.6 refusal in miniature. A target on a `minimize` is either a
            # misunderstanding or a leftover, and silently ignoring it would optimise
            # something other than what the body appears to ask for.
            raise ValueError(f"sense {self.sense!r} does not take a target")
        return self

    def as_minimum(self, metric: float) -> float:
        """The value the method minimises, from the metric the solve reported."""
        if self.sense == "minimize":
            return metric
        if self.sense == "maximize":
            return -metric
        return (metric - float(self.target)) ** 2

    def unit_of(self, metric_unit: str) -> str:
        """What the minimised value is measured in, given the metric's declared unit.

        Negating leaves it alone; squaring a distance does not. Derived rather than left
        blank because the capability *declares* the metric's unit, so a curve of the objective
        can be captioned correctly — and `1` would be a claim of dimensionlessness rather than
        an absence. Raised in review of #22, where both traces went out unitless.
        """
        if self.sense != "target" or metric_unit in ("", "1"):
            return metric_unit
        return f"({metric_unit})^2"


class OptimizationBody(BaseModel):
    """The `optimization` object: a design, one parameter to move, and what better means.

    The seventh workspace object type, and a *question* like the others — which is why it is
    an object rather than a call with arguments. "The angle that trims this section" is worth
    keeping, sharing and re-running, and re-running it is free.
    """

    design: str = Field(
        description=(
            "Reference to the `design` this optimization varies. Pinned or not — an unpinned "
            "reference resolves to the head when it runs and is frozen in each job's "
            "provenance, exactly as `job.submit` and `study.run` do."
        )
    )
    parameter: str = Field(
        description="Which of the capability's parameters to move. One, for now — see `method`."
    )
    bounds: list[float] = Field(
        description=(
            "The bracket to search, `[lower, upper]`. Required rather than inferred: a "
            "capability's parameter has a schema range, but the range a solver *accepts* and "
            "the range a question is asked over are different claims, and searching the whole "
            "of the first would spend solves outside anything the caller meant."
        )
    )
    objective: Objective = Field(description="What better means.")
    method: Literal["golden_section"] = Field(
        default="golden_section",
        description=(
            "Bounded scalar search on a **unimodal** bracket — stated because it is an "
            "assumption the caller must check, not a detail. Given two minima it converges to "
            "one of them and says nothing about the other."
        ),
    )
    max_evaluations: int = Field(
        default=12,
        ge=2,
        le=MAX_EVALUATIONS,
        description=(
            "The most solves to spend. Two is the floor because the method needs two interior "
            "points before it can discard a side, and a budget that stops at one would report "
            "a 'best' that is simply the first thing tried."
        ),
    )
    tolerance: float = Field(
        default=0.01,
        gt=0.0,
        lt=1.0,
        description=(
            "Stop when the bracket has shrunk to this fraction of its original width. "
            "Relative rather than absolute so it means the same thing for an angle in "
            "degrees and a length in metres — and part of the specification, like a study's "
            "tolerance: 'located to 1% of the range' is a different claim from 0.1%."
        ),
    )

    @model_validator(mode="after")
    def _a_bracket_not_a_pair(self) -> "OptimizationBody":
        if len(self.bounds) != 2:
            raise ValueError("bounds is [lower, upper]")
        lower, upper = self.bounds
        if not lower < upper:
            raise ValueError(f"bounds must increase; {lower} is not below {upper}")
        return self

    def parameter_loc(self, name: str) -> list[str | int]:
        """Where in this body a parameter name was written, for an error to point at."""
        return ["parameter"]


def _replay(
    bounds: list[float], answered: list[float], tolerance: float
) -> tuple[float, float, float | None]:
    """Walk the golden-section state machine over the answers so far.

    Returns the bracket it has narrowed to and where to look next, or None for next when the
    bracket has reached the tolerance.

    Replayed from the start every time rather than stored. That is not the cheap way and it is
    the correct one: a stored bracket would be a second description of a state the history
    already determines, and the two could disagree — the same argument as "the jobs are the
    answer", one level down.

    `answered` is the *minimised* objective at each point previously returned, in that order.
    This function never sees the raw metric, so it cannot know whether the caller asked to
    maximise and cannot develop an opinion about it.
    """
    lower, upper = bounds
    span = upper - lower
    a, b = lower, upper
    c = b - INVPHI * (b - a)
    d = a + INVPHI * (b - a)
    if not answered:
        return a, b, c
    if len(answered) < 2:
        return a, b, d
    fc, fd = answered[0], answered[1]
    index = 2
    while True:
        if (b - a) <= tolerance * span:
            return a, b, None
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - INVPHI * (b - a)
            if index >= len(answered):
                return a, b, c
            fc, index = answered[index], index + 1
        else:
            a, c, fc = c, d, fd
            d = a + INVPHI * (b - a)
            if index >= len(answered):
                return a, b, d
            fd, index = answered[index], index + 1


def next_point(bounds: list[float], answered: list[float], tolerance: float) -> float | None:
    """Where to evaluate next, or None when the bracket has shrunk enough.

    A **pure function of the answers so far**, which is the property everything else here
    rests on: it makes the trajectory a replay rather than a recording, it makes the method
    testable against a list of numbers with no solver anywhere near it, and it is the seam a
    second method would implement.
    """
    return _replay(bounds, answered, tolerance)[2]


def bracket_of(bounds: list[float], answered: list[float]) -> list[float]:
    """What the search has narrowed the parameter to, given the answers so far.

    Reported beside the best point because the two say different things: the best point is
    where the lowest value was *seen*, and the bracket is where the minimum is *known to be*.
    A search that stopped on its budget has a bracket wider than its tolerance, and a report
    that gave only the best point would present that as a located answer.

    The tolerance is deliberately absent here — this asks where the search has got to, not
    whether that is far enough, and passing one would let a caller ask the question in a way
    that answers itself.
    """
    a, b, _ = _replay(bounds, answered, tolerance=0.0)
    return [a, b]


class Evaluation(BaseModel):
    """One solve the search asked for, and what came back."""

    iteration: int = Field(description="Position in the sequence, from zero.")
    value: float = Field(description="The parameter value this evaluation used.")
    job_id: str | None = Field(
        default=None, description="The job that answered it. Null if the submission was refused."
    )
    status: str = Field(
        description="`queued`/`running`/`done`/`failed`/`cancelled`, or `refused`."
    )
    cached: bool = Field(
        default=False,
        description="True when a solve that had already run answered it — every evaluation of "
        "a second run of the same optimization.",
    )
    metric: float | None = Field(
        default=None,
        description="The declared metric as the solve reported it. Null until it finishes.",
    )
    objective: float | None = Field(
        default=None,
        description=(
            "The same number as the method saw it: negated for a `maximize`, squared distance "
            "for a `target`. Both are reported because only one of them is the engineering "
            "quantity — a report carrying the objective alone would show a maximised lift as "
            "a negative number and call it the best."
        ),
    )
    error: str | None = Field(default=None, description="Why this evaluation has no answer.")


class OptimizationRun(BaseModel):
    """The reply to `optimize.run` — the search has been *accepted*, not finished.

    It used to be the finished report, and ADR 0002 decision 4 is the argument for the change:
    a search is minutes of solving and HTTP cannot hold a request open across whatever proxies
    and browser timeouts sit between a page and the server. The first draft of that record
    concluded the transports would have to diverge, then found the codebase had already
    answered the question for jobs — `job.submit` returns a receipt everywhere and the CLI
    waits, and nobody calls that a divergence, because **waiting is a convenience of the
    caller-facing layer**, not part of the operation. So this joins that pattern instead of
    breaking it, and there is no exception to teach the conformance suite.

    Deliberately not a job id or a list of them. A search cannot say which points it will
    evaluate — that is what "chooses the next one from the last answer" means — so a receipt
    promising jobs would be promising something unknowable. `optimize.get` is where the
    trajectory appears, one row at a time, and it was already readable mid-search.
    """

    optimization: str = Field(description="The optimization revision that is running, pinned.")
    started: bool = Field(
        description=(
            "False when a search for this revision was already in flight in this process and "
            "this call joined it rather than starting a second one. The analogue of a study's "
            "`reused`: it says you did not pay for this. Two searches over one revision would "
            "not corrupt anything — they replay the same sequence and every point is a cache "
            "hit — but they would be two loops doing one loop's work."
        )
    )


class OptimizationReport(BaseModel):
    """The trajectory, the best point, and why the search stopped.

    Compact by the same rule as a study report — scalars and job references, never a payload —
    with the convergence history as `Series1DData`, which is the shape
    [protocol 1.5](../../docs/04-wire-protocol.md#one-dimensional-results) named as its
    motivating case when it was introduced.
    """

    optimization: str = Field(description="The optimization revision this describes, pinned.")
    design: str = Field(description="The design revision the evaluations were solved from.")
    solver: str = Field(description="Capability they ran on.")
    parameter: str = Field(description="The parameter the search moved.")
    objective: Objective = Field(description="What better meant.")
    evaluations: list[Evaluation] = Field(
        description="Every solve the search asked for, in the order it asked."
    )
    best: Evaluation | None = Field(
        default=None,
        description=(
            "The best evaluation so far — the answer. Null when nothing has been answered. "
            "The *best seen*, not the last: golden section brackets rather than descends, so "
            "the final point is not the lowest one in general."
        ),
    )
    bracket: list[float] | None = Field(
        default=None,
        description=(
            "What the search has narrowed the parameter to, `[lower, upper]`. The honest "
            "answer to 'where is it': a search that stopped on its budget has a bracket wider "
            "than its tolerance, and reporting the best point alone would hide that."
        ),
    )
    evaluations_spent: int = Field(description="How many solves this cost.")
    stopped: Literal["converged", "budget", "stalled", "incomplete", "not_run"] = Field(
        description=(
            "Why it is not still going. `converged` — the bracket reached the tolerance. "
            "`budget` — `max_evaluations` ran out first, and the bracket says how far it got. "
            "`stalled` — an evaluation has no answer, which ends a *search* where it would "
            "only leave a gap in a study's table: the next point is a function of that answer, "
            "so there is nowhere to continue from. `not_run` — nothing has been submitted. "
            "`incomplete` — **only from `optimize.get`**: the trajectory stops and the jobs do "
            "not say why, because a submission the server refused leaves no job behind for a "
            "replay to find. A run was there and says `stalled`; a replay knows only how far "
            "it got, and saying more than that would be inventing the reason."
        )
    )
    history: Series1DData | None = Field(
        default=None,
        description=(
            "The convergence history as a curve: the minimised objective and the metric it "
            "came from, against the evaluation index, drawn by anything that draws a "
            "`series1d`. Not the parameter — where the search *looked* is the `value` column "
            "of every row above, in the order it looked, and putting a bracket in degrees on "
            "the same axis as a lift coefficient would caption neither."
        ),
    )
    running: bool = Field(
        default=False,
        description=(
            "Whether a search for this revision is in flight **in the process answering this "
            "call** (protocol 1.12). The one field here that is not a replay: everything else "
            "is a pure function of the object and the jobs, and this is process state, which "
            "is why it says so rather than claiming to know globally. Behind two API replicas "
            "a search driven by the other one reads as `false` with `stopped: incomplete` — "
            "true statements both, and better than a `true` this process cannot substantiate. "
            "It is what `optimize run` and `wait_for_optimization` poll, and those are "
            "single-process by construction."
        ),
    )


def history_curve(
    objective: Objective, evaluations: list[Evaluation], metric_unit: str = ""
) -> Series1DData | None:
    """The trajectory as a drawable curve, or None when nothing has been answered.

    Two traces against the evaluation index: **the minimised objective, and the metric it was
    computed from**. Not the parameter — where the search looked is the `value` column of
    every row of the report, in the order it looked, and a bracket in degrees on the same axis
    as a lift coefficient would caption neither. (The docstring here said "where it looked"
    while the code emitted the metric, which is the mismatch a self-documenting schema exists
    to avoid; raised in review of #22.)

    Both traces carry the unit the capability declares, transformed for the objective where
    the sense transforms it. Each carries its own abscissa for the reason a sweep's do — an
    evaluation with no answer has no value to line up, and a shared axis would force it to
    become a zero.
    """
    answered = [e for e in evaluations if e.objective is not None]
    if not answered:
        return None
    index = SeriesAxis(
        name="evaluation", unit="", values=[float(e.iteration) for e in answered]
    )
    return Series1DData(
        name="convergence",
        description=f"{objective.sense} {objective.metric} over {len(answered)} evaluations",
        traces=[
            SeriesTrace(
                name="objective",
                unit=objective.unit_of(metric_unit),
                values=[float(e.objective) for e in answered],
                x=index,
            ),
            SeriesTrace(
                name=objective.metric,
                unit=metric_unit,
                # From `answered` rather than filtered again: an evaluation carries its metric
                # and its objective together or not at all, and a second filter would be a
                # second definition of "answered" for the two traces to disagree over.
                values=[float(e.metric) for e in answered],
                x=index,
            ),
        ],
    )


def variation_inputs(
    ref: str, iteration: int, variation: dict[str, float], resolved: Any
) -> dict[str, Any]:
    """What an evaluation's job records about where it came from.

    The same shape a study's rung records, deliberately: `iteration` is this sequence's
    `variation_index`, and a reader of a job's provenance should not have to learn two
    vocabularies for "which step of what produced this".
    """
    return {
        "optimization": ref,
        "variation_index": iteration,
        "variation": dict(variation),
        "design": resolved.design,
        "geometry": resolved.geometry,
        **({"materials": list(resolved.materials)} if resolved.materials else {}),
    }
