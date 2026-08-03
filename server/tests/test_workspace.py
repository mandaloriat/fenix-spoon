"""The local workspace and object references (#44).

The acceptance criterion is one test — :func:`test_the_iteration_loop_survives_the_process`:
create a design, close the process, reopen the workspace, patch one geometry parameter and
submit a solve, all by id and with no geometry payload re-sent. It runs the reopen for real
by building a second core over the same directory, because "reopenable" is a property of the
files rather than of an object graph that happens to still be in memory.

The rest are the decisions #44 asked to be *recorded*, turned into things that fail if the
decision is quietly reversed: JSON Patch over merge patch (the moving-control-point case),
revisions immutable, objects outliving the job TTL, and the thin types staying thin.
"""

import asyncio
import json

import pytest
from geometries import SOLENOID

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.core.workspace import Workspace
from fenixspoon.jobs import JobManager
from fenixspoon.objects import OBJECT_TYPES, ObjectFileStore, parse_ref

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}

FAST = {"resolution": 40, "iterations": 60, "write_vtk": False}


def build_core(data_dir):
    """A core over a directory — call it twice on the same path to simulate a restart."""
    return FenixSpoonCore(JobManager(data_dir=data_dir))


@pytest.fixture()
def core(tmp_path):
    return build_core(tmp_path / "workspace")


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


def solve_design(core, design_ref, me):
    """Submit a design and wait for it, in one event loop.

    Both halves have to share a loop: the in-process backend finishes a job through a
    callback scheduled on the loop that submitted it, so waiting inside a *second*
    `asyncio.run` watches a status that can never move.
    """

    async def run():
        job = await core.submit_design(design_ref, me)
        deadline = asyncio.get_running_loop().time() + 60
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return core.job(job.id, me)

    return asyncio.run(run())


def make_design(core, me, geometry=None, params=None):
    geometry_ref = core.create_object("geometry", geometry or AIRFOIL, me, label="airfoil").ref
    design = core.create_object(
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry_ref, "params": params or FAST},
        me,
        label="baseline",
    )
    return geometry_ref, design.ref


# ------------------------------------------------------------------ acceptance criterion


def test_the_iteration_loop_survives_the_process(tmp_path):
    """#44's acceptance criterion, end to end.

    "Create a design, close the process, reopen the workspace, patch one geometry parameter
    and submit a solve — all by id, with no geometry payload re-sent."
    """
    data_dir = tmp_path / "workspace"
    me = Principal(id="tester", quotas=Quotas())

    first = build_core(data_dir)
    geometry_ref, design_ref = make_design(first, me)
    first.jobs.store.close()

    # A second core over the same directory. Nothing is carried over in memory.
    reopened = build_core(data_dir)
    assert reopened.workspace_info().objects == {"geometry": 1, "design": 1}

    # One control point moves. This is the entire request body for the edit.
    patch = [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.14]}]
    updated = reopened.patch_object(geometry_ref, patch, me)
    assert updated.revision == 2
    assert updated.body["obstacle"]["points"][1] == [0.35, 0.14]
    # …and the original is still readable, which is what "versioned, not mutated" buys.
    assert reopened.object(f"{geometry_ref}@1", me).body["obstacle"]["points"][1] == [0.35, 0.09]

    job = solve_design(reopened, design_ref, me)
    assert job.status == "done"

    # The job records exactly which revisions it ran on — the design -> job relation.
    assert reopened.job(job.id, me).inputs == {
        "solver": "mock.laplace2d",
        "design": f"{design_ref}@1",
        "geometry": f"{geometry_ref}@2",
        "materials": [],
        "boundary_conditions": [],
        "load_cases": [],
    }
    assert reopened.result(job.id, me).kind == "grid2d"


def test_submitting_a_design_sends_no_geometry(core, me):
    """The point of the whole milestone, asserted as an argument list.

    `submit_design` takes one string. There is nowhere in its signature for a geometry to
    be passed, which is a stronger guarantee than measuring a payload.
    """
    _, design_ref = make_design(core, me)
    assert solve_design(core, design_ref, me).status == "done"
    assert isinstance(design_ref, str) and len(design_ref) < 32


# ----------------------------------------------------------------------- the patch format


def test_json_patch_moves_one_point_where_merge_patch_would_resend_them_all(core, me):
    """The case #44 named as the decider between JSON Patch and JSON Merge Patch.

    Merge patch has no operation for "element 1 of this array": it replaces arrays whole, so
    the equivalent edit carries every point. The assertion is the size difference, because
    that is the entire reason the format was chosen.
    """
    geometry_ref, _ = make_design(core, me)
    patch = [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.14]}]
    merge_equivalent = {
        "obstacle": {"points": [[0.0, 0.0], [0.35, 0.14], [1.0, 0.0], [0.35, -0.06]]}
    }
    assert len(json.dumps(patch)) < len(json.dumps(merge_equivalent))

    updated = core.patch_object(geometry_ref, patch, me)
    assert updated.body["obstacle"]["points"] == merge_equivalent["obstacle"]["points"]


def test_a_patch_that_breaks_the_schema_is_refused(core, me):
    """Validation is not skipped on the patch path. A geometry that cannot be solved must
    not be reachable by editing one that could."""
    geometry_ref, _ = make_design(core, me)
    with pytest.raises(errors.InvalidObject) as caught:
        core.patch_object(geometry_ref, [{"op": "remove", "path": "/obstacle"}], me)
    assert caught.value.object_type == "geometry"
    assert caught.value.errors  # pydantic's structured list, for a caller that fixes fields
    # Nothing was written: the head is still revision 1.
    assert core.object(geometry_ref, me).revision == 1


def test_a_patch_that_does_not_apply_says_so(core, me):
    geometry_ref, _ = make_design(core, me)
    with pytest.raises(errors.InvalidPatch):
        core.patch_object(geometry_ref, [{"op": "replace", "path": "/nope", "value": 1}], me)
    with pytest.raises(errors.InvalidPatch):
        core.patch_object(geometry_ref, [{"op": "nonsense", "path": "/x"}], me)


def test_a_patch_that_changes_nothing_is_refused_rather_than_written(core, me):
    """A no-op revision would move the head, so every reference pinned afterwards names a
    new number for identical content. The caller also probably meant to change something."""
    geometry_ref, _ = make_design(core, me)
    same = core.object(geometry_ref, me).body["obstacle"]["points"][1]
    with pytest.raises(errors.PatchChangedNothing):
        core.patch_object(geometry_ref, [{"op": "replace", "path": "/obstacle/points/1",
                                          "value": same}], me)
    assert core.object(geometry_ref, me).revision == 1


def test_a_pinned_revision_cannot_be_patched(core, me):
    """Revisions are immutable and there are no branches, so this has no honest meaning."""
    geometry_ref, _ = make_design(core, me)
    with pytest.raises(errors.CannotPatchRevision):
        core.patch_object(f"{geometry_ref}@1", [{"op": "replace", "path": "/bounds/0",
                                                 "value": -2.0}], me)


# -------------------------------------------------------------------- revisions and refs


def test_revisions_accumulate_and_stay_readable(core, me):
    geometry_ref, _ = make_design(core, me)
    for i, y in enumerate([0.10, 0.11, 0.12], start=2):
        assert core.patch_object(
            geometry_ref, [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, y]}], me
        ).revision == i
    assert core.object_revisions(geometry_ref, me) == [1, 2, 3, 4]
    assert core.object(geometry_ref, me).revision == 4  # unpinned means head
    assert core.object(f"{geometry_ref}@2", me).body["obstacle"]["points"][1] == [0.35, 0.10]


def test_a_revision_file_is_never_rewritten(tmp_path):
    """Immutability enforced by O_EXCL rather than by convention — the reason a stale head
    cannot silently overwrite somebody else's revision."""
    store = ObjectFileStore(tmp_path)
    record = store.create("geometry", "tester", AIRFOIL)
    with pytest.raises(FileExistsError):
        store.revise(record)


def test_a_stray_directory_does_not_break_the_listing(tmp_path):
    """Raised in review of #66. A workspace is meant to be browsed, committed and poked at,
    so a `tmp/` or a mistyped folder under `objects/geometry/` is a thing that happens — and
    it used to raise `ValueError` out of `parse_ref` and take the whole listing with it.

    The id allocator already skipped what it could not parse; listing now matches it.
    """
    store = ObjectFileStore(tmp_path)
    store.create("geometry", "me", AIRFOIL)
    (store.root / "geometry" / "tmp").mkdir()
    (store.root / "geometry" / ".ipynb_checkpoints").mkdir()
    assert [record.ref for record in store.list_heads()] == ["geometry:g-1"]
    # …and allocation still lands on the next real number rather than counting the strays.
    assert store.create("geometry", "me", AIRFOIL).ref == "geometry:g-2"


def test_ids_are_readable_and_allocated_in_sequence(tmp_path):
    store = ObjectFileStore(tmp_path)
    assert [store.create("geometry", "me", AIRFOIL).ref for _ in range(3)] == [
        "geometry:g-1",
        "geometry:g-2",
        "geometry:g-3",
    ]
    # Numbering is per type, so a design does not inherit the geometry counter.
    assert store.create("design", "me", {}).ref == "design:d-1"


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ("g-1", "no type"),
        ("geometry:1", "no prefix"),
        ("nope:x-1", "unknown type"),
        ("geometry:d-1", "type and prefix disagree"),
        ("geometry:g-1@0", "revisions start at 1"),
        ("geometry:g-1@x", "revision is not a number"),
    ],
)
def test_malformed_references_are_rejected_by_the_parser(ref, reason):
    del reason
    with pytest.raises(ValueError):
        parse_ref(ref)


def test_a_malformed_reference_is_a_different_error_from_a_missing_one(core, me):
    """They send a caller to different places: one built the string wrong, the other is
    looking in a workspace that does not hold what it expected."""
    with pytest.raises(errors.MalformedReference):
        core.object("not-a-reference", me)
    with pytest.raises(errors.ObjectNotFound):
        core.object("geometry:g-999", me)


def test_another_principals_objects_are_not_found_rather_than_forbidden(core, me):
    geometry_ref, design_ref = make_design(core, me)
    stranger = Principal(id="stranger", quotas=Quotas())
    with pytest.raises(errors.ObjectNotFound):
        core.object(geometry_ref, stranger)
    with pytest.raises(errors.ObjectNotFound):
        core.patch_object(geometry_ref, [{"op": "replace", "path": "/bounds/0", "value": 0.0}],
                          stranger)
    with pytest.raises(errors.ObjectNotFound):
        asyncio.run(core.submit_design(design_ref, stranger))
    assert core.objects(stranger) == []


# ------------------------------------------------------------------------------ designs


def test_a_design_pins_the_head_at_submit_time(core, me):
    """An unpinned design follows the geometry, and freezes it when it runs. Otherwise two
    solves of "the same design" could mean two shapes with nothing recording which."""
    geometry_ref, design_ref = make_design(core, me)
    assert core.resolve_design(design_ref, me).geometry == f"{geometry_ref}@1"
    core.patch_object(
        geometry_ref, [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.2]}], me
    )
    assert core.resolve_design(design_ref, me).geometry == f"{geometry_ref}@2"


def test_a_design_can_pin_a_geometry_revision_and_then_stops_following_it(core, me):
    geometry_ref = core.create_object("geometry", AIRFOIL, me).ref
    design_ref = core.create_object(
        "design",
        {"solver": "mock.laplace2d", "geometry": f"{geometry_ref}@1", "params": FAST},
        me,
    ).ref
    core.patch_object(
        geometry_ref, [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.2]}], me
    )
    assert core.resolve_design(design_ref, me).geometry == f"{geometry_ref}@1"


def test_a_design_pointing_at_the_wrong_type_says_which(core, me):
    """More useful than "not found": the caller passed a real id of the wrong kind."""
    material_ref = core.create_object(
        "material", {"name": "iron", "properties": {"mu_r": 1000.0}}, me
    ).ref
    design_ref = core.create_object(
        "design", {"solver": "mock.laplace2d", "geometry": material_ref}, me
    ).ref
    with pytest.raises(errors.WrongObjectType) as caught:
        core.resolve_design(design_ref, me)
    assert (caught.value.expected, caught.value.got) == ("geometry", "material")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("materials", "material"),
        ("boundary_conditions", "boundary_condition"),
        ("load_cases", "load_case"),
    ],
)
def test_every_reference_list_is_type_checked_not_just_the_geometry(core, me, field, expected):
    """Raised in review of #66, and true of all three lists rather than only `materials`.

    A design naming a geometry under `materials` used to resolve happily and record
    provenance that was wrong in a way nothing would ever surface — the reference resolved,
    so no error, and `inputs.materials` then said a geometry was a material. The thin types
    are the likeliest to be miswired, because nothing validates their bodies either.
    """
    geometry_ref = core.create_object("geometry", AIRFOIL, me).ref
    design_ref = core.create_object(
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry_ref, field: [geometry_ref]},
        me,
    ).ref
    with pytest.raises(errors.WrongObjectType) as caught:
        core.resolve_design(design_ref, me)
    assert (caught.value.expected, caught.value.got) == (expected, "geometry")


def test_a_design_pins_every_reference_it_names(core, me):
    """Provenance covers the thin types too, even though no solver consumes them yet:
    what the design named is what the job should record it ran with."""
    geometry_ref = core.create_object("geometry", AIRFOIL, me).ref
    material_ref = core.create_object(
        "material", {"name": "iron", "properties": {"mu_r": 1000.0}}, me
    ).ref
    bc_ref = core.create_object("boundary_condition", {"kind": "fixed"}, me).ref
    design_ref = core.create_object(
        "design",
        {
            "solver": "mock.laplace2d",
            "geometry": geometry_ref,
            "materials": [material_ref],
            "boundary_conditions": [bc_ref],
        },
        me,
    ).ref
    resolved = core.resolve_design(design_ref, me)
    assert resolved.materials == [f"{material_ref}@1"]
    assert resolved.boundary_conditions == [f"{bc_ref}@1"]
    assert resolved.load_cases == []


def test_a_design_naming_a_missing_reference_is_refused(core, me):
    """A design is only submittable if every reference resolves — otherwise "this design
    works" is a claim about the two fields that happen to feed the solver."""
    geometry_ref = core.create_object("geometry", AIRFOIL, me).ref
    design_ref = core.create_object(
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry_ref, "materials": ["material:m-99"]},
        me,
    ).ref
    with pytest.raises(errors.ObjectNotFound):
        core.resolve_design(design_ref, me)


def test_submitting_a_design_still_validates_params_and_the_geometry_kind(core, me):
    """The workspace is a different door, not a bypass. Every submit-time rule still runs."""
    geometry_ref = core.create_object("geometry", AIRFOIL, me).ref
    bad_params = core.create_object(
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry_ref, "params": {"resolution": -5}},
        me,
    ).ref
    with pytest.raises(errors.InvalidParams):
        asyncio.run(core.submit_design(bad_params, me))

    wrong_kind = core.create_object(
        "design", {"solver": "mock.magnetostatics2d", "geometry": geometry_ref}, me
    ).ref
    with pytest.raises(errors.GeometryKindMismatch):
        asyncio.run(core.submit_design(wrong_kind, me))


def test_a_design_submitted_by_a_capped_principal_still_meets_the_quota(tmp_path):
    core = build_core(tmp_path / "workspace")
    capped = Principal(id="capped", quotas=Quotas(jobs_per_hour=1))
    _, design_ref = make_design(core, capped)

    async def run():
        # Two different designs. Resubmitting *one* design is a cache hit, which costs no
        # compute and so no quota (#47) — asserted deliberately in `test_cache.py`.
        await core.submit_design(design_ref, capped)
        _, other = make_design(core, capped, params={**FAST, "u_inf": 2.0})
        with pytest.raises(errors.QuotaExceeded):
            await core.submit_design(other, capped)

    asyncio.run(run())


# ------------------------------------------------------------------------- object types


def test_the_validated_types_are_validated(core, me):
    with pytest.raises(errors.InvalidObject):
        core.create_object("geometry", {"type": "domain2d"}, me)  # no bounds, no obstacle
    with pytest.raises(errors.InvalidObject):
        core.create_object("material", {"properties": {"k": 205.0}}, me)  # no name
    with pytest.raises(errors.InvalidObject):
        core.create_object("design", {"solver": "mock.laplace2d"}, me)  # no geometry


def test_the_thin_types_are_stored_exactly_as_given(core, me):
    """#44 asked for this to be explicit rather than papered over with an invented schema.

    No shipped solver has a boundary condition separable from its params in a way this type
    would capture, so there is nothing to validate against yet. Storing the body faithfully —
    including a shape nobody has agreed on — is the honest behaviour until a capability needs
    one.

    Two types have left this list since, and the pattern is the same both times: `study` in
    #48 and `load_case` in #85, each at the moment it had a concrete use case to generalise
    from rather than an imaginable one. `boundary_condition` still does not, so it is still
    here, and the assertions below keep that a decision rather than an omission.
    """
    body = {"kind": "whatever-the-caller-decided", "nested": {"values": [1, 2, 3]}}
    stored = core.create_object("boundary_condition", body, me)
    assert stored.body == body
    assert core.object(stored.ref, me).body == body

    for typed in ("study", "load_case"):
        with pytest.raises(errors.InvalidObject):
            core.create_object(typed, {"kind": "whatever-the-caller-decided"}, me)


def test_an_unknown_object_type_is_refused(core, me):
    with pytest.raises(errors.UnknownObjectType) as caught:
        core.create_object("spaceship", {}, me)
    assert set(caught.value.known) == set(OBJECT_TYPES)


def test_listing_omits_bodies_and_can_filter_by_type(core, me):
    make_design(core, me)
    core.create_object("material", {"name": "iron", "properties": {"mu_r": 1000.0}}, me)

    everything = core.objects(me)
    assert {item.type for item in everything} == {"geometry", "design", "material"}
    assert not any(hasattr(item, "body") for item in everything)
    assert len(json.dumps([item.model_dump() for item in everything])) < 1024

    assert [item.ref for item in core.objects(me, "design")] == ["design:d-1"]
    with pytest.raises(errors.UnknownObjectType):
        core.objects(me, "spaceship")


# ------------------------------------------------------------------------------ lifetime


def test_objects_outlive_the_job_retention_sweep(tmp_path):
    """The decision #44 asked for: `FENIXSPOON_JOB_TTL` is wrong for a design somebody has
    been iterating on for a week, so it does not apply to objects at all.

    A job is a computation and losing it costs a re-run; an object is authored input. The
    consequence — a workspace that outlives the results computed from it — is the right
    direction, because what survives is enough to recompute.
    """
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "workspace", job_ttl=0.0001))
    me = Principal(id="tester", quotas=Quotas())
    geometry_ref, design_ref = make_design(core, me)
    job = solve_design(core, design_ref, me)

    assert core.jobs.purge_expired() == [job.id]
    with pytest.raises(errors.JobNotFound):
        core.job(job.id, me)
    # The inputs are still there, and still solvable.
    assert core.object(geometry_ref, me).revision == 1
    assert core.resolve_design(design_ref, me).solver == "mock.laplace2d"


def test_the_workspace_lives_in_the_data_directory_and_nowhere_else(tmp_path):
    """The constraint #44 fixed even while leaving the storage format open: "one directory
    to mount, back up or delete"."""
    data_dir = tmp_path / "workspace"
    core = build_core(data_dir)
    me = Principal(id="tester", quotas=Quotas())
    make_design(core, me)
    written = {p.relative_to(data_dir).parts[0] for p in data_dir.rglob("*") if p.is_file()}
    assert written <= {"objects", "jobs.db", "jobs.db-wal", "jobs.db-shm"}


def test_the_files_are_the_thing_a_human_would_read(tmp_path):
    """Why files and not rows: the criterion in the design draft was whether a workspace is
    meant to be committed to a repository. It is, so a revision has to be a readable,
    diffable, self-describing file rather than a row somebody needs a tool to see."""
    data_dir = tmp_path / "workspace"
    me = Principal(id="tester", quotas=Quotas())
    core = build_core(data_dir)
    make_design(core, me)

    path = data_dir / "objects" / "geometry" / "g-1" / "00001.json"
    raw = json.loads(path.read_text())
    # Self-describing: the file says what it is without its path being parsed.
    assert raw["ref"] == "geometry:g-1"
    assert raw["type"] == "geometry" and raw["revision"] == 1
    assert raw["body"] == AIRFOIL
    # Stable formatting, so a diff shows the edit rather than a reordering.
    assert path.read_text() == json.dumps(raw, indent=2, sort_keys=True) + "\n"


def test_a_workspace_is_usable_without_the_core(tmp_path):
    """The store is a plain object over a directory. A script that wants to walk a checked-in
    workspace does not have to build a `JobManager` to do it."""
    workspace = Workspace(ObjectFileStore(tmp_path / "elsewhere"))
    created = workspace.create("geometry", AIRFOIL, "tester", label="airfoil")
    assert created.ref == "geometry:g-1"
    assert workspace.get("geometry:g-1", "tester").label == "airfoil"
    assert workspace.info().objects == {"geometry": 1}


def test_a_regions2d_geometry_round_trips_too(core, me):
    """Both geometry kinds, because the workspace validates against the union rather than
    against `domain2d`, and only one of them is used everywhere else in this file."""
    ref = core.create_object("geometry", SOLENOID.model_dump(), me).ref
    design_ref = core.create_object(
        "design",
        {"solver": "mock.magnetostatics2d", "geometry": ref,
         "params": {"resolution": 40, "iterations": 60, "write_vtk": False}},
        me,
    ).ref
    assert solve_design(core, design_ref, me).status == "done"
