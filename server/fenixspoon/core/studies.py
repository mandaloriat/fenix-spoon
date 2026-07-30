"""Studies: one specification for the shape every iterative workflow shares (#48).

Parameter sweeps, mesh-convergence ladders, material comparisons and load-case comparisons
are the same thing wearing different words — vary something about a design, run the jobs,
collect compact results. Written once here so that each of them is not a bespoke driver, and
so that #21's sweep/DOE API builds on this rather than introducing a second abstraction.

**Exactly one kind is implemented: `mesh_convergence`.** That is the issue's instruction and
it is the right scope — the point is to prove that several jobs can be orchestrated through
object references and compact results, not to ship a framework with one user. The kinds that
would follow (a material comparison, a load-case sweep) are the same machinery over a
different axis, and adding them before anything needs them would be generalising from one
example.

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

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

#: Study kinds this server implements. One, deliberately — see the module docstring.
KINDS = ("mesh_convergence",)


class StudyBody(BaseModel):
    """The `study` object type, which #44 left thin and this issue defines.

    Everything needed to say which solves the study implies, and nothing about their results:
    a study object is a *question*, and the jobs are the answer.
    """

    kind: Literal["mesh_convergence"] = Field(
        description="The only kind implemented. See `study.run` for what it does."
    )
    design: str = Field(
        description=(
            "Reference to the `design` this study varies. Pinned or not — an unpinned "
            "reference resolves to the head when the study runs and is frozen in each job's "
            "provenance, exactly as `job.submit` does."
        )
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
    metrics: list[str] | None = Field(
        default=None,
        description=(
            "Which declared metrics to tabulate. Omit for all of them — a capability that "
            "declares six reports six, which is what makes the table worth reading."
        ),
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
    def _a_ladder_not_a_list(self) -> "StudyBody":
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


class StudyRung(BaseModel):
    """One rung: what was varied, which job answered it, and what it reported."""

    value: float = Field(description="The parameter value for this rung.")
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


class StudyReport(BaseModel):
    """What `study.get` returns: the table, and what it means.

    No field arrays anywhere, by construction — a rung carries metrics, which are scalars, and
    a job reference for the caller that wants more. The acceptance criterion is that this fits
    in a screen of text, and the thing that would break it is a result payload, not a rung.
    """

    study: str = Field(description="The study revision this describes, pinned.")
    kind: str = Field(description="Study kind.")
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


def tabulated_metrics(body: StudyBody, declared: list[str]) -> list[str]:
    """Which metrics to put in the table, and refuse the ones that do not exist.

    An unknown metric name is an error rather than an empty column, for the reason
    `capability.describe` refuses an unknown section: a caller that asks for `c_1` and gets a
    table without it would conclude the capability does not report it, when in fact the name
    was `c_l`.
    """
    if body.metrics is None:
        return list(declared)
    return list(body.metrics)


def variation_inputs(
    study_ref: str, index: int, value: float, resolved: Any
) -> dict[str, Any]:
    """What a rung's job records about where it came from.

    The study revision *and* the design revision, because they answer different questions:
    the design says what was solved, the study says why this particular variation of it
    exists. Both are pinned, so the pair is enough to reconstruct the job's parameters
    without consulting anything mutable.
    """
    return {
        "study": study_ref,
        "variation_index": index,
        "variation_value": value,
        "design": resolved.design,
        "geometry": resolved.geometry,
        **({"materials": list(resolved.materials)} if resolved.materials else {}),
    }
