"""Transport-neutral application core (roadmap M2.5, issue #42).

Import `FenixSpoonCore` and call it. Nothing in here imports FastAPI, and nothing in here
builds a URL — those are an adapter's job. `errors` is the vocabulary an adapter maps onto
its own failure representation.
"""

from . import discovery, errors
from .discovery import (
    SECTIONS,
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
)
from .errors import CoreError
from .service import ArtifactHandle, FenixSpoonCore, ResultView

__all__ = [
    "SECTIONS",
    "ArtifactHandle",
    "CapabilityDescription",
    "CapabilitySummary",
    "CoreError",
    "EnvironmentInfo",
    "FenixSpoonCore",
    "ResultView",
    "discovery",
    "errors",
]
