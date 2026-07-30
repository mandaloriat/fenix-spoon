"""Turning what the core returns into what the wire carries (roadmap M2.5, issue #45).

The core answers in its own vocabulary — pydantic models, frozen dataclasses, `Path` — and
none of that is a transport's business. This is the one place that translates, so a handler
in :mod:`.methods` returns the core's object and never a hand-built dict that could describe
it slightly wrong.

`mode="json"` on the pydantic side matters: it is what renders a `datetime` as an ISO string
and an enum as its value rather than leaving objects the encoder cannot take. The HTTP
adapter gets the same treatment from FastAPI, which is why the two transports agree on
these shapes without sharing code that says so.
"""

import dataclasses
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..core.wire import Selective


def jsonable(value: Any) -> Any:
    """Recursively render a core value as JSON-encodable data.

    Deliberately not a `json.dumps(default=...)` hook. A hook runs only on values the
    encoder has already failed on, which means a dataclass nested inside a dict of lists
    gets converted while the surrounding structure has already been walked — and it gives
    no place to normalise a `Path`, which `json.dumps` fails on with a message about a
    `PosixPath` rather than about the field that held it.
    """
    if isinstance(value, Selective):
        # A payload where a null field would be a different claim from a missing one renders
        # itself, because the rule belongs to the model rather than to each transport that
        # carries it. See :mod:`fenixspoon.core.wire`.
        return value.wire()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        # Absolute, because the caller is a different process and may have a different
        # working directory. A relative artifact path would resolve to nothing there, or —
        # worse — to something else.
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(item) for item in value]
    return value
