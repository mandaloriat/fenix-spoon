"""Circulation, lift and surface pressure for the potential-flow adapters (issue #68).

A streamfunction solve with `psi = U*y` at the far field and `psi = const` on the body is a
complete potential-flow solution, and its lift is meaningless. Not wrong — *undetermined*.
On a doubly-connected domain the circulation is fixed by whichever constant the body's
streamline was set to, and both adapters used to pick that constant by a heuristic (the
free-stream value at the obstacle's centroid). A number nobody chose on physical grounds
was setting the most famous output of the model, and the capability declaration said nothing
about it — so #21 and #22 had both written "lift *proxy*" into their acceptance criteria,
because there was no lift to optimise.

This module supplies the missing equation. The whole thing rests on the problem being
**linear in the body's streamfunction value**, which makes the fix a superposition rather
than a new solver:

1. solve once with the real far field and ``psi = 0`` on the body → ``psi_A``;
2. solve once with ``psi = 0`` at the far field and ``psi = 1`` on the body → ``psi_G``;
3. ``psi = psi_A + lam * psi_G`` is a valid solution for *every* ``lam``, and ``lam`` is
   exactly the body's streamfunction value.

So one extra solve buys the entire family, and picking ``lam`` is picking the circulation.
The Kutta condition picks it: at a sharp trailing edge the flow must leave smoothly, which
means equal speed — and therefore equal pressure — on the two sides. That condition is
*linear* in ``lam`` (see :func:`kutta_body_value`), so it is one division, not an iteration.

**Sign conventions, derived once here because every quantity below depends on them.**

With ``u = dpsi/dy`` and ``v = -dpsi/dx``, take a closed contour `C` around the body,
traversed counter-clockwise, with `n` the normal pointing *out of* the region `C` encloses.
The counter-clockwise tangent is ``t = (-n_y, n_x)``, so

    u . t  =  (dpsi/dy)(-n_y) + (-dpsi/dx)(n_x)  =  -(grad psi . n)

and therefore ``Gamma_ccw = -∮_C dpsi/dn ds``. That is the quantity :func:`circulation`
returns, and it is the same on every contour enclosing the body (the difference between two
of them is the integral of ``div grad psi = 0`` over the annulus between them).

Kutta–Joukowski then gives ``L' = -rho * U * Gamma_ccw``: lift is *up* when the circulation is
*clockwise*, because clockwise circulation is what adds speed over the upper surface. Checking
that against the arithmetic: the counter-clockwise loop runs downstream along the underside and
upstream along the top, so a faster top makes the integral negative — and a faster top is
suction, which is lift. The minus sign is not a convention to be chosen, it follows.

Pitching moment uses the aerodynamic convention, **nose-up positive**, which is the opposite
sign to the right-hand moment about *+z*: the leading edge rising is a clockwise rotation in a
frame with `x` aft and `y` up. This is why a cambered section's ``c_m_c4`` comes out negative,
which is what a table of section data shows.

**What is deliberately approximate.** The surface integrals run over the staircase of grid
faces between body and fluid, not over the polygon. Their *projected* areas are exact — a
staircase and the outline it approximates have the same shadow on each axis — so the force
coefficients converge, but they converge slowly and from a first-order discretisation. The
circulation, by contrast, is a contour integral taken well away from the body through clean
interior nodes, so it is much the more accurate of the two. That asymmetry is why ``c_l``
below is the Kutta–Joukowski value and the pressure integral is used to *check* it: two
independent routes to the same number, and a disagreement is reported rather than averaged.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..series import Series1DData, SeriesAxis, SeriesTrace

#: Points per trace in the surface-pressure curve. Well under
#: :data:`~fenixspoon.series.MAX_SERIES_POINTS`, because the staircase around a body on a 512
#: grid has more faces than a plot can show and the curve is meant to be read, not stored.
MAX_SURFACE_SAMPLES = 400

#: How far the circulation contour is placed outside the body's bounding box, in grid cells.
#:
#: Three rather than one: ``np.gradient`` is a central difference, so a node two cells from
#: the body still has a stencil that reaches a node one cell from it, and the nodes closest to
#: a staircase boundary are where the discretisation is worst. Three keeps every node on the
#: contour clear of that, and the contour's value is independent of where it is drawn — so
#: paying a few cells of margin costs nothing but domain.
CONTOUR_MARGIN = 3

#: Fractional disagreement between the two lift routes that is worth telling the caller about.
#:
#: Kutta–Joukowski and the surface-pressure integral are independent computations of one
#: quantity, so their difference measures the discretisation rather than either answer. 25% is
#: chosen to catch a *wrong* result, not an imprecise one: on the staircase surface of a coarse
#: grid the pressure integral is routinely off by 10–15% while the flow field is perfectly
#: reasonable, and a warning that fires on every ordinary run is one a caller learns to ignore.
LIFT_AGREEMENT_TOLERANCE = 0.25


#: The metrics that exist only because a circulation was determined.
#:
#: Single-sourced because this list had been written out in four places — two adapters'
#: warnings, :func:`describe_unavailable`, and the `no_circulation` assumption's `excludes` —
#: so the fifth aerodynamic quantity anyone adds had to be found in all of them.
LIFT_METRICS: tuple[str, ...] = ("circulation", "c_l", "c_m_c4", "x_cp")

#: Said on every run that turns the Kutta condition off, not merely declared in `assumptions`.
#:
#: A caller who reached that mode by accident is exactly the caller who has not read the
#: capability declaration, and a zero lift with no explanation beside it is the failure #68 was
#: filed about. Lives here rather than in each adapter because it was byte-identical in both.
NO_CIRCULATION_WARNING = (
    "no Kutta condition was imposed (`kutta` is false), so the circulation is an artefact of "
    "where the body's streamline was fixed and lift is not a physical result. "
    + ", ".join(f"`{name}`" for name in LIFT_METRICS[:-1])
    + f" and `{LIFT_METRICS[-1]}` are therefore absent."
)


class AerodynamicsUnavailable(ValueError):
    """The body or the grid cannot support this post-processing, with prose saying why.

    Raised rather than returning a zero. A lift coefficient of 0.0 from a failed trailing-edge
    search is exactly the *"wrong-looking-but-correct"* zero #68 was filed about, and repeating
    it under a new name would be the whole point missed.
    """


def velocity_on_grid(
    psi: np.ndarray, x: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """``(u, v)`` from a streamfunction on a regular grid: ``u = dpsi/dy``, ``v = -dpsi/dx``.

    Lives here, beside the docstring that derives the sign convention the rest of this module
    rests on, because it had been written out three times — the mock adapter's field, this
    module's contour integral, and the FEniCSx adapter's sampled speed. Those are precisely the
    quantities whose agreement :func:`analyse` uses as a per-run self-check, so a minus sign
    fixed in one of three places would have made the check compare two different conventions.
    """
    return (
        np.gradient(psi, y[1] - y[0], axis=0),
        -np.gradient(psi, x[1] - x[0], axis=1),
    )


def speed_on_grid(
    psi: np.ndarray, x: np.ndarray, y: np.ndarray, mask: np.ndarray
) -> np.ndarray:
    """``|grad psi|`` on a regular grid, zeroed inside the body.

    Zeroed rather than left as a stale gradient for the reason the adapters already documented
    about their vector fields: there is no flow inside the obstacle, and a leftover value there
    draws arrows into the body and poisons a reduction over the field.
    """
    u, v = velocity_on_grid(psi, x, y)
    return np.where(mask, 0.0, np.hypot(u, v))


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.hypot(vector[0], vector[1]))
    if norm <= 0.0:
        raise AerodynamicsUnavailable("a degenerate edge has no direction")
    return np.asarray(vector, dtype=float) / norm


@dataclass(frozen=True)
class Profile:
    """A body polygon resolved into the geometry aerodynamics is written against.

    Vertices are stored counter-clockwise, which is what makes ``(t_y, -t_x)`` the *outward*
    normal of the edge with direction ``t`` — an identity the trailing-edge probes and the
    surface integrals both rely on, and one that silently inverts on a clockwise polygon.
    """

    points: np.ndarray
    """(n, 2) vertices, counter-clockwise."""

    te_index: int
    """Index into :attr:`points` of the trailing edge."""

    le_index: int
    """Index into :attr:`points` of the leading edge."""

    @property
    def trailing_edge(self) -> np.ndarray:
        return self.points[self.te_index]

    @property
    def leading_edge(self) -> np.ndarray:
        return self.points[self.le_index]

    @property
    def chord_vector(self) -> np.ndarray:
        return self.trailing_edge - self.leading_edge

    @property
    def chord(self) -> float:
        return float(np.hypot(*self.chord_vector))

    @property
    def chord_unit(self) -> np.ndarray:
        return _unit(self.chord_vector)

    @property
    def chord_normal(self) -> np.ndarray:
        """The chord direction rotated a quarter turn, so it points from the chord line to
        the upper surface. What "upper" and "lower" mean is defined by this and nothing else."""
        along = self.chord_unit
        return np.array([-along[1], along[0]])

    @property
    def quarter_chord(self) -> np.ndarray:
        """The moment reference point. A quarter of the chord back from the leading edge is
        the aerodynamic centre of a thin section, which is why section data is tabulated
        there: ``c_m`` about it is very nearly independent of incidence."""
        return self.leading_edge + 0.25 * self.chord_vector


def profile_of(points) -> Profile:
    """Resolve a body polygon into a :class:`Profile`.

    The chord is taken as the **furthest-apart pair of vertices**, rather than the minimum and
    maximum `x`. For an airfoil the two agree; for a body given at an angle they do not, and
    the furthest-apart pair is the one that still means "chord". Incidence is applied to the
    free stream rather than to the geometry (see :func:`far_field_psi`), so a rotated *body* is
    the caller's own coordinate choice and this should survive it.
    """
    pts = np.asarray(points, dtype=float)
    if len(pts) < 3:
        raise AerodynamicsUnavailable(f"a body needs at least 3 vertices, got {len(pts)}")
    # Shoelace: positive is counter-clockwise. Flipping rather than tracking an orientation
    # flag means every consumer below can assume one.
    twice_area = float(
        np.sum(pts[:, 0] * np.roll(pts[:, 1], -1) - np.roll(pts[:, 0], -1) * pts[:, 1])
    )
    if twice_area < 0:
        pts = pts[::-1].copy()
    if twice_area == 0:
        raise AerodynamicsUnavailable("the body polygon encloses no area")

    separation = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=-1)
    first, second = np.unravel_index(int(np.argmax(separation)), separation.shape)
    # Downstream is +x: the free stream is rotated, never the body, so the trailing edge is
    # the aft member of the pair whatever the polygon's winding turned out to be.
    if pts[first][0] <= pts[second][0]:
        le_index, te_index = int(first), int(second)
    else:
        le_index, te_index = int(second), int(first)
    return Profile(points=pts, te_index=te_index, le_index=le_index)


def far_field_psi(xx: np.ndarray, yy: np.ndarray, u_inf: float, alpha_deg: float) -> np.ndarray:
    """The uniform-stream streamfunction at incidence: ``U (y cos a - x sin a)``.

    Incidence enters here and nowhere else. Rotating the *stream* rather than the *body* is
    what keeps a swept parameter from re-meshing anything — and for the superposition above it
    means every angle of attack is a different ``psi_A`` over the same ``psi_G``.
    """
    alpha = np.radians(alpha_deg)
    return u_inf * (yy * np.cos(alpha) - xx * np.sin(alpha))


# ------------------------------------------------------------------- the Kutta condition


def centroid_body_value(points, u_inf: float, alpha_deg: float) -> float:
    """The free-stream streamfunction through a body's centroid.

    What both adapters held the body at before #68, and what they still fall back to when there
    is no Kutta condition to do better — as a shared function because it is the same formula and
    the same caveat in both, and the caveat is the point: this is a *choice*, it sets the
    circulation, and nothing about it is physical.
    """
    centre = np.asarray(points, dtype=float).mean(axis=0)
    return float(far_field_psi(centre[0], centre[1], u_inf, alpha_deg))


def probe_points(profile: Profile, offset: float) -> tuple[np.ndarray, np.ndarray]:
    """Two points flanking the trailing edge, one per surface, at equal distance from it.

    Equal distance is the load-bearing part: the condition below compares
    ``psi - psi_body`` across the two, and reads that difference as a surface *speed* only
    because it is divided by the same length on each side.

    They are offset along the outward normals of the two edges that meet at the trailing edge,
    which for a wedge of any thickness puts one point above the upper surface and one below the
    lower — the two sides whose pressures the Kutta condition equates.
    """
    pts = profile.points
    count = len(pts)
    apex = pts[profile.te_index]
    arriving = apex - pts[(profile.te_index - 1) % count]
    leaving = pts[(profile.te_index + 1) % count] - apex
    # Counter-clockwise winding, so (t_y, -t_x) is outward. `profile_of` guarantees it.
    first = _unit(np.array([arriving[1], -arriving[0]]))
    second = _unit(np.array([leaving[1], -leaving[0]]))
    return apex + offset * first, apex + offset * second


def kutta_body_value(
    psi_a: tuple[float, float], psi_gamma: tuple[float, float]
) -> float:
    """The body streamfunction value that satisfies the Kutta condition. One division.

    At a sharp trailing edge the flow leaves smoothly, so the surface speed — and hence the
    pressure — is the same just above and just below it. With probes at equal distance
    ``d`` on either side, the surface speed on each side is ``|psi(Q) - psi_body| / d`` to
    first order, and the two sides sit on opposite sides of ``psi_body``. Equating the
    magnitudes is therefore

        psi(Q_1) - psi_body  =  psi_body - psi(Q_2)
        psi(Q_1) + psi(Q_2)  =  2 psi_body

    and substituting ``psi = psi_A + lam psi_G`` with ``psi_body = lam`` leaves

        lam = (psi_A(Q_1) + psi_A(Q_2)) / (2 - psi_G(Q_1) - psi_G(Q_2))

    which is linear in the unknown. Both halves of that fraction are small — ``psi_G`` is 1 on
    the body and the probes are near it — but they are small *together*: numerator and
    denominator each scale with the probe distance, so it cancels and the answer has a finite
    limit rather than being an ill-conditioned difference. That is worth knowing, because the
    expression looks like the opposite.
    """
    denominator = 2.0 - psi_gamma[0] - psi_gamma[1]
    if abs(denominator) < 1e-12:
        raise AerodynamicsUnavailable(
            "the circulatory solution is indistinguishable from its body value at both "
            "trailing-edge probes, so the Kutta condition determines nothing; the probes are "
            "probably inside the body"
        )
    return float((psi_a[0] + psi_a[1]) / denominator)


@dataclass(frozen=True)
class GridField:
    """A scalar on a regular grid, with the mask saying which nodes were solved.

    Exists so the interpolation used for the trailing-edge probes is written once and reads
    the same as the rest of this module, rather than being a fourth copy of bilinear weights.
    """

    x: np.ndarray
    y: np.ndarray
    values: np.ndarray
    mask: np.ndarray

    def at(self, px: float, py: float) -> float | None:
        """Bilinear interpolation, or ``None`` if any corner is outside the solved region.

        ``None`` rather than a renormalised blend, which is what
        :func:`fenixspoon.fields.query` does. A query is answering "roughly what is the field
        here" for a human; this is feeding an equation whose whole content is a small
        difference, and a corner replaced by three-quarters of its neighbours would put a
        discretisation error straight into the circulation.
        """
        nx, ny = len(self.x), len(self.y)
        if not (self.x[0] <= px <= self.x[-1] and self.y[0] <= py <= self.y[-1]):
            return None
        step_x = (self.x[-1] - self.x[0]) / (nx - 1)
        step_y = (self.y[-1] - self.y[0]) / (ny - 1)
        fx = (px - self.x[0]) / step_x
        fy = (py - self.y[0]) / step_y
        ix = min(int(fx), nx - 2)
        iy = min(int(fy), ny - 2)
        tx, ty = fx - ix, fy - iy
        if self.mask[iy : iy + 2, ix : ix + 2].any():
            return None
        block = self.values[iy : iy + 2, ix : ix + 2]
        return float(
            block[0, 0] * (1 - tx) * (1 - ty)
            + block[0, 1] * tx * (1 - ty)
            + block[1, 0] * (1 - tx) * ty
            + block[1, 1] * tx * ty
        )


#: How a caller hands this module a way to read a solution at a point.
#:
#: ``None`` means "not evaluable there" — outside the domain, inside the body, or in a cell the
#: solver did not solve. Returning it rather than a filler value is what lets
#: :func:`resolve_kutta` grow its probe distance instead of quietly differencing a placeholder.
Sampler = Callable[[float, float], float | None]


def resolve_kutta(
    profile: Profile,
    stream: Sampler,
    circulatory: Sampler,
    *,
    step: float,
    growth: float = 1.5,
    attempts: int = 10,
) -> float:
    """Place the trailing-edge probes and solve for the body streamfunction value.

    Takes two **samplers** rather than two grids, because the Kutta condition needs exactly two
    point evaluations per basis solution and nothing else. That is what lets each adapter answer
    from its own representation — :meth:`GridField.at` interpolates a raster, and the FEniCSx
    adapter evaluates its P1 solution on the triangulation directly. The first version of this
    function took grids, which forced the FEniCSx adapter to resolve a **Dirichlet value of the
    field it returns** by Cartesian interpolation: `mesh_size` alone could not converge `c_l`,
    and the trailing edge — the one place an unstructured mesh is decisively better — was the
    one place the grid was consulted.

    ``step`` seeds the probe distance in the units of whatever resolves the geometry: a grid
    spacing, or a target element size. The distance is then *searched* rather than fixed. It
    wants to be as small as possible — the condition is a statement about the trailing edge
    itself — and large enough that both probes land somewhere the solution can be read, which
    near a thin trailing edge can take a couple of cells. A fixed fraction of chord would be
    needlessly loose on a fine discretisation and would fail on a coarse one.
    """
    offset = 1.25 * float(step)
    for _ in range(attempts):
        first, second = probe_points(profile, offset)
        readings = [(sample(*first), sample(*second)) for sample in (stream, circulatory)]
        if all(value is not None for pair in readings for value in pair):
            (stream_pair, circulatory_pair) = readings
            return kutta_body_value(stream_pair, circulatory_pair)  # type: ignore[arg-type]
        offset *= growth
    raise AerodynamicsUnavailable(
        f"no trailing-edge probe distance between {1.25 * step:.3g} and {offset:.3g} let both "
        f"probes be evaluated; the body is too small for this discretisation, or too close to "
        f"the domain edge. Refine it, or enlarge the domain."
    )


# ------------------------------------------------------------------------- circulation


def _trapezoid(values: np.ndarray, coords: np.ndarray) -> float:
    """Trapezoidal quadrature, written out rather than imported.

    ``np.trapz`` was removed in NumPy 2 and ``np.trapezoid`` does not exist in NumPy 1, and
    this repository runs against both — the FEniCSx image pins its own. Four lines beats a
    version check.
    """
    values = np.asarray(values, dtype=float)
    widths = np.diff(np.asarray(coords, dtype=float))
    weights = np.zeros(len(values))
    weights[:-1] += widths / 2.0
    weights[1:] += widths / 2.0
    return float((values * weights).sum())


def circulation(
    psi: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    mask: np.ndarray,
    *,
    margin: int = CONTOUR_MARGIN,
) -> float:
    """Counter-clockwise circulation about the body, as a contour integral of the velocity.

    The contour is the body's index-space bounding box grown by ``margin`` cells, which keeps
    every node on it clear of the staircase where the discretisation is worst. Its value does
    not depend on that choice — see the module docstring — so a test can move it and get the
    same number, which is the cheapest available check that this is right.

    A contour integral rather than the equivalent variational identity
    ``Gamma = ∫ grad psi . grad psi_G`` because the latter weights the region *nearest* the
    body most heavily, and on a masked Cartesian grid that region is precisely the least
    trustworthy part of the solution.
    """
    rows, columns = np.nonzero(mask)
    if rows.size == 0:
        raise AerodynamicsUnavailable("this result has no body: the mask is empty")
    ny, nx = psi.shape
    bottom = int(rows.min()) - margin
    top = int(rows.max()) + margin
    left = int(columns.min()) - margin
    right = int(columns.max()) + margin
    # One node in from the edge: `np.gradient` falls back to a one-sided difference on the
    # boundary, and a contour that leans on those is measuring the domain's edge as much as
    # the body's circulation.
    if bottom < 1 or left < 1 or top > ny - 2 or right > nx - 2:
        raise AerodynamicsUnavailable(
            "the body reaches within "
            f"{margin + 1} cells of the domain edge, leaving nowhere to draw a circulation "
            "contour. Enlarge the domain bounds or raise `resolution`."
        )

    u, v = velocity_on_grid(psi, x, y)
    xs = x[left : right + 1]
    ys = y[bottom : top + 1]
    # Counter-clockwise: downstream along the bottom, up the right side, back along the top,
    # down the left. The two reversed legs are the same integral with the path negated.
    return (
        _trapezoid(u[bottom, left : right + 1], xs)
        + _trapezoid(v[bottom : top + 1, right], ys)
        - _trapezoid(u[top, left : right + 1], xs)
        - _trapezoid(v[bottom : top + 1, left], ys)
    )


# --------------------------------------------------------------------- the body surface


@dataclass(frozen=True)
class BodySurface:
    """The staircase of grid faces between body and fluid, as a surface to integrate over.

    One entry per face: where it is, which way it points out of the body, how long it is, and
    which fluid node's speed prices it. The *projected* area of this staircase is exact on each
    axis, which is why a net force computed from it converges to the polygon's — slowly, and
    from a first-order discretisation, but to the right answer.
    """

    at: np.ndarray
    """(m, 2) face midpoints."""

    normal: np.ndarray
    """(m, 2) unit normals, pointing out of the body into the fluid."""

    length: np.ndarray
    """(m,) face lengths."""

    rows: np.ndarray
    """(m,) grid row of the fluid node adjacent to each face."""

    columns: np.ndarray
    """(m,) grid column of that node."""

    def sample(self, field: np.ndarray) -> np.ndarray:
        """One value per face, read from the fluid node beside it."""
        return np.asarray(field)[self.rows, self.columns]


def body_surface(mask: np.ndarray, x: np.ndarray, y: np.ndarray) -> BodySurface:
    """Walk the mask boundary and collect its faces."""
    if not mask.any():
        raise AerodynamicsUnavailable("this result has no body: the mask is empty")
    ny, nx = mask.shape
    step_x = float(x[1] - x[0])
    step_y = float(y[1] - y[0])
    inside_rows, inside_columns = np.nonzero(mask)

    at, normal, length, rows, columns = [], [], [], [], []
    # A face's length is the spacing along the *other* axis: stepping in x exposes a face
    # spanning one cell of y, and vice versa.
    for row_step, column_step, outward, face_length in (
        (0, 1, (1.0, 0.0), step_y),
        (0, -1, (-1.0, 0.0), step_y),
        (1, 0, (0.0, 1.0), step_x),
        (-1, 0, (0.0, -1.0), step_x),
    ):
        neighbour_rows = inside_rows + row_step
        neighbour_columns = inside_columns + column_step
        on_grid = (
            (neighbour_rows >= 0)
            & (neighbour_rows < ny)
            & (neighbour_columns >= 0)
            & (neighbour_columns < nx)
        )
        body_rows = inside_rows[on_grid]
        body_columns = inside_columns[on_grid]
        fluid_rows = neighbour_rows[on_grid]
        fluid_columns = neighbour_columns[on_grid]
        exposed = ~mask[fluid_rows, fluid_columns]
        if not exposed.any():
            continue
        body_rows, body_columns = body_rows[exposed], body_columns[exposed]
        fluid_rows, fluid_columns = fluid_rows[exposed], fluid_columns[exposed]
        at.append(
            np.column_stack(
                [
                    0.5 * (x[body_columns] + x[fluid_columns]),
                    0.5 * (y[body_rows] + y[fluid_rows]),
                ]
            )
        )
        normal.append(np.tile(np.asarray(outward, dtype=float), (len(fluid_rows), 1)))
        length.append(np.full(len(fluid_rows), face_length))
        rows.append(fluid_rows)
        columns.append(fluid_columns)

    if not at:
        raise AerodynamicsUnavailable("the body fills the domain: it has no wetted surface")
    return BodySurface(
        at=np.concatenate(at),
        normal=np.concatenate(normal),
        length=np.concatenate(length),
        rows=np.concatenate(rows),
        columns=np.concatenate(columns),
    )


def pressure_coefficient(speed: np.ndarray, u_inf: float) -> np.ndarray:
    """``C_p = 1 - (q / U)^2``, Bernoulli for an incompressible irrotational flow."""
    if not u_inf:
        raise AerodynamicsUnavailable(
            "a pressure coefficient is normalised by the dynamic pressure, and a zero free "
            "stream has none"
        )
    return 1.0 - (np.asarray(speed, dtype=float) / u_inf) ** 2


@dataclass(frozen=True)
class SurfaceLoads:
    """Force and moment coefficients from integrating pressure over the body."""

    c_l: float
    """Lift coefficient: the component across the free stream, nose-up positive."""

    c_d: float
    """Drag coefficient. **Identically zero in exact potential flow** (d'Alembert). What comes
    back here is the discretisation error in the surface integral, which makes it a useful
    accuracy indicator and a meaningless piece of aerodynamics — which is why it is not a
    declared metric and the `inviscid` assumption lists `drag` under `excludes`."""

    c_m_c4: float
    """Pitching moment about the quarter chord, nose-up positive."""

    c_n: float
    """Force coefficient normal to the chord, which is what `x_cp` divides into."""


def surface_loads(
    surface: BodySurface, cp: np.ndarray, profile: Profile, alpha_deg: float
) -> SurfaceLoads:
    """Integrate a surface pressure distribution into force and moment coefficients.

    Dynamic pressure divides out of every one of these, so nothing here needs a density: the
    coefficients are ``∮ C_p n ds`` over the chord, which is why this function takes no fluid
    properties at all.
    """
    chord = profile.chord
    weighted = np.asarray(cp, dtype=float) * surface.length
    # Pressure pushes *inward*, along -n: the force on the body is -∮ p n ds.
    per_face = -(weighted[:, None] * surface.normal)
    force = per_face.sum(axis=0) / chord

    lever = surface.at - profile.quarter_chord
    # Right-hand moment about +z, then negated: nose-up is a *clockwise* rotation with x aft
    # and y up, and nose-up is what the aerodynamic convention calls positive.
    moment_z = float(
        (lever[:, 0] * per_face[:, 1] - lever[:, 1] * per_face[:, 0]).sum()
    ) / chord**2

    alpha = np.radians(alpha_deg)
    along = profile.chord_unit
    across = profile.chord_normal
    chordwise = float(force @ along)
    normalwise = float(force @ across)
    return SurfaceLoads(
        c_l=float(normalwise * np.cos(alpha) - chordwise * np.sin(alpha)),
        c_d=float(chordwise * np.cos(alpha) + normalwise * np.sin(alpha)),
        c_m_c4=-moment_z,
        c_n=normalwise,
    )


def centre_of_pressure(loads: SurfaceLoads) -> float | None:
    """Chordwise station where the resultant normal force acts, as a fraction of chord.

    ``x_cp/c = 0.25 - c_m_c4 / c_n`` follows from taking moments about the quarter chord.
    ``None`` when the normal force is too small to divide by — an uncambered section at zero
    incidence carries no resultant, so its centre of pressure is genuinely undefined rather
    than being somewhere in particular, and a large number from a near-zero denominator would
    be the most misleading thing this module could return.
    """
    if abs(loads.c_n) < 1e-6:
        return None
    return float(0.25 - loads.c_m_c4 / loads.c_n)


def surface_cp_series(
    surface: BodySurface, cp: np.ndarray, profile: Profile
) -> Series1DData:
    """The surface pressure distribution as a drawable curve, split upper and lower.

    Two traces, each with **its own abscissa**: the staircase does not put a face at the same
    ``x/c`` on both sides of the body, and resampling one side onto the other's stations would
    invent data to avoid using a field the protocol already has.

    Sides are assigned by *position* relative to the chord line rather than by normal
    direction. The blunt steps of a staircase have normals pointing straight up- or downstream,
    where the normal says nothing about which surface a face belongs to, while its position
    always does.
    """
    chord = profile.chord
    offset = surface.at - profile.leading_edge
    station = (offset @ profile.chord_unit) / chord
    side = offset @ profile.chord_normal
    values = np.asarray(cp, dtype=float)

    traces = []
    for name, selected in (("cp_upper", side > 0), ("cp_lower", side < 0)):
        if not selected.any():
            continue
        order = np.argsort(station[selected])
        stations = station[selected][order]
        pressures = values[selected][order]
        if len(stations) > MAX_SURFACE_SAMPLES:
            keep = np.linspace(0, len(stations) - 1, MAX_SURFACE_SAMPLES).astype(int)
            stations, pressures = stations[keep], pressures[keep]
        traces.append(
            SeriesTrace(
                name=name,
                unit="1",
                values=[float(v) for v in pressures],
                x=SeriesAxis(
                    name="x/c", unit="1", values=[float(s) for s in stations]
                ),
            )
        )
    if not traces:
        raise AerodynamicsUnavailable(
            "no face of the body surface falls on either side of the chord line"
        )
    return Series1DData(
        name="surface_cp",
        description=(
            "Pressure coefficient along the body, upper and lower surface against chord "
            "fraction. Conventionally drawn with the y axis inverted, so suction is up."
        ),
        traces=traces,
    )


# ------------------------------------------------------------------------------ the whole


@dataclass(frozen=True)
class Aerodynamics:
    """Everything a potential-flow adapter reports once it has a circulation."""

    metrics: dict[str, float]
    series: list[Series1DData]
    warnings: list[str]


def analyse(
    *,
    profile: Profile,
    psi: np.ndarray,
    speed: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    mask: np.ndarray,
    u_inf: float,
    alpha_deg: float,
) -> Aerodynamics:
    """Circulation, lift, moment, centre of pressure and the surface `C_p` curve.

    Two independent routes to the lift, reported as one number and one check:

    - ``c_l`` is the **Kutta–Joukowski** value, ``-2 Gamma / (U c)``, from a contour integral
      taken away from the body. This is the accurate one.
    - the surface-pressure integral gives the same quantity over the staircase, and is used to
      *verify* it. The two agreeing is a per-run check that needs nothing external, which is
      the strongest thing available here; a disagreement past
      :data:`LIFT_AGREEMENT_TOLERANCE` becomes a warning rather than being averaged away,
      because averaging two numbers to hide that one of them is wrong is how a plausible
      answer survives.

    ``c_m_c4`` and ``x_cp`` have only the surface integral behind them — a moment is not
    recoverable from a scalar circulation — so they inherit its accuracy. Worth knowing when
    reading them off a coarse grid.
    """
    gamma = circulation(psi, x, y, mask, margin=CONTOUR_MARGIN)
    chord = profile.chord
    # L' = -rho U Gamma_ccw, so c_l = L' / (q c) = -2 Gamma_ccw / (U c). The sign is derived in
    # the module docstring; it is not a convention picked to make a test pass.
    c_l = -2.0 * gamma / (u_inf * chord)

    surface = body_surface(mask, x, y)
    cp = pressure_coefficient(surface.sample(speed), u_inf)
    loads = surface_loads(surface, cp, profile, alpha_deg)

    metrics = {
        "circulation": float(gamma),
        "c_l": float(c_l),
        "c_m_c4": float(loads.c_m_c4),
    }
    station = centre_of_pressure(loads)
    if station is not None:
        metrics["x_cp"] = station

    warnings: list[str] = []
    scale = max(abs(c_l), abs(loads.c_l))
    if scale > 1e-3:
        disagreement = abs(c_l - loads.c_l) / scale
        if disagreement > LIFT_AGREEMENT_TOLERANCE:
            warnings.append(
                f"the two routes to lift disagree by {disagreement:.0%}: "
                f"c_l = {c_l:.4g} from circulation (Kutta-Joukowski) against "
                f"{loads.c_l:.4g} from integrating the surface pressure. The reported c_l is "
                f"the first; the second is a staircase approximation of the body and is the "
                f"likelier culprit. Raise `resolution` — and treat c_m_c4 and x_cp, which have "
                f"only the surface integral behind them, with the same suspicion."
            )
    return Aerodynamics(
        metrics=metrics,
        series=[surface_cp_series(surface, cp, profile)],
        warnings=warnings,
    )


def describe_unavailable(exc: AerodynamicsUnavailable) -> str:
    """Turn a failure into the warning a caller sees on the result.

    A run that cannot produce lift must say so in a sentence naming the metrics that are
    therefore missing. The alternative is the metrics quietly not being in the payload, which
    is the shape of problem #68 exists to close.
    """
    named = ", ".join(f"`{name}`" for name in LIFT_METRICS[:-1])
    return (
        f"no circulation could be determined, so {named} and `{LIFT_METRICS[-1]}` are absent "
        f"from this result: {exc}"
    )


__all__ = [
    "Aerodynamics",
    "AerodynamicsUnavailable",
    "BodySurface",
    "GridField",
    "LIFT_METRICS",
    "MAX_SURFACE_SAMPLES",
    "NO_CIRCULATION_WARNING",
    "Profile",
    "Sampler",
    "SurfaceLoads",
    "analyse",
    "CONTOUR_MARGIN",
    "LIFT_AGREEMENT_TOLERANCE",
    "body_surface",
    "centre_of_pressure",
    "centroid_body_value",
    "circulation",
    "describe_unavailable",
    "far_field_psi",
    "kutta_body_value",
    "pressure_coefficient",
    "probe_points",
    "profile_of",
    "resolve_kutta",
    "speed_on_grid",
    "surface_cp_series",
    "surface_loads",
    "velocity_on_grid",
]
