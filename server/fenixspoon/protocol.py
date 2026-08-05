"""Typed models for wire-protocol payloads (docs/04-wire-protocol.md).

This module is the machine-checkable side of the protocol: the conformance fixtures in
``protocol/fixtures/`` are validated against these models in CI, and the JS SDK (roadmap
M2) mirrors them. Handlers may still build plain dicts, but anything they emit must parse
with these models — the fixture suite also validates live server output.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from .frames import (  # noqa: F401  (re-export for consumers)
    MAX_FRAMES,
    FrameRef,
    check_frame_count,
    frames_of,
)
from .geometry import Domain2D, Geometry, Polygon2D  # noqa: F401  (re-export for consumers)
from .modes import (  # noqa: F401  (re-export for consumers)
    MAX_MODES,
    ModeRef,
    check_mode_count,
    modes_of,
)
from .series import (  # noqa: F401  (re-export for consumers)
    MAX_SERIES_POINTS,
    Series1DData,
    SeriesAxis,
    SeriesTrace,
    check_series,
)
from .solvers.base import ProgressEvent  # noqa: F401  (re-export for consumers)

#: The version of the wire contract this server speaks, `MAJOR.MINOR`.
#:
#: **MAJOR is the compatibility boundary and is mirrored in the path** (`/api/v1`). It
#: changes only when an existing client would break: a field removed or renamed, a type
#: narrowed, a discriminator value changed, an optional field made required, a status code
#: given a new meaning.
#:
#: **MINOR changes are additive** and share the path, because a client written against an
#: earlier minor keeps working: a new optional field, a new member of a discriminated union
#: (a geometry or result kind), a new endpoint, a new solver, a new `stats` key.
#:
#: Deliberately *not* carried in every payload. It does not vary within a session, so
#: repeating it on each event and result would be per-message overhead for something one
#: call answers — see `GET /api/v1/version`, and `docs/04-wire-protocol.md` for the
#: reasoning and the bump procedure.
PROTOCOL_VERSION = "1.14"


class ProtocolVersion(BaseModel):
    """What this server speaks, as `GET /api/v1/version` returns it.

    An *operation* rather than a header or a payload field, because the version has to be
    discoverable over transports that have neither. The planned JSON-RPC and MCP adapters
    (M2.5) expose the same question as `environment.inspect`; a header would have been
    HTTP-only, and a path alone cannot be read without already having guessed it.
    """

    protocol: str = Field(
        # Constrained rather than merely typed. `str` already refuses a JSON number —
        # pydantic v2 does not coerce one, unlike v1 — but it happily accepts "1.banana",
        # which a client would then have to parse defensively. The pattern is what lets a
        # consumer split on "." and trust both halves.
        pattern=r"^\d+\.\d+$",
        description="Wire-contract version as `MAJOR.MINOR`. MAJOR matches the path segment.",
    )
    implementation: str = Field(
        description="Version of this server package. Independent of the protocol version."
    )
    api_path: str = Field(
        description="Path prefix this protocol version is served under, e.g. `/api/v1`."
    )


class StatusEvent(BaseModel):
    """A job's lifecycle transition. The stream ends after a terminal one."""

    type: Literal["status"]
    status: Literal["running", "done", "failed", "cancelled"] = Field(
        description="`done`, `failed` and `cancelled` are terminal; the stream closes after."
    )
    error: str | None = Field(
        default=None, description="Why the job failed. Null unless `status` is `failed`."
    )


class Grid2DData(BaseModel):
    """Fields sampled on a regular grid. Arrays are row-major, y increasing upward."""

    bounds: tuple[float, float, float, float] = Field(
        description="Area the grid covers, as [xmin, ymin, xmax, ymax]."
    )
    shape: tuple[int, int] = Field(description="Grid size as [ny, nx], rows before columns.")
    fields: dict[str, list[float]] = Field(
        description="Field name to ny*nx values, indexed `[iy * nx + ix]`."
    )
    vector_fields: dict[str, list[tuple[float, float]]] = Field(
        default={},
        description=(
            "Field name to ny*nx `[x, y]` pairs, indexed like `fields`. Separate from "
            "`fields` so a vector is one named thing rather than two conventions apart."
        ),
    )
    mask: list[int] = Field(
        description="One entry per grid point, 1 inside the obstacle. Same indexing as a field."
    )

    @model_validator(mode="after")
    def _check(self) -> "Grid2DData":
        ny, nx = self.shape
        n = ny * nx
        if len(self.mask) != n:
            raise ValueError(f"mask has {len(self.mask)} entries, expected ny*nx={n}")
        for name, values in self.fields.items():
            if len(values) != n:
                raise ValueError(f"field {name!r} has {len(values)} entries, expected {n}")
        for name, vectors in self.vector_fields.items():
            if len(vectors) != n:
                raise ValueError(f"vector field {name!r} has {len(vectors)} entries, expected {n}")
        return self


class Mesh2DData(BaseModel):
    """An unstructured triangle mesh with one value per node per field."""

    bounds: tuple[float, float, float, float] = Field(
        description="Bounding box of the mesh, as [xmin, ymin, xmax, ymax]."
    )
    points: list[tuple[float, float]] = Field(description="Node coordinates as [x, y] pairs.")
    triangles: list[tuple[int, int, int]] = Field(
        description="Each triangle as three indices into `points`."
    )
    point_fields: dict[str, list[float]] = Field(
        description="Field name to one value per node, in `points` order."
    )
    point_vector_fields: dict[str, list[tuple[float, float]]] = Field(
        default={},
        description="Field name to one `[x, y]` pair per node, in `points` order.",
    )

    @model_validator(mode="after")
    def _check(self) -> "Mesh2DData":
        n = len(self.points)
        for tri in self.triangles:
            if any(i < 0 or i >= n for i in tri):
                raise ValueError(f"triangle {tri} references a node outside 0..{n - 1}")
        for name, values in self.point_fields.items():
            if len(values) != n:
                raise ValueError(f"point field {name!r} has {len(values)} entries, expected {n}")
        for name, vectors in self.point_vector_fields.items():
            if len(vectors) != n:
                raise ValueError(
                    f"point vector field {name!r} has {len(vectors)} entries, expected {n}"
                )
        return self


class ArtifactRef(BaseModel):
    """A downloadable file the solver produced alongside the inline result."""

    name: str = Field(description="Bare filename, e.g. `solution.vtk`.")
    content_type: str = Field(description="MIME type to serve it with.")
    size: int = Field(description="Size on disk in bytes.")
    url: str = Field(description="Server-relative download path; join with the API base URL.")
    t: float | None = Field(
        default=None,
        description=(
            "The instant this file holds, for a time-dependent solve; null for everything "
            "else. Added in protocol 1.7 (#86) — the artifacts carrying one are the result's "
            "`frames`, and putting the time on the file itself is what makes the index and "
            "the files unable to disagree."
        ),
    )
    mode: int | None = Field(
        default=None,
        description=(
            "The mode number this file holds, for an eigensolve; null for everything else. "
            "Added in protocol 1.14 (#101), and the artifacts carrying one are the result's "
            "`modes` — the same derivation `t` gets, because an eigensolve indexes its "
            "shapes exactly as a transient indexes its instants. A separate field rather "
            "than a reuse of `t`: a mode number is an ordinal, not a time, and a slot whose "
            "meaning has to be guessed from context is the failure this protocol keeps "
            "refusing. The two are mutually exclusive on one file."
        ),
    )

    @model_validator(mode="after")
    def _check_index(self) -> "ArtifactRef":
        if self.t is not None and self.mode is not None:
            raise ValueError(
                f"artifact {self.name!r} carries both an instant and a mode number; a file "
                "holds one or the other, and a result that indexed it twice would order it "
                "two ways"
            )
        return self


class ResultEnvelope(BaseModel):
    """What `GET /api/v1/jobs/{id}/result` returns once a job is `done`."""

    job_id: str = Field(description="The job this result belongs to.")
    kind: Literal["grid2d", "mesh2d", "series1d"] = Field(
        description=(
            "Selects the schema of `data`: `Grid2DData`, `Mesh2DData` or `Series1DData`. "
            "`series1d` was added in protocol 1.5 for answers that are curves rather than "
            "fields — a sweep, a convergence history."
        )
    )
    data: dict[str, Any] = Field(description="The field data, shaped according to `kind`.")
    stats: dict[str, float] = Field(
        default={},
        description=(
            "What the solve cost — `cells`, `dofs`, `iterations`, `seconds`. Keys are "
            "server-defined and all optional: display them, never branch on one existing."
        ),
    )
    metrics: dict[str, float] = Field(
        default={},
        description=(
            "The engineering answer: the scalars this capability declared, keyed by the "
            "names `GET /capabilities/{name}?sections=metrics` reports. Added in protocol "
            "1.3. Distinct from `stats`, which is what the solve *cost* — `t_max` and "
            "`t_rise` moved here from `stats` in 1.3 for exactly that reason."
        ),
    )
    diagnostics: dict[str, Any] = Field(
        default={},
        description=(
            "How the solve went: `converged`, `residual`, `warnings`. Added in protocol "
            "1.3, because `stats` is typed `dict[str, float]` and a flag or a warning "
            "string had nowhere to go in it."
        ),
    )
    provenance: dict[str, Any] = Field(
        default={},
        description=(
            "Where the answer came from: `cached`, the solver and its declared version, the "
            "content-addressed `cache_key`, when it was computed, and the workspace object "
            "revisions it resolved. Added in protocol 1.4. `cached` is the one to read — it "
            "is the difference between a number that reflects the edit you just made and "
            "one answering a question you asked earlier."
        ),
    )
    series: list[Series1DData] = Field(
        default=[],
        description=(
            "Curves this solve produced *alongside* its field — the airfoil adapters return "
            "the flow field and the surface `C_p` from one solve. Added in protocol 1.5. "
            "Empty on a result that carries none, and empty on a `series1d` result, whose "
            "curves are in `data` because that is what `kind` selects."
        ),
    )
    artifacts: list[ArtifactRef] = Field(
        default=[], description="Files the solver wrote, downloadable from the artifact endpoint."
    )

    @computed_field
    @property
    def frames(self) -> list[FrameRef]:
        """Stored instants of a time-dependent solve, in time order (protocol 1.7, #86).

        An index *over* the artifacts rather than beside them: derived from the ones carrying
        a `t`, so it cannot name a file this result does not serve. Empty for a steady solve
        and for a transient that was not asked to save any.
        """
        return frames_of(self.artifacts)

    @computed_field
    @property
    def modes(self) -> list[ModeRef]:
        """Stored mode shapes of an eigensolve, in mode order (protocol 1.14, #101).

        `frames`' twin, and derived the same way — from the artifacts carrying a `mode`, so
        it cannot name a file this result does not serve. Empty for everything that is not an
        eigensolve, and empty for one that was not asked to write its shapes.

        The frequency of each is in the spectrum this result carries as a series, whose
        abscissa is the mode number: the two are joined by that number rather than by
        position in two lists.
        """
        return modes_of(self.artifacts)

    @model_validator(mode="after")
    def _check_frames(self) -> "ResultEnvelope":
        check_frame_count(self.artifacts)
        check_mode_count(self.artifacts)
        return self

    @model_validator(mode="after")
    def _check_data(self) -> "ResultEnvelope":
        model = {"grid2d": Grid2DData, "mesh2d": Mesh2DData, "series1d": Series1DData}[self.kind]
        payload = model.model_validate(self.data)
        # One home per result, not two. A `series1d` result's curves are in `data` because
        # `kind` selects the schema of `data` and that invariant is worth more than the
        # convenience of a single accessor; carrying them in both places would let the two
        # disagree, and a consumer would have to know which one wins.
        if self.kind == "series1d" and self.series:
            raise ValueError(
                "a series1d result carries its curves in `data`; `series` is for the curves "
                "that accompany a field result"
            )
        # Both homes go through the same collective budget. Checking only `series` would have
        # left the ceiling applying to the shape that carries curves *incidentally* and not to
        # the one that is nothing but curves — where an adapter could have put thirty-two
        # full-length traces and met every individual limit.
        check_series([payload] if isinstance(payload, Series1DData) else self.series)
        return self
