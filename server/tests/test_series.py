"""One-dimensional results (#69).

Protocol 1.4 added `series1d`, a kind whose payload is curves rather than a field, and a
`series` list so a *field* result can carry curves beside it. Two things need guarding.

**The shape rules**, because a curve is the one payload where the lengths do not follow from a
`shape` or a `points` list: a trace lines up with a shared abscissa, or with its own, and
nothing in the JSON says which until a validator says it.

**The ceilings**, because a bounded curve and an unbounded one are different protocol features.
Without a limit, "series" is a second name for the field arrays, and #46's separation between
the compact answer and the arrays would have a hole in it that no test would notice.
"""

import json

import pytest
from pydantic import ValidationError

from fenixspoon.core.results import build
from fenixspoon.protocol import ResultEnvelope
from fenixspoon.series import (
    MAX_SERIES_POINTS,
    MAX_SERIES_TOTAL_POINTS,
    MAX_SERIES_TRACES,
    Series1DData,
    SeriesAxis,
    SeriesTrace,
    check_series,
    curves_of,
)
from fenixspoon.solvers.base import SolverResult
from fenixspoon.store import JobRecord, SqliteJobStore


def axis(n: int, name: str = "x/c") -> SeriesAxis:
    return SeriesAxis(name=name, unit="1", values=[i / n for i in range(n)])


def trace(n: int, name: str = "cp", **kwargs) -> SeriesTrace:
    return SeriesTrace(name=name, unit="1", values=[0.5] * n, **kwargs)


def curves(name: str = "surface_cp", **kwargs) -> Series1DData:
    return Series1DData(name=name, x=axis(8), traces=[trace(8)], **kwargs)


# -------------------------------------------------------------------------- the shape


def test_a_trace_lines_up_with_the_shared_abscissa():
    assert len(curves().traces[0].values) == 8
    with pytest.raises(ValidationError, match="shared abscissa"):
        Series1DData(name="s", x=axis(8), traces=[trace(7)])


def test_a_trace_may_bring_its_own_abscissa_and_is_checked_against_that_one():
    """The case the airfoil needs: the upper and lower surface are not sampled at the same
    `x/c`, and resampling one onto the other would invent data to avoid using a field the
    protocol has."""
    pair = Series1DData(
        name="surface_cp",
        traces=[trace(5, "cp_upper", x=axis(5)), trace(9, "cp_lower", x=axis(9))],
    )
    assert pair.x is None, "a shared abscissa is unnecessary when every trace carries one"
    with pytest.raises(ValidationError):
        SeriesTrace(name="cp", unit="1", values=[0.0] * 5, x=axis(6))


def test_a_trace_with_no_abscissa_anywhere_is_refused():
    """The one combination that leaves a curve un-plottable: no shared `x`, and none of its own.
    Accepting it would mean a consumer has to guess the sample positions, and index order is not
    a guess a protocol should invite."""
    with pytest.raises(ValidationError, match="no abscissa"):
        Series1DData(name="s", traces=[trace(4)])


def test_an_empty_series_or_a_repeated_trace_name_is_refused():
    with pytest.raises(ValidationError, match="no traces"):
        Series1DData(name="s", x=axis(4), traces=[])
    with pytest.raises(ValidationError, match="names a trace twice"):
        Series1DData(name="s", x=axis(4), traces=[trace(4, "cp"), trace(4, "cp")])
    with pytest.raises(ValidationError, match="no values"):
        SeriesAxis(name="x", unit="1", values=[])


def test_units_travel_on_the_wire_unlike_the_two_dimensional_kinds():
    """A curve is drawn with axis labels and the client has no other way to learn what the axis
    is. A field is coloured, and `<fs-viewer>` takes its colourbar caption from the page — which
    is exactly the workaround this removes."""
    for model in (SeriesAxis, SeriesTrace):
        assert "unit" in model.model_fields
        assert model.model_fields["unit"].is_required()


# ----------------------------------------------------------------------- the ceilings


def test_one_trace_cannot_grow_to_field_size():
    with pytest.raises(ValidationError, match="over the"):
        trace(MAX_SERIES_POINTS + 1)
    with pytest.raises(ValidationError, match="over the"):
        axis(MAX_SERIES_POINTS + 1)


def test_a_result_cannot_carry_unlimited_curve_sets():
    # Distinctly named, or the uniqueness rule fires first and this stops testing the count.
    with pytest.raises(ValueError, match="over the"):
        check_series([curves(name=f"s{i}") for i in range(MAX_SERIES_TRACES + 1)])


def test_the_total_point_budget_closes_the_gap_the_two_other_limits_leave():
    """The limit that actually stops a series being a field.

    Each of the others is generous on its own and they multiply: thirty-two legal traces of four
    thousand legal points is a quarter of a million numbers, which is a field. So the collection
    is bounded by total points as well, and this is the case that would otherwise slip through
    — every individual part of it is within its own limit.
    """
    per_trace = MAX_SERIES_POINTS
    sets = MAX_SERIES_TOTAL_POINTS // per_trace + 1
    smuggled = [
        Series1DData(name=f"s{i}", x=axis(per_trace), traces=[trace(per_trace)])
        for i in range(sets)
    ]
    for entry in smuggled:  # each one is individually legal
        check_series([entry])
    with pytest.raises(ValueError, match="a curve that large is a field"):
        check_series(smuggled)


def test_a_repeated_series_name_on_one_result_is_refused():
    with pytest.raises(ValueError, match="names a series twice"):
        check_series([curves(), curves()])


def test_a_solver_result_enforces_the_ceilings_where_the_adapter_wrote_them():
    """At construction, inside the adapter, rather than only on the wire. An adapter that wrote
    too much should learn where it wrote it, not three layers later in a serialiser."""
    with pytest.raises(ValidationError, match="names a series twice"):
        SolverResult(kind="grid2d", data={}, series=[curves(), curves()])


# ------------------------------------------------------------------------ the envelope

GRID = {
    "bounds": [0.0, 0.0, 1.0, 1.0],
    "shape": [2, 2],
    "fields": {"speed": [1.0, 1.0, 1.0, 1.0]},
    "mask": [0, 0, 0, 0],
}

#: A surface distribution: two traces, sampled differently, so each carries its own abscissa.
SURFACE = Series1DData(
    name="surface_cp",
    traces=[trace(5, "cp_upper", x=axis(5)), trace(9, "cp_lower", x=axis(9))],
)

#: A sweep: several traces against one shared abscissa.
SWEEP = Series1DData(
    name="lift_curve",
    x=SeriesAxis(name="alpha", unit="deg", values=[-2.0, 0.0, 2.0]),
    traces=[SeriesTrace(name="c_l", unit="1", values=[-0.24, 0.0, 0.24])],
)


def test_a_field_result_may_carry_curves_beside_its_field():
    """One solve, two questions. The airfoil adapters produce the flow field *and* the surface
    `C_p`, and making a caller submit twice for the second would be inventing a round trip."""
    envelope = ResultEnvelope(
        job_id="j-1", kind="grid2d", data=GRID, series=[curves().model_dump()]
    )
    assert [entry.name for entry in envelope.series] == ["surface_cp"]


def test_a_series1d_result_carries_its_curves_in_data_because_that_is_what_kind_selects():
    envelope = ResultEnvelope(job_id="j-1", kind="series1d", data=curves().model_dump())
    assert envelope.kind == "series1d"
    assert envelope.series == [], "the curves are in `data`; `series` accompanies a *field*"


def test_a_series1d_result_may_not_carry_curves_in_both_places():
    """One home per result. Carrying them twice would let the two disagree, and a consumer
    would need a rule for which wins — which is a rule the protocol should not have."""
    with pytest.raises(ValidationError, match="carries its curves in `data`"):
        ResultEnvelope(
            job_id="j-1",
            kind="series1d",
            data=curves().model_dump(),
            series=[curves().model_dump()],
        )


def test_a_series1d_result_validates_its_payload_like_every_other_kind():
    with pytest.raises(ValidationError):
        ResultEnvelope(job_id="j-1", kind="series1d", data=GRID)
    with pytest.raises(ValidationError):
        ResultEnvelope(job_id="j-1", kind="grid2d", data=curves().model_dump())


def test_the_envelope_ceilings_apply_to_a_payload_from_the_wire():
    """The models are the enforcement point for a payload the server did not build itself —
    a proxy, a replay, another implementation of the protocol."""
    body = {
        "job_id": "j-1",
        "kind": "grid2d",
        "data": GRID,
        "series": [curves().model_dump(), curves().model_dump()],
    }
    with pytest.raises(ValidationError, match="names a series twice"):
        ResultEnvelope.model_validate(json.loads(json.dumps(body)))


# ------------------------------------------------- the compact level, for both homes


def test_the_series_level_finds_curves_in_either_home():
    """`curves_of` is the accessor everything downstream of the envelope uses.

    The wire deliberately has two homes for curves and one invariant about `kind`. Every
    *consumer* — the compact level, the store column, the SDK — wants neither of those, it wants
    "the curves", and reading the sibling key directly meant the `series` level answered `[]` for
    a `series1d` result: the one kind that is nothing but curves. Nothing failed, because no
    shipped adapter emits one yet, which is exactly why this is a test and not a bug report.
    """
    field_result = curves_of("grid2d", GRID, [SURFACE])
    assert [entry.name for entry in field_result] == ["surface_cp"]
    assert curves_of("mesh2d", {}, []) == []

    # The case that was silently empty: curves in `data`, because `kind` selects `data`.
    curve_result = curves_of("series1d", SWEEP.model_dump(), [])
    assert [entry.name for entry in curve_result] == ["lift_curve"]
    assert curve_result[0].traces[0].values == SWEEP.traces[0].values


def test_a_series1d_result_survives_a_real_sqlite_round_trip(tmp_path):
    """The curves of a `series1d` result have to reach the **column**, not just the summary.

    This is the gap the review found, and it is worth keeping the shape of the miss on record:
    the sibling test below asserts through `JobRecord.summarize()`, which reads
    `curves_of(...)` — so it passed while `SqliteJobStore.put()` was still writing
    `record.result.series`, which is empty for this kind. The curves were there until the
    process restarted and the store read them back from a column that had `[]` in it.

    A test that stops at the summary cannot see that. This one writes, closes, reopens and
    reads, which is the only way the two halves of the persistence path get compared.
    """
    store = SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    store.put(
        JobRecord(
            id="j-1",
            solver="future.sweep",
            status="done",
            result=SolverResult(kind="series1d", data=SWEEP.model_dump()),
        )
    )
    store.close()

    reopened = SqliteJobStore(tmp_path / "jobs.db", result_dir=tmp_path)
    record = reopened.get("j-1", with_result=False)
    assert record is not None and record.summary is not None
    assert [entry.name for entry in record.summary.series] == ["lift_curve"], (
        "a series1d result's curves did not reach the series column"
    )
    assert record.summary.series[0].traces[0].values == SWEEP.traces[0].values


def test_a_series1d_result_reaches_the_compact_level_through_the_store(tmp_path):
    """The level itself, from a summary — the other half of the path above.

    Asserted through `JobRecord.summarize()` rather than a live solve because no shipped adapter
    emits `series1d`; this is the seam that would have been wrong for the first one that did.
    """
    record = JobRecord(
        id="j-1",
        solver="future.sweep",
        status="done",
        result=SolverResult(kind="series1d", data=SWEEP.model_dump()),
    )
    summary = record.summarize()
    assert summary is not None
    assert [entry.name for entry in summary.series] == ["lift_curve"]

    built = build(
        job_id="j-1",
        solver="future.sweep",
        status="done",
        error=None,
        created_at="2026-07-30T09:00:00+00:00",
        finished_at="2026-07-30T09:00:01+00:00",
        summary=summary,
        artifacts=[],
        data=None,
        provenance=None,
        levels=("series",),
    )
    assert built.series is not None
    assert [entry.name for entry in built.series] == ["lift_curve"]
