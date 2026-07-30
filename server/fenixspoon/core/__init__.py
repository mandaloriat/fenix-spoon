"""Transport-neutral application core (roadmap M2.5, issue #42).

Import `FenixSpoonCore` and call it. Nothing in here imports FastAPI, and nothing in here
builds a URL — those are an adapter's job. `errors` is the vocabulary an adapter maps onto
its own failure representation.
"""

from . import discovery, errors, workspace
from .discovery import (
    SECTIONS,
    CapabilityDescription,
    CapabilitySummary,
    EnvironmentInfo,
)
from .errors import CoreError
from .service import ArtifactHandle, FenixSpoonCore, ResultView
from .workspace import ObjectSummary, ObjectView, ResolvedDesign, Workspace, WorkspaceInfo

__all__ = [
    "SECTIONS",
    "ArtifactHandle",
    "CapabilityDescription",
    "CapabilitySummary",
    "CoreError",
    "EnvironmentInfo",
    "FenixSpoonCore",
    "ObjectSummary",
    "ObjectView",
    "ResolvedDesign",
    "ResultView",
    "Workspace",
    "WorkspaceInfo",
    "discovery",
    "errors",
    "workspace",
]
