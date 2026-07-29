"""The load-test harness's own parsing and statistics (#16).

The measurement path is exercised by actually running it (`make loadtest`); what is worth
unit-testing is the part that silently produces *wrong numbers* rather than failing —
percentile arithmetic and parameter coercion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadtest import DEFAULT_PARAMS, parse_args, parse_params, percentiles, rss_mb  # noqa: E402


def test_percentiles_of_a_known_distribution():
    values = [float(n) for n in range(1, 101)]
    stats = percentiles(values)
    assert stats["p50"] == 50.5
    assert stats["p95"] == 96.0
    assert stats["max"] == 100.0


def test_percentiles_do_not_index_past_the_end():
    """The p95 index is computed, so a short sample must not raise."""
    assert percentiles([7.0]) == {"p50": 7.0, "p95": 7.0, "max": 7.0}
    assert percentiles([]) == {"p50": 0.0, "p95": 0.0, "max": 0.0}


def test_params_are_parsed_as_json_not_strings():
    # A solver's params model wants an int for `resolution`; handing it "96" either fails
    # in strict mode or coerces silently, and the second is worse.
    parsed = parse_params(["resolution=96", "write_vtk=false", "mesh_size=0.035"])
    assert parsed == {"resolution": 96, "write_vtk": False, "mesh_size": 0.035}
    assert isinstance(parsed["resolution"], int)


def test_non_json_param_values_stay_strings():
    assert parse_params(["output=mesh2d"]) == {"output": "mesh2d"}


def test_malformed_param_is_refused():
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        parse_params(["resolution"])


def test_default_params_apply_only_when_none_are_given():
    assert parse_args([]).params == DEFAULT_PARAMS
    # Passing any param replaces the defaults wholesale: mixing mock-solver defaults into
    # another solver's params would send fields it never declared.
    assert parse_args(["--param", "mesh_size=0.05"]).params == {"mesh_size": 0.05}


def test_rss_reads_this_process_and_tolerates_a_dead_one():
    import os

    assert rss_mb(os.getpid()) > 0
    assert rss_mb(999_999_999) is None
