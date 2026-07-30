"""Transport-neutral application core (roadmap M2.5, issue #42).

Import `FenixSpoonCore` and call it. Nothing in here imports FastAPI, and nothing in here
builds a URL — those are an adapter's job. `errors` is the vocabulary an adapter maps onto
its own failure representation.
"""

from . import discovery, errors, results, workspace
from .discovery import (
    SECTIONS,
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
)
from .errors import CoreError
from .results import (
    DEFAULT_LEVELS,
    LEVELS,
    FieldQuery,
    FieldQueryResult,
    LeveledResult,
)
from .service import ArtifactHandle, FenixSpoonCore, ResultView
from .workspace import ObjectSummary, ObjectView, ResolvedDesign, Workspace, WorkspaceInfo

__all__ = [
    "DEFAULT_LEVELS",
    "LEVELS",
    "SECTIONS",
    "ArtifactHandle",
    "CapabilityDescription",
    "CapabilitySummary",
    "CoreError",
    "EnvironmentInfo",
    "FenixSpoonCore",
    "FieldQuery",
    "FieldQueryResult",
    "LeveledResult",
    "ObjectSummary",
    "ObjectView",
    "ResolvedDesign",
    "ResultView",
    "Workspace",
    "WorkspaceInfo",
    "discovery",
    "errors",
    "results",
    "workspace",
]
