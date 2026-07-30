"""Bounded queries over a result's field data (roadmap M2.5, issue #46).

A `grid2d` result is tens of thousands of floats. That is the right payload for a viewer
widget, which draws every one of them, and the wrong one for any caller that has to *reason*
about the answer — an agent, a CI check, a convergence script. This module is how such a
caller asks a question of a field without receiving it.

Every function here returns a bounded payload. The input is unbounded — answering "where is
the peak" does mean reading the array — but the array never leaves the process. That
asymmetry is the whole design: the cost of reading a field is a file read on the machine that
already has it, and the cost of *sending* one is a context window.

Two result kinds, one set of operations. `grid2d` is a regular lattice with a `mask` marking
points outside the solved domain (the obstacle for potential flow, the fluid for the heat
sink); `mesh2d` is an unstructured triangulation with one value per node. Where the two need
different mathematics — an area-weighted mean is a plain mean on a uniform grid and a
dual-area weighting on a triangulation — they get it, rather than one of them getting an
answer that is merely close.

Deliberately scalar-only. A vector field has no single extremum until somebody picks a norm,
and picking one silently is how a caller ends up comparing magnitudes with components. The
adapters that emit `velocity` also emit `speed` beside it for exactly this reason.

This module knows nothing about jobs, stores or transports: it takes a payload dict and a
kind, and returns numbers. :mod:`fenixspoon.core.results` is the service around it.
"""

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

Reduction = Literal["max", "min", "mean", "integral"]

#: Operations :func:`query` accepts. `max`/`min` carry a location, which is usually the
#: point of asking — "how hot" is half an answer without "where".
OPERATIONS: tuple[str, ...] = (
    "max",
    "min",
    "mean",
    "integral",
    "at_point",
    "over_region",
    "section",
    "sample",
    "hotspots",
)

#: Hard ceiling on how many samples any one query may return. A caller asks for a budget and
#: the server caps it here, because an uncapped `sample` is "the whole field, spelled
#: differently" — the exact thing this module exists to prevent. 512 doubles is about 12 kB
#: of JSON, which is already generous for something billed as compact.
MAX_SAMPLES = 512

#: What `section` and `sample` return when the caller does not say.
DEFAULT_SAMPLES = 64

#: Ceiling on `hotspots`. Small on purpose: past a handful, a list of extrema is a field.
MAX_HOTSPOTS = 32


class FieldError(ValueError):
    """A query that cannot be answered as asked. Carries prose a caller can act on."""


@dataclass(frozen=True)
class FieldData:
    """A result's field data, normalised so the two result kinds answer the same questions.

    ``values`` is one scalar per *sample point*, ``xy`` the coordinate of each, and ``valid``
    which of them are inside the solved domain. Building this once per query means the
    operations below are written against one shape instead of two, and the places where the
    kinds genuinely differ — cell area, interpolation — stay explicit rather than smeared
    through every operation.
    """

    kind: str
    name: str
    values: np.ndarray
    xy: np.ndarray
    valid: np.ndarray
    payload: dict[str, Any]

    @property
    def live(self) -> np.ndarray:
        """Values inside the solved domain, in `xy[valid]` order."""
        return self.values[self.valid]


def _scalar_map(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    return payload.get("fields" if kind == "grid2d" else "point_fields", {}) or {}


def field_names(payload: dict[str, Any], kind: str) -> list[str]:
    """Scalar fields this result carries, in payload order."""
    return list(_scalar_map(payload, kind))


def load(payload: dict[str, Any], kind: str, name: str) -> FieldData:
    """Normalise one named scalar field. Raises :class:`FieldError` if it is not there."""
    scalars = _scalar_map(payload, kind)
    if name not in scalars:
        vectors = payload.get(
            "vector_fields" if kind == "grid2d" else "point_vector_fields", {}
        ) or {}
        if name in vectors:
            raise FieldError(
                f"{name!r} is a vector field; these operations are scalar-only because an "
                f"extremum of a vector needs a norm nobody has chosen. Available scalar "
                f"fields: {sorted(scalars)}"
            )
        raise FieldError(f"no field {name!r} in this result; it has {sorted(scalars)}")

    values = np.asarray(scalars[name], dtype=float)
    if kind == "grid2d":
        ny, nx = payload["shape"]
        xmin, ymin, xmax, ymax = payload["bounds"]
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, ny)
        xx, yy = np.meshgrid(x, y)
        xy = np.column_stack([xx.ravel(), yy.ravel()])
        mask = np.asarray(payload.get("mask") or [], dtype=bool)
        # A mask entry of 1 means "not part of what was solved" in both senses the adapters
        # use it — the obstacle for potential flow, the fluid for the heat sink. Either way
        # those points carry a filler value and must not reach a reduction.
        valid = ~mask if mask.size == values.size else np.ones(values.size, dtype=bool)
    elif kind == "mesh2d":
        xy = np.asarray(payload["points"], dtype=float)
        valid = np.ones(values.size, dtype=bool)
    else:  # pragma: no cover - the result kinds are a closed set on the wire
        raise FieldError(f"cannot query a result of kind {kind!r}")

    if values.size != len(xy):
        raise FieldError(
            f"field {name!r} has {values.size} values for {len(xy)} points; result is malformed"
        )
    return FieldData(kind=kind, name=name, values=values, xy=xy, valid=valid, payload=payload)


# ------------------------------------------------------------------ geometry helpers


def _cell_area(field: FieldData) -> float:
    """Area one grid point stands for. Uniform by construction — `linspace` on both axes."""
    ny, nx = field.payload["shape"]
    xmin, ymin, xmax, ymax = field.payload["bounds"]
    if nx < 2 or ny < 2:
        raise FieldError("grid is degenerate: an integral needs at least a 2x2 lattice")
    return ((xmax - xmin) / (nx - 1)) * ((ymax - ymin) / (ny - 1))


def _triangles(field: FieldData) -> np.ndarray:
    return np.asarray(field.payload["triangles"], dtype=int)


def _triangle_areas(field: FieldData) -> tuple[np.ndarray, np.ndarray]:
    """``(triangles, area per triangle)``. Shoelace on each, always positive."""
    tri = _triangles(field)
    p = field.xy[tri]  # (ntri, 3, 2)
    a = p[:, 1] - p[:, 0]
    b = p[:, 2] - p[:, 0]
    return tri, 0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])


def _nodal_weights(field: FieldData) -> np.ndarray:
    """Dual area per node: each triangle gives a third of itself to each of its corners.

    This is what makes `mean` on a `mesh2d` an *area*-weighted average rather than an
    average over nodes. On a graded mesh the two differ substantially — refinement puts
    many nodes where the field varies fastest, so a nodal mean is biased toward exactly
    the region the mesher decided was interesting.
    """
    tri, areas = _triangle_areas(field)
    weights = np.zeros(len(field.xy))
    for corner in range(3):
        np.add.at(weights, tri[:, corner], areas / 3.0)
    return weights


# ---------------------------------------------------------------------- operations


def _extremum(field: FieldData, which: str) -> dict[str, Any]:
    if not field.valid.any():
        raise FieldError(f"field {field.name!r} has no points inside the solved domain")
    indices = np.flatnonzero(field.valid)
    values = field.values[indices]
    pick = indices[int(np.argmax(values) if which == "max" else np.argmin(values))]
    x, y = field.xy[pick]
    return {"value": float(field.values[pick]), "at": [float(x), float(y)]}


def _mean(field: FieldData) -> dict[str, Any]:
    if field.kind == "grid2d":
        live = field.live
        if live.size == 0:
            raise FieldError(f"field {field.name!r} has no points inside the solved domain")
        # Uniform lattice, so every live point stands for the same area and the plain mean
        # already *is* the area-weighted one.
        return {"value": float(live.mean()), "weighting": "uniform-cell"}
    weights = _nodal_weights(field)
    total = weights.sum()
    if total <= 0:
        raise FieldError("mesh has zero area; nothing to average over")
    return {
        "value": float((field.values * weights).sum() / total),
        "weighting": "nodal-dual-area",
    }


def _grid_quadrature_weights(field: FieldData) -> np.ndarray:
    """Trapezoidal weights per grid point, in cell-area units.

    Summing values and multiplying by the cell area treats every *point* as owning a whole
    cell, which over-counts by `(n/(n-1))^2` because the lattice has one more point per axis
    than it has intervals — 10% on a 21-point grid, which is not a rounding difference.
    Halving the edges and quartering the corners is the trapezoidal rule, and it is exact
    for any field that is linear within a cell.

    With a mask this is still approximate: the boundary of the solved region is a staircase
    through the middle of cells, and no weighting of lattice points fixes that. It is the
    smaller error of the two, and it is the one that does not also affect the unmasked case.
    """
    ny, nx = field.payload["shape"]
    wy = np.ones(ny)
    wx = np.ones(nx)
    if ny > 1:
        wy[0] = wy[-1] = 0.5
    if nx > 1:
        wx[0] = wx[-1] = 0.5
    return np.outer(wy, wx).ravel()


def _integral(field: FieldData) -> dict[str, Any]:
    if field.kind == "grid2d":
        weights = _grid_quadrature_weights(field) * field.valid
        cell = _cell_area(field)
        return {
            "value": float((field.values * weights).sum() * cell),
            "area": float(weights.sum() * cell),
        }
    tri, areas = _triangle_areas(field)
    per_triangle = field.values[tri].mean(axis=1)
    return {"value": float((per_triangle * areas).sum()), "area": float(areas.sum())}


def _at_point(field: FieldData, x: float, y: float) -> dict[str, Any]:
    """Interpolate the field at one coordinate. Bilinear on a grid, barycentric on a mesh."""
    if field.kind == "grid2d":
        return _grid_at_point(field, x, y)
    return _mesh_at_point(field, x, y)


def _grid_at_point(field: FieldData, x: float, y: float) -> dict[str, Any]:
    ny, nx = field.payload["shape"]
    xmin, ymin, xmax, ymax = field.payload["bounds"]
    if not (xmin <= x <= xmax and ymin <= y <= ymax):
        raise FieldError(
            f"({x:g}, {y:g}) is outside the result bounds "
            f"[{xmin:g}, {ymin:g}, {xmax:g}, {ymax:g}]"
        )
    dx = (xmax - xmin) / (nx - 1) if nx > 1 else 1.0
    dy = (ymax - ymin) / (ny - 1) if ny > 1 else 1.0
    fx = (x - xmin) / dx
    fy = (y - ymin) / dy
    ix = min(int(fx), nx - 2) if nx > 1 else 0
    iy = min(int(fy), ny - 2) if ny > 1 else 0
    tx, ty = fx - ix, fy - iy

    corners = [(iy, ix), (iy, ix + 1), (iy + 1, ix), (iy + 1, ix + 1)]
    weights = [(1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty]
    values = field.values.reshape(ny, nx)
    valid = field.valid.reshape(ny, nx)

    total, accumulated = 0.0, 0.0
    for (cy, cx), weight in zip(corners, weights, strict=True):
        cy, cx = min(cy, ny - 1), min(cx, nx - 1)
        if valid[cy, cx]:
            accumulated += weight * values[cy, cx]
            total += weight
    if total <= 0:
        raise FieldError(
            f"({x:g}, {y:g}) falls inside the masked region — no solved values surround it"
        )
    # Renormalising by the live weight rather than returning a blend with filler in it:
    # near the obstacle boundary some corners are masked, and averaging their placeholder
    # into the answer would report a value the solver never computed.
    return {"value": float(accumulated / total), "at": [float(x), float(y)]}


def _mesh_at_point(field: FieldData, x: float, y: float) -> dict[str, Any]:
    tri = _triangles(field)
    p = field.xy[tri]
    v0 = p[:, 1] - p[:, 0]
    v1 = p[:, 2] - p[:, 0]
    v2 = np.array([x, y]) - p[:, 0]
    denominator = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    safe = np.where(np.abs(denominator) < 1e-300, 1e-300, denominator)
    a = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / safe
    b = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / safe
    c = 1.0 - a - b
    tolerance = -1e-9  # a point exactly on an edge belongs to both triangles; take either
    inside = np.flatnonzero((a >= tolerance) & (b >= tolerance) & (c >= tolerance))
    if inside.size == 0:
        raise FieldError(f"({x:g}, {y:g}) is not inside any element of this mesh")
    index = int(inside[0])
    nodes = tri[index]
    weights = np.array([c[index], a[index], b[index]])
    return {
        "value": float((field.values[nodes] * weights).sum()),
        "at": [float(x), float(y)],
    }


def _section(
    field: FieldData, start: list[float], end: list[float], samples: int
) -> dict[str, Any]:
    count = _budget(samples, MAX_SAMPLES, "samples")
    xs = np.linspace(start[0], end[0], count)
    ys = np.linspace(start[1], end[1], count)
    distances, values, points = [], [], []
    skipped = 0
    for x, y in zip(xs, ys, strict=True):
        try:
            values.append(_at_point(field, float(x), float(y))["value"])
        except FieldError:
            # A section across an airfoil crosses the body. Dropping those samples and
            # saying how many beats interpolating filler into the profile, and beats
            # failing the whole query because one end of the line left the domain.
            skipped += 1
            continue
        distances.append(float(np.hypot(x - start[0], y - start[1])))
        points.append([float(x), float(y)])
    if not values:
        raise FieldError("no point on that section falls inside the solved domain")
    return {
        "distance": distances,
        "value": values,
        "at": points,
        "requested": count,
        "skipped": skipped,
    }


def _sample(field: FieldData, samples: int) -> dict[str, Any]:
    """Evenly decimated points, for a caller that wants a feel for the field cheaply."""
    count = _budget(samples, MAX_SAMPLES, "samples")
    indices = np.flatnonzero(field.valid)
    if indices.size == 0:
        raise FieldError(f"field {field.name!r} has no points inside the solved domain")
    if indices.size > count:
        indices = indices[np.linspace(0, indices.size - 1, count).astype(int)]
    return {
        "value": [float(v) for v in field.values[indices]],
        "at": [[float(x), float(y)] for x, y in field.xy[indices]],
        "of": int(field.valid.sum()),
    }


def _hotspots(field: FieldData, count: int, minimum: bool = False) -> dict[str, Any]:
    """The N most extreme points, spread out rather than clustered on one peak.

    Sorting by value and taking the top N returns N neighbours of the same maximum, which
    answers nothing a plain `max` did not. Suppressing everything within a radius of each
    pick gives the *distinct* hot regions, which is the question somebody deciding where to
    add a fin is actually asking.
    """
    wanted = _budget(count, MAX_HOTSPOTS, "hotspots")
    indices = np.flatnonzero(field.valid)
    if indices.size == 0:
        raise FieldError(f"field {field.name!r} has no points inside the solved domain")
    order = indices[np.argsort(field.values[indices])]
    if not minimum:
        order = order[::-1]

    xmin, ymin, xmax, ymax = field.payload["bounds"]
    # A tenth of the diagonal: large enough that two picks are visibly different places,
    # small enough that a real second hotspot is not swallowed by the first.
    radius = 0.1 * float(np.hypot(xmax - xmin, ymax - ymin))
    picks: list[int] = []
    chosen_xy: list[np.ndarray] = []
    for index in order:
        if len(picks) >= wanted:
            break
        point = field.xy[index]
        if any(float(np.hypot(*(point - other))) < radius for other in chosen_xy):
            continue
        picks.append(int(index))
        chosen_xy.append(point)
    return {
        "value": [float(field.values[i]) for i in picks],
        "at": [[float(field.xy[i][0]), float(field.xy[i][1])] for i in picks],
        "radius": radius,
    }


def _over_region(field: FieldData, inside: np.ndarray) -> dict[str, Any]:
    """Statistics restricted to a boolean point selection, supplied by the caller.

    The selection comes from the geometry rather than from the result — a result carries no
    region names — so resolving "the region called `core`" happens a layer up, in
    :mod:`fenixspoon.core.results`, which is the layer that can reach the workspace.
    """
    selected = inside & field.valid
    if not selected.any():
        raise FieldError("no solved points fall inside that region")
    values = field.values[selected]
    peak = int(np.flatnonzero(selected)[int(np.argmax(values))])
    return {
        "count": int(selected.sum()),
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "max_at": [float(field.xy[peak][0]), float(field.xy[peak][1])],
    }


def points_in_polygon(xy: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Vectorised even-odd rule (PNPOLY) over a flat array of points.

    The same test ``mock_laplace.polygon_mask`` runs on a meshgrid, written against `(n, 2)`
    points so it works for a `mesh2d`'s nodes as well as a grid's. Duplicated rather than
    shared because importing a solver module into the query path would make a query depend
    on which adapters happen to be installed.
    """
    inside = np.zeros(len(xy), dtype=bool)
    px, py = polygon[:, 0], polygon[:, 1]
    x, y = xy[:, 0], xy[:, 1]
    j = len(polygon) - 1
    for i in range(len(polygon)):
        denominator = py[j] - py[i] or 1e-30
        crosses = (py[i] > y) != (py[j] > y)
        intercept = (px[j] - px[i]) * (y - py[i]) / denominator + px[i]
        inside ^= crosses & (x < intercept)
        j = i
    return inside


def _budget(requested: int | None, ceiling: int, what: str) -> int:
    if requested is None:
        return min(DEFAULT_SAMPLES, ceiling)
    if requested < 1:
        raise FieldError(f"{what} must be at least 1, got {requested}")
    # Capped rather than refused: a caller asking for 10,000 samples wants "a lot", and
    # giving it the most this server will send beats an error it has to guess a number for.
    return min(int(requested), ceiling)


# --------------------------------------------------------------------------- reduce


def reduce_field(payload: dict[str, Any], kind: str, name: str, reduction: Reduction) -> float:
    """Apply one of the four declared reductions. The bridge from a `MetricSpec` to a number.

    Issue #43 let a capability declare that `t_max` is the `max` of the field `T`; this is
    what turns that declaration into a value, once, in a place both the solve path and a
    later query can call.
    """
    field = load(payload, kind, name)
    if reduction == "max":
        return float(_extremum(field, "max")["value"])
    if reduction == "min":
        return float(_extremum(field, "min")["value"])
    if reduction == "mean":
        return float(_mean(field)["value"])
    if reduction == "integral":
        return float(_integral(field)["value"])
    raise FieldError(f"unknown reduction {reduction!r}")


def query(
    payload: dict[str, Any],
    kind: str,
    field_name: str,
    operation: str,
    *,
    at: list[float] | None = None,
    start: list[float] | None = None,
    end: list[float] | None = None,
    samples: int | None = None,
    count: int | None = None,
    minimum: bool = False,
    inside: np.ndarray | None = None,
) -> dict[str, Any]:
    """Run one bounded operation. Every return value fits in a log line or close to it."""
    if operation not in OPERATIONS:
        raise FieldError(f"unknown operation {operation!r}: expected one of {list(OPERATIONS)}")
    field = load(payload, kind, field_name)

    if operation in ("max", "min"):
        return _extremum(field, operation)
    if operation == "mean":
        return _mean(field)
    if operation == "integral":
        return _integral(field)
    if operation == "at_point":
        if at is None or len(at) != 2:
            raise FieldError("at_point needs `at` as [x, y]")
        return _at_point(field, float(at[0]), float(at[1]))
    if operation == "section":
        if start is None or end is None or len(start) != 2 or len(end) != 2:
            raise FieldError("section needs `start` and `end`, each [x, y]")
        return _section(field, [float(v) for v in start], [float(v) for v in end], samples)
    if operation == "sample":
        return _sample(field, samples)
    if operation == "hotspots":
        return _hotspots(field, count, minimum=minimum)
    if operation == "over_region":
        if inside is None:  # pragma: no cover - the service always resolves this first
            raise FieldError("over_region needs a resolved region selection")
        return _over_region(field, inside)
    raise FieldError(f"unhandled operation {operation!r}")  # pragma: no cover
