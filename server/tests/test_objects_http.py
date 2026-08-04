"""The workspace over HTTP — protocol 1.10, ADR 0002.

Two acceptance criteria, and the second is the one the record calls the actual payoff.
:func:`test_a_geometry_is_created_patched_and_read_back_over_http` is the object lifecycle a
browser needs; :func:`test_a_job_can_be_submitted_by_design_reference` is what all of it is
*for* — a page that stops resending its geometry and starts naming it.

The third group is the one ADR 0002 named as the deliverable rather than the work.
Per-principal isolation on objects was already written and already correct before this branch;
what it had never been is *falsifiable*, because on a single-principal machine every caller is
the same caller. These tests are that check under pressure, one per verb.
"""

import time

import pytest
from fastapi.testclient import TestClient

from fenixspoon.main import create_app

GEOMETRY = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
FAST_PARAMS = {"resolution": 32, "iterations": 100, "report_every": 20}

ALICE = "sk-alice-secret"
BOB = "sk-bob-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.delenv("FENIXSPOON_API_KEYS", raising=False)
    monkeypatch.delenv("FENIXSPOON_MAX_OBJECTS", raising=False)
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def two_principals(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_API_KEYS", f"alice:{ALICE},bob:{BOB}")
    with TestClient(create_app()) as c:
        yield c


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def wait_terminal(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()
        if status["status"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def create(client, object_type: str, body: dict, headers=None, **extra):
    return client.post(
        f"/api/v1/objects/{object_type}", json={"body": body, **extra}, headers=headers or {}
    )


# ------------------------------------------------------------------------ acceptance


def test_a_geometry_is_created_patched_and_read_back_over_http(client):
    """The lifecycle a browser needs, in one test: create, read, patch, read the old one.

    The last step is the point of versioning. A patch writes the *next* revision, and the one
    it was computed from stays readable forever — which is what lets a result name the exact
    inputs it came from long after the design moved on.
    """
    created = create(client, "geometry", GEOMETRY, label="airfoil")
    assert created.status_code == 201, created.text
    made = created.json()
    assert made["ref"] == "geometry:g-1"
    assert made["pinned"] == "geometry:g-1@1"
    assert made["revision"] == 1

    head = client.get("/api/v1/objects/geometry/g-1")
    assert head.status_code == 200
    assert head.json()["body"]["obstacle"]["points"][1] == [0.35, 0.09]

    patched = client.patch(
        "/api/v1/objects/geometry/g-1",
        json={"patch": [{"op": "replace", "path": "/obstacle/points/1", "value": [0.35, 0.12]}]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["revision"] == 2

    # The moved point is the head's, and revision 1 still has the original — one control
    # point resent, not twenty, which is the whole argument for the workspace.
    assert client.get("/api/v1/objects/geometry/g-1").json()["body"]["obstacle"]["points"][1] == [
        0.35,
        0.12,
    ]
    pinned = client.get("/api/v1/objects/geometry/g-1", params={"revision": 1})
    assert pinned.status_code == 200
    assert pinned.json()["body"]["obstacle"]["points"][1] == [0.35, 0.09]

    assert client.get("/api/v1/objects/geometry/g-1/revisions").json()["revisions"] == [1, 2]


def test_a_job_can_be_submitted_by_design_reference(client):
    """What the object routes are *for*, and the part that shows only when you ask for it.

    A submission by reference records the object revisions it resolved, so `provenance` can
    answer "which design produced this picture". A submission that inlined its geometry never
    can — the same solve, and an answer that cannot be traced.
    """
    geometry = create(client, "geometry", GEOMETRY).json()["ref"]
    design = create(
        client,
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry, "params": FAST_PARAMS},
    ).json()["ref"]

    submitted = client.post("/api/v1/jobs", json={"design": design})
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    assert wait_terminal(client, job_id)["status"] == "done"

    provenance = client.get(f"/api/v1/jobs/{job_id}/summary", params={"levels": "provenance"})
    assert provenance.status_code == 200
    inputs = provenance.json()["provenance"]["inputs"]
    assert inputs["design"].startswith(f"{design}@")
    assert inputs["geometry"].startswith(f"{geometry}@")


def test_the_inline_form_still_works_and_the_two_are_not_mixed(client):
    """1.10 is additive: everything that worked at 1.9 works unchanged. And a request that
    carries both forms is refused rather than resolved by precedence — it holds two intents,
    and honouring one silently produces a job whose inputs are not what the caller thinks."""
    inline = client.post(
        "/api/v1/jobs",
        json={"solver": "mock.laplace2d", "geometry": GEOMETRY, "params": FAST_PARAMS},
    )
    assert inline.status_code == 202

    geometry = create(client, "geometry", GEOMETRY).json()["ref"]
    design = create(
        client, "design", {"solver": "mock.laplace2d", "geometry": geometry}
    ).json()["ref"]
    both = client.post(
        "/api/v1/jobs",
        json={"design": design, "solver": "mock.laplace2d", "geometry": GEOMETRY},
    )
    assert both.status_code == 422, both.text
    assert "not both" in both.text

    assert client.post("/api/v1/jobs", json={"solver": "mock.laplace2d"}).status_code == 422


# -------------------------------------------------------------------------- isolation
#
# ADR 0002's deliverable. The checks below were already correct before this branch — every
# workspace method takes an owner — but on a single-principal machine they were unfalsifiable.


def test_another_principals_object_is_not_readable(two_principals):
    ref = create(two_principals, "geometry", GEOMETRY, headers=auth(ALICE)).json()["ref"]

    mine = two_principals.get(f"/api/v1/objects/geometry/{ref.split(':')[1]}", headers=auth(ALICE))
    assert mine.status_code == 200

    theirs = two_principals.get(
        f"/api/v1/objects/geometry/{ref.split(':')[1]}", headers=auth(BOB)
    )
    assert theirs.status_code == 404, "somebody else's object must not be readable"
    assert "sk-alice" not in theirs.text and "alice" not in theirs.text.lower()


def test_another_principals_object_is_not_patchable_or_listable(two_principals):
    """404 rather than 403, deliberately: confirming that an id exists is itself a
    disclosure, and it is the answer jobs have always given."""
    create(two_principals, "geometry", GEOMETRY, headers=auth(ALICE))

    patch = two_principals.patch(
        "/api/v1/objects/geometry/g-1",
        json={"patch": [{"op": "replace", "path": "/bounds/0", "value": -2.0}]},
        headers=auth(BOB),
    )
    assert patch.status_code == 404

    revisions = two_principals.get(
        "/api/v1/objects/geometry/g-1/revisions", headers=auth(BOB)
    )
    assert revisions.status_code == 404
    assert two_principals.get("/api/v1/objects", headers=auth(BOB)).json() == []
    assert len(two_principals.get("/api/v1/objects", headers=auth(ALICE)).json()) == 1


def test_a_design_referencing_another_principals_geometry_cannot_be_solved(two_principals):
    """The interesting one, because it is the way round that is not obvious: a reference is
    just a string, so nothing stops Bob *writing* a design that names Alice's geometry. What
    stops him is that resolving it reads that geometry as Bob, and it is not there."""
    create(two_principals, "geometry", GEOMETRY, headers=auth(ALICE))
    design = create(
        two_principals,
        "design",
        {"solver": "mock.laplace2d", "geometry": "geometry:g-1", "params": FAST_PARAMS},
        headers=auth(BOB),
    )
    assert design.status_code == 201, "writing it is allowed; a reference is a string"

    solved = two_principals.post(
        "/api/v1/jobs", json={"design": design.json()["ref"]}, headers=auth(BOB)
    )
    assert solved.status_code == 404, "resolving it must not reach another principal's object"


# ----------------------------------------------------------------------------- refusals


def test_an_unknown_object_type_is_refused(client):
    refused = create(client, "gemoetry", GEOMETRY)
    assert refused.status_code == 422
    assert "geometry" in refused.text, "the message should list the real types"


def test_a_body_that_fails_its_schema_is_refused(client):
    """The types with a schema are validated at create, so a malformed geometry is refused
    here rather than at submit — which is the earliest moment anything can say so."""
    refused = create(client, "geometry", {"type": "domain2d", "bounds": [0.0, 0.0]})
    assert refused.status_code == 422


def test_a_url_whose_halves_disagree_is_refused_rather_than_looked_up(client):
    """`{type}/{id}` are the two halves of a reference and the server rebuilds it from its own
    path parameters — so `design/g-1` becomes `design:g-1`, and a design id does not start
    with `g`.

    ADR 0002 said this would 404 "honestly". The reference parser is stricter and better: it
    refuses the string as malformed, with a message naming both halves, because the two encode
    the same fact and a mismatch means the caller assembled it wrongly — "much better caught
    here than by a lookup that quietly finds nothing", as `parse_ref` has said since #44. The
    record was corrected to match the code rather than the other way round.
    """
    create(client, "geometry", GEOMETRY)
    refused = client.get("/api/v1/objects/design/g-1")
    assert refused.status_code == 422
    assert "design" in refused.text and "'d'" in refused.text


def test_a_patch_that_changes_nothing_and_one_aimed_at_a_revision_are_refused(client):
    create(client, "geometry", GEOMETRY)
    nothing = client.patch("/api/v1/objects/geometry/g-1", json={"patch": []})
    assert nothing.status_code == 409

    # A pinned reference is a view of history; writing "through" it would mean either
    # rewriting the past or branching, and neither is what a caller meant.
    pinned = client.patch(
        "/api/v1/objects/geometry/g-1%401",
        json={"patch": [{"op": "replace", "path": "/bounds/0", "value": -2.0}]},
    )
    assert pinned.status_code in (404, 409, 422)


def test_the_object_quota_refuses_the_one_over_and_says_waiting_will_not_help(
    tmp_path, monkeypatch
):
    """The gap ADR 0002 found by reading the code: three quotas counted jobs and artifact
    bytes, none counted objects, which is free and local until it is neither.

    No `Retry-After`, and the message says why: nothing about waiting deletes an object,
    where an hourly job window genuinely does roll.
    """
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.delenv("FENIXSPOON_API_KEYS", raising=False)
    monkeypatch.setenv("FENIXSPOON_MAX_OBJECTS", "2")
    with TestClient(create_app()) as client:
        assert create(client, "geometry", GEOMETRY).status_code == 201
        assert create(client, "geometry", GEOMETRY).status_code == 201

        refused = create(client, "geometry", GEOMETRY)
        assert refused.status_code == 429
        assert "Retry-After" not in refused.headers
        assert "does not relieve itself" in refused.text


# ---------------------------------------------------------------------------- listing


def test_the_listing_carries_no_bodies_and_filters_by_type(client):
    """Without bodies because a geometry body is the payload progressive iteration exists to
    stop moving: twenty designs would otherwise carry twenty outlines nobody asked for."""
    create(client, "geometry", GEOMETRY)
    create(client, "material", {"name": "aluminium", "properties": {"k": 167.0}})

    everything = client.get("/api/v1/objects").json()
    assert {row["type"] for row in everything} == {"geometry", "material"}
    assert all("body" not in row for row in everything)

    only = client.get("/api/v1/objects", params={"type": "material"}).json()
    assert [row["ref"] for row in only] == ["material:m-1"]
    assert client.get("/api/v1/objects", params={"type": "nonsense"}).status_code == 422


# ------------------------------------------------------------------ studies (1.11)
#
# ADR 0002 decision 3, and the shape it refused: #21 sketched `POST /sweeps` with the grid in
# the request body. A sweep sent that way is a computation with no identity — so the object is
# created like any other and these two routes act on it.


def sweep_body(design: str) -> dict:
    return {
        "kind": "sweep",
        "design": design,
        "axes": [{"parameter": "alpha", "values": [-4.0, 0.0, 4.0]}],
        "metrics": ["c_l"],
    }


def polar(client) -> str:
    geometry = create(client, "geometry", GEOMETRY).json()["ref"]
    design = create(
        client,
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry, "params": FAST_PARAMS},
    ).json()["ref"]
    return create(client, "study", sweep_body(design)).json()["ref"]


def completed(client, study_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = client.get(f"/api/v1/studies/{study_id}").json()
        if report["complete"]:
            return report
        time.sleep(0.05)
    raise TimeoutError("the sweep did not finish")


def test_a_sweep_runs_and_reports_from_the_browser_side(client):
    """#21's second half, and the first time a page can reach any of this.

    The report carries the response curve as `Series1DData` — which is not a shape invented
    for this route but the one `<fs-plot>` has drawn since 1.5, so the page needs no adapter
    between the two.
    """
    study_id = polar(client).split(":")[1]

    started = client.post(f"/api/v1/studies/{study_id}/run")
    assert started.status_code == 202, started.text
    assert started.json()["submitted"] == 3

    report = completed(client, study_id)
    assert report["kind"] == "sweep"
    assert [point["values"]["alpha"] for point in report["points"]] == [-4.0, 0.0, 4.0]
    assert all(point["status"] == "done" for point in report["points"])

    (curve,) = report["curves"]
    assert curve["name"] == "c_l"
    (trace,) = curve["traces"]
    assert trace["x"]["values"] == [-4.0, 0.0, 4.0]
    assert trace["values"][0] < trace["values"][-1], "lift rises with angle of attack"


def test_the_report_is_readable_before_the_run_and_free_afterwards(client):
    """Two properties a page depends on and neither is a special case.

    Before: there is no stored run record to be absent, so a report of an unrun study is a
    table of refused rows rather than a 404 — which is what lets a page render the shape
    before pressing anything. After: re-running is a cache hit per point, so a reload that
    re-posts costs nothing.
    """
    study_id = polar(client).split(":")[1]

    before = client.get(f"/api/v1/studies/{study_id}")
    assert before.status_code == 200
    assert [point["status"] for point in before.json()["points"]] == ["refused"] * 3

    client.post(f"/api/v1/studies/{study_id}/run")
    completed(client, study_id)

    again = client.post(f"/api/v1/studies/{study_id}/run")
    assert again.json() == {**again.json(), "submitted": 0, "reused": 3, "refused": 0}


def test_a_study_route_will_not_run_something_that_is_not_a_study(client):
    """The type is in the path, so `/studies/d-1` names `study:d-1` — refused by the
    reference parser for the same reason `/objects/design/g-1` is."""
    create(client, "geometry", GEOMETRY)
    assert client.post("/api/v1/studies/g-1/run").status_code == 422
    assert client.get("/api/v1/studies/s-99").status_code == 404


def test_another_principals_study_cannot_be_run_or_read(two_principals):
    geometry = create(two_principals, "geometry", GEOMETRY, headers=auth(ALICE)).json()["ref"]
    design = create(
        two_principals,
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry, "params": FAST_PARAMS},
        headers=auth(ALICE),
    ).json()["ref"]
    study = create(two_principals, "study", sweep_body(design), headers=auth(ALICE)).json()["ref"]
    study_id = study.split(":")[1]

    assert two_principals.post(
        f"/api/v1/studies/{study_id}/run", headers=auth(BOB)
    ).status_code == 404
    assert two_principals.get(f"/api/v1/studies/{study_id}", headers=auth(BOB)).status_code == 404
