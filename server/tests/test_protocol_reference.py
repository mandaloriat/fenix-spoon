"""The generated protocol reference (#17).

CI runs `--check` on every pull request, so the *staleness* of the page is already
guarded there. What these cover is the generator itself: that it renders types a reader
can act on, and that every protocol model reaches the page. A generator that quietly
skipped a model would produce a reference that passes its own freshness check while
being incomplete.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from generate_protocol_reference import build, type_name  # noqa: E402


def test_every_protocol_model_appears():
    page = build()
    for model in (
        "Polygon2D",
        "Domain2D",
        "Region2D",
        "Regions2D",
        "JobRequest",
        "JobCreated",
        "JobStatus",
        "JobList",
        "ProgressEvent",
        "StatusEvent",
        "ResultEnvelope",
        "Grid2DData",
        "Mesh2DData",
        "ArtifactRef",
        "SolverInfo",
    ):
        assert f"## `{model}`" in page, f"{model} is missing from the reference"


def test_discriminator_values_are_shown_not_just_the_word_literal():
    """`Literal` alone would hide the one thing that field is for."""
    page = build()
    assert "`'domain2d'`" in page
    assert "`'regions2d'`" in page


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (str, "str"),
        (int | None, "int \\| null"),
        (list[float], "list[float]"),
        (tuple[float, float], "tuple[float, float]"),
        (dict[str, float], "dict[str, float]"),
        (list[tuple[int, int, int]], "list[tuple[int, int, int]]"),
    ],
)
def test_type_rendering(annotation, expected):
    assert type_name(annotation) == expected


def test_pipes_are_escaped_so_the_markdown_table_survives():
    """A raw `|` in a union type or a description would split the table cell."""
    rendered = type_name(str | None)
    assert "\\|" in rendered and " | " not in rendered


def test_descriptions_are_not_silently_missing():
    """The page is only as useful as the models' field descriptions.

    The five exceptions are discriminators, whose type column already shows the literal
    value — a description there would only repeat it. Anything beyond that is a field
    somebody added without saying what it means.
    """
    page = build()
    undocumented = [
        line for line in page.splitlines() if line.startswith("| `") and line.endswith("|  |")
    ]
    assert len(undocumented) <= 5, "\n".join(undocumented)
