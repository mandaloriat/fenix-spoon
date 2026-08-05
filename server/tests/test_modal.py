"""The eigensolve, and the mode index it needed from the protocol (#101).

Three things, and the middle one is the reason the issue was filed:

- **Where it resonates, against answers from outside this repository.** A prismatic beam has
  closed-form natural frequencies — 1.875 for a cantilever, 4.730 for a free-free bar — and
  they are what these tests compare against rather than against a previous run of this code.
- **The free-free case works.** An unrestrained structure has a singular stiffness matrix and
  exactly three rigid-body modes in the plane. They are counted rather than filtered, and the
  count is a correctness check a caller can read.
- **A mode number is not an instant.** Protocol 1.14 gives an ordering that is not time its
  own field, and the tests here are what say the two indices stay apart.
"""

import asyncio
import math

import numpy as np
import pytest
from geometries import UNIFORM_BEAM, rect
from pydantic import ValidationError

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import BoundarySpec, NearSelector, Region2D, Regions2D
from fenixspoon.jobs import JobManager
from fenixspoon.modes import MAX_MODES, modes_of
from fenixspoon.protocol import ArtifactRef, ResultEnvelope
from fenixspoon.solvers.base import SolverContext
from fenixspoon.solvers.mock_modal import (
    MockModal2D,
    count_rigid_body_modes,
    natural_frequencies,
)

#: The beam `UNIFORM_BEAM` describes: 1 m long, 100 mm deep, steel, per unit thickness.
LENGTH, DEPTH = 1.0, 0.1
YOUNGS, DENSITY = 2.1e11, 7850.0


def beam_frequency(root: float) -> float:
    """The Euler-Bernoulli frequency for a mode whose eigenvalue root is `root * L`.

    `f = (beta L)^2 / (2 pi) * sqrt(E I / (rho A L^4))`, with the roots of the transcendental
    frequency equation supplied by the caller: 1.875104 for a cantilever's first mode,
    4.730041 for a free-free bar's first elastic one. The formula is textbook and the roots
    are tabulated, which is the point — nothing here is a previous run of this solver.
    """
    inertia = DEPTH**3 / 12.0  # per unit thickness
    return (root**2 / (2.0 * math.pi)) * math.sqrt(
        YOUNGS * inertia / (DENSITY * DEPTH * LENGTH**4)
    )


def make_ctx(tmp_path, conditions=None, events=None):
    return SolverContext(
        progress_cb=(events.append if events is not None else lambda e: None),
        artifact_dir=tmp_path / "artifacts",
        conditions=conditions,
    )


def solve(tmp_path, geometry=UNIFORM_BEAM, conditions=None, **params):
    params.setdefault("write_vtk", False)
    return MockModal2D().solve(
        geometry, MockModal2D.Params(**params), make_ctx(tmp_path, conditions)
    )


def spectrum(result) -> list[float]:
    return result.series[0].traces[0].values


def _vtk_scalars(path) -> np.ndarray:
    """The point data of a legacy-VTK structured-points file the mock wrote."""
    lines = path.read_text().splitlines()
    start = lines.index("LOOKUP_TABLE default") + 1
    return np.array([float(value) for value in lines[start:] if value.strip()])


# ------------------------------------------------------------- against a closed form


def test_the_cantilever_matches_the_euler_bernoulli_frequency(tmp_path):
    """The first bending mode of a clamped beam, against the tabulated 1.875 root.

    Stiff rather than merely close, and *predictably* stiff: the constant-strain triangle
    over-estimates bending stiffness, so the frequency comes out above the beam formula and
    walks down toward it as the mesh refines. Both halves of that are asserted, because "it
    is within 8%" alone would pass just as well for a solver that was wrong by a constant.
    """
    analytic = beam_frequency(1.875104)
    coarse = solve(tmp_path, resolution=32, n_modes=4, fixed_edge="xmin")
    fine = solve(tmp_path, resolution=64, n_modes=4, fixed_edge="xmin")

    assert fine.metrics["f_1"] == pytest.approx(analytic, rel=0.06)
    assert fine.metrics["f_1"] > analytic, "the CST element is stiff in bending, not soft"
    assert fine.metrics["f_1"] < coarse.metrics["f_1"], "refining must approach the beam formula"


def test_the_free_free_bar_matches_its_own_closed_form(tmp_path):
    """The first *elastic* mode of an unrestrained beam, against the tabulated 4.730 root.

    A better-converged check than the cantilever, and deliberately kept beside it: there is no
    clamp for the discretisation to over-constrain, so what is left is the element's bending
    stiffness alone.
    """
    result = solve(tmp_path, resolution=48, n_modes=6, mode=4)
    assert result.metrics["f_1"] == pytest.approx(beam_frequency(4.730041), rel=0.03)


def test_an_unrestrained_structure_has_exactly_three_rigid_body_modes(tmp_path):
    """The case an eigensolve has to get right, and the check that says it did.

    Two translations and a rotation cost no strain energy, so K is singular and its null space
    has dimension three — in the plane, always. Four would mean the solver invented one; two
    would mean the structure is held somewhere its author did not intend. It is reported
    rather than filtered for exactly that reason.
    """
    result = solve(tmp_path, resolution=40, n_modes=8, mode=4)
    frequencies = spectrum(result)

    assert result.metrics["rigid_body_modes"] == 3
    # And they are *zero* rather than merely small: six orders below the first elastic mode.
    assert max(frequencies[:3]) < 1e-5 * frequencies[3]
    assert result.metrics["f_1"] == pytest.approx(frequencies[3])


def test_a_restrained_structure_has_none(tmp_path):
    result = solve(tmp_path, resolution=32, n_modes=4, fixed_edge="xmin")
    assert result.metrics["rigid_body_modes"] == 0
    assert result.metrics["f_1"] == pytest.approx(spectrum(result)[0])


def test_a_symmetric_structure_has_degenerate_modes(tmp_path):
    """Degeneracy is normal, and nothing may assume distinct eigenvalues.

    A square has a symmetry that maps one mode onto another, so their frequencies are equal:
    the pair comes back as two modes at (numerically) one frequency, with an arbitrary but
    orthogonal basis of the shared eigenspace. A solver that deduplicated eigenvalues, or that
    returned one vector twice, would fail here.
    """
    material = {"youngs_modulus": YOUNGS, "poisson_ratio": 0.3, "density": DENSITY}
    square = Regions2D(
        bounds=(0.0, 0.0, 1.0, 1.0),
        regions=[Region2D(name="body", shape=rect(0.4, 0.4, 0.6, 0.6), material=material)],
        background=material,
    )
    frequencies = spectrum(solve(tmp_path, geometry=square, resolution=40, n_modes=7, mode=4))

    elastic = frequencies[3:]
    pairs = [
        (a, b) for a, b in zip(elastic, elastic[1:], strict=False) if abs(b / a - 1.0) < 1e-3
    ]
    assert pairs, f"no degenerate pair among {elastic}"
    # Equal to within the discretisation's own asymmetry rather than exactly equal: the mesh
    # of a square is not itself symmetric under the rotation that makes the pair degenerate,
    # so the two come back a fraction of a percent apart. Asserting exact equality would be
    # asserting something about the mesh generator, not about the physics.
    assert pairs[0][1] / pairs[0][0] == pytest.approx(1.0, abs=1e-3)


def test_density_moves_every_frequency(tmp_path):
    """`f` scales as `1/sqrt(rho)`, which is the cheapest whole-model check there is."""
    light = solve(tmp_path, resolution=32, n_modes=4, fixed_edge="xmin", density=DENSITY)
    heavy = solve(tmp_path, resolution=32, n_modes=4, fixed_edge="xmin", density=4.0 * DENSITY)
    assert heavy.metrics["f_1"] == pytest.approx(light.metrics["f_1"] / 2.0, rel=1e-6)


# ------------------------------------------------------------------ the answer's shape


def test_the_spectrum_is_a_curve_with_the_mode_number_on_its_axis(tmp_path):
    """The `series1d` row that had no producer, and the join it makes possible.

    The abscissa is the mode number — the same number the shape artifacts carry — so a caller
    reads "mode 4 is at 537 Hz" and fetches mode 4's shape by that number, rather than by
    position in two lists that happen to be ordered alike.
    """
    result = solve(tmp_path, resolution=32, n_modes=5, mode=4)
    curve = result.series[0]

    assert curve.name == "spectrum"
    assert (curve.x.name, curve.x.unit) == ("mode", "1")
    assert curve.x.values == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [trace.name for trace in curve.traces] == ["frequency", "rigid_body"]
    assert curve.traces[0].unit == "Hz"
    # The flag marks the three zero modes, so the curve alone says where the physics starts.
    assert curve.traces[1].values == [1.0, 1.0, 1.0, 0.0, 0.0]


def test_every_mode_shape_is_an_indexed_artifact(tmp_path):
    """Protocol 1.14: the shapes are an index over artifacts, derived from the files.

    The same design 1.7 used for instants, which is the finding #101 predicted — an eigensolve
    needed almost nothing new, because a list of large things ordered by a number is a shape
    this protocol already had.
    """
    ctx = make_ctx(tmp_path)
    result = MockModal2D().solve(
        UNIFORM_BEAM, MockModal2D.Params(resolution=32, n_modes=4, mode=1), ctx
    )
    assert result.kind == "mesh2d"

    registered = ctx.artifacts
    assert [item["name"] for item in registered] == [f"mode_{i}.vtk" for i in (1, 2, 3, 4)]
    assert [item["mode"] for item in registered] == [1, 2, 3, 4]
    assert all(item.get("t") is None for item in registered), "a mode is not an instant"

    index = modes_of([ArtifactRef(url="/x", **item) for item in registered])
    assert [entry.mode for entry in index] == [1, 2, 3, 4]
    assert index[2].artifact == "mode_3.vtk"


def test_the_payload_carries_the_mode_that_was_asked_for(tmp_path):
    """`mode` selects the shape in `data`; the rest stay by reference.

    Checked against the *artifact of the same number*, which is the invariant that matters: a
    payload showing one mode while the index labels it another would be a picture of the
    wrong thing, and nothing else in the result would contradict it.

    Not checked by shape: the three rigid-body modes are a degenerate triple, so which
    combination of translation and rotation lands in mode 1 is arbitrary and asserting on it
    would be asserting on LAPACK's choice of basis.
    """
    ctx = make_ctx(tmp_path)
    result = MockModal2D().solve(
        UNIFORM_BEAM, MockModal2D.Params(resolution=32, n_modes=5, mode=4), ctx
    )
    payload = np.asarray(result.data["point_fields"]["u_mag"])
    stored = _vtk_scalars(tmp_path / "artifacts" / "mode_4.vtk")

    assert payload.max() == pytest.approx(stored.max(), rel=1e-9)
    assert payload.max() > 0.0
    # And a different mode really is a different shape, so the check above has content.
    other = _vtk_scalars(tmp_path / "artifacts" / "mode_1.vtk")
    assert abs(other.max() - stored.max()) > 1e-6 * stored.max()


def test_asking_for_a_shape_beyond_the_computed_set_is_refused(tmp_path):
    with pytest.raises(ValueError, match="only 3 modes are being computed"):
        solve(tmp_path, resolution=32, n_modes=3, mode=5)


def test_asking_for_more_modes_than_the_mesh_has_is_refused():
    """A mesh has as many modes as free degrees of freedom, and no more.

    Refused rather than truncated: a caller that asked for fifty and got eleven would have to
    notice, and the request is a mistake about the mesh rather than about the physics.

    Exercised on the helper rather than through the adapter, because the smallest grid the
    adapter accepts already has hundreds of degrees of freedom — the guard is for a caller who
    asks for fifty modes of something tiny, and that shape is easier to state directly.
    """
    stiffness = np.eye(4)
    mass = np.ones(4)
    with pytest.raises(ValueError, match="fewer than the 50 modes"):
        natural_frequencies(stiffness, mass, np.ones(4), 50)


# ------------------------------------------------- a mode is not an instant (ADR 0004)


def test_an_artifact_cannot_be_both_an_instant_and_a_mode():
    """The two indices are exclusive, and the model refuses the combination.

    A file that claimed both would be ordered two ways by two derived lists, and a consumer
    reading either would be told something the other denies. Refusing it is what keeps the
    1.7 index and the 1.14 one from becoming one ambiguous slot.
    """
    with pytest.raises(ValidationError, match="both an instant and a mode"):
        ArtifactRef(
            name="confused.vtk", content_type="model/vnd.vtk", size=1, url="/x", t=0.5, mode=2
        )


def test_the_context_refuses_the_same_combination(tmp_path):
    """Refused where a solver registers a file, not only where a result is serialised.

    The compact levels and the local API never build a `ResultEnvelope`, so a rule enforced
    only there is one half the callers walk past — the same reasoning that put the frame cap
    at registration in #86.
    """
    ctx = make_ctx(tmp_path)
    with pytest.raises(ValueError, match="both an instant and a mode"):
        ctx.artifact("confused.vtk", t=1.0, mode=1)


def test_a_result_indexes_its_modes_and_leaves_frames_empty(tmp_path):
    """The envelope derives both indices, and an eigensolve populates exactly one of them."""
    del tmp_path
    envelope = ResultEnvelope(
        job_id="j-1",
        kind="series1d",
        data={
            "name": "spectrum",
            "x": {"name": "mode", "unit": "1", "values": [1.0, 2.0]},
            "traces": [{"name": "frequency", "unit": "Hz", "values": [10.0, 20.0]}],
        },
        artifacts=[
            ArtifactRef(
                name="mode_2.vtk", content_type="model/vnd.vtk", size=1, url="/2", mode=2
            ),
            ArtifactRef(
                name="mode_1.vtk", content_type="model/vnd.vtk", size=1, url="/1", mode=1
            ),
        ],
    )
    assert envelope.frames == []
    assert [entry.mode for entry in envelope.modes] == [1, 2], "mode order, not list order"
    assert envelope.modes[0].artifact == "mode_1.vtk"


def test_the_mode_index_is_capped_like_the_time_index(tmp_path):
    """A list of references is cheap right up until it becomes the payload."""
    ctx = make_ctx(tmp_path)
    for index in range(MAX_MODES):
        ctx.artifact(f"mode_{index + 1}.vtk", mode=index + 1)
    with pytest.raises(ValueError, match=f"at most {MAX_MODES} mode shapes"):
        ctx.artifact("one_too_many.vtk", mode=MAX_MODES + 1)


def test_the_rigid_body_test_is_relative_to_the_spectrum():
    """The classifier both halves of the pair share, checked on numbers rather than a solve."""
    frequencies = np.array([1e-6, 2e-6, 3e-6, 500.0, 1400.0])
    assert count_rigid_body_modes(frequencies, 1e5) == 3
    # A restrained structure: nothing is near zero relative to the same reference.
    assert count_rigid_body_modes(np.array([87.0, 520.0, 1400.0]), 1e5) == 0


# --------------------------------------------------------------------- the load case


def test_a_load_case_restrains_and_cannot_push(tmp_path):
    """The restraints of an elasticity load case apply unchanged; its tractions are refused.

    Both halves matter. Sharing the restraint vocabulary is what lets one load case hold a
    bracket for a stress check *and* for its modes — the reuse #85 made a load case an object
    for. Refusing the traction is the other half of the same discipline: an eigensolve has no
    load, so a key it does not declare is a 422 at submit rather than a silent no-op.
    """
    beam = UNIFORM_BEAM.model_copy(
        update={
            "boundaries": [
                BoundarySpec(name="root", select=NearSelector(axis="x", value=0.0)),
            ]
        }
    )
    clamped = solve(tmp_path, geometry=beam, conditions={"root": {"fixed": 1.0}},
                    resolution=32, n_modes=4)
    assert clamped.metrics["rigid_body_modes"] == 0
    assert clamped.metrics["f_1"] == pytest.approx(beam_frequency(1.875104), rel=0.10)

    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    me = Principal(id="anonymous", quotas=Quotas())
    with pytest.raises(errors.UnknownConditionKey):
        asyncio.run(
            core.submit(
                "mock.modal2d",
                beam,
                {"resolution": 32, "n_modes": 4},
                me,
                conditions={"root": {"traction_y": -1.0e6}},
            )
        )


def test_the_pair_declares_the_restraints_the_elasticity_pair_does(tmp_path):
    """Shared objects, not a second spelling — so a clamp means one thing in both."""
    del tmp_path
    from fenixspoon.solvers.declarations import ELASTICITY_CONDITIONS

    modal = {spec.name for spec in MockModal2D.conditions}
    assert modal == {"fixed", "fixed_x", "fixed_y"}
    assert modal < {spec.name for spec in ELASTICITY_CONDITIONS}
    assert all(spec in ELASTICITY_CONDITIONS for spec in MockModal2D.conditions)
