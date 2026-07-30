"""Progressive capability discovery (#43).

Three of these are the issue's acceptance criteria: `capability.list` stays small,
`capability.describe` with one section returns that section and nothing else, and
`GET /solvers` keeps the payload it had.

The rest guard the declaration itself. Metrics, artifacts and the `availability` tag are
prose an adapter author writes by hand, so nothing but a test stops them describing a solve
that does not happen — `test_declared_metrics_reduce_a_field_the_solver_emits` and
`test_declared_artifacts_are_the_files_a_solve_writes` run a real solve and compare.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from geometries import SOLENOID

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.discovery import SECTIONS, schema_ref
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import Domain2D, Polygon2D
from fenixspoon.jobs import JobManager
from fenixspoon.main import create_app
from fenixspoon.protocol import PROTOCOL_VERSION
from fenixspoon.solvers import registered_solvers
from fenixspoon.solvers.base import Solver

AIRFOIL = Domain2D(
    bounds=(-1.0, -1.0, 2.0, 1.0),
    obstacle=Polygon2D(points=[(0.0, 0.0), (0.35, 0.09), (1.0, 0.0), (0.35, -0.06)]),
)

#: Params small enough to solve inside a test, per solver. Nothing here has to converge —
#: the assertions are about which *keys* come back, not which numbers.
SMOKE = {
    "mock.laplace2d": (AIRFOIL, {"resolution": 40, "iterations": 60}),
    "mock.magnetostatics2d": (SOLENOID, {"resolution": 40, "iterations": 60}),
    "mock.heat2d": (SOLENOID, {"resolution": 40, "iterations": 60}),
}

#: The FEniCSx half, exercised only by `pytest -m fenics` in the dolfinx image. Without
#: these, the declaration-versus-reality tests below would cover exactly the adapters whose
#: field names are easiest to check and skip the ones meshing is involved in — and since the
#: pairs share one metric declaration, a wrong field name would look correct on the half
#: that runs.
#:
#: The element sizes are each adapter's own "coarse mesh" example, which is the coarsest size
#: its author considered meshable — a figure picked freely here would either be slower than
#: needed or below a feature size (the solenoid's coils are 10 mm wide) and produce a
#: degenerate mesh rather than a useful failure.
FENICS_SMOKE = {
    "dolfinx.potential_flow2d": (AIRFOIL, {"mesh_size": 0.08, "output": "mesh2d"}),
    "dolfinx.magnetostatics2d": (SOLENOID, {"mesh_size": 0.01}),
    "dolfinx.heat2d": (SOLENOID, {"mesh_size": 0.004}),
}


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    with TestClient(create_app()) as test_client:
        yield test_client


def solve(core, me, solver, geometry, params):
    async def run():
        job = await core.submit(solver, geometry, {**params, "write_vtk": True}, me)
        deadline = asyncio.get_running_loop().time() + 60
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
            await asyncio.sleep(0.02)
        return core.job(job.id, me, with_result=True)

    return asyncio.run(run())


# --------------------------------------------------------------- acceptance criteria


def test_capability_list_stays_small(core):
    """The acceptance criterion: "`capability.list` on an installation with all solvers
    stays under a couple of kilobytes."

    Measured against the whole installed set, whatever it is: this file runs both with and
    without dolfinx, and the FEniCSx entries are the same size as the mock ones, so the
    budget holds either way.
    """
    payload = json.dumps([item.model_dump() for item in core.capability_list()])
    assert len(payload) < 2048, f"capability.list is {len(payload)} bytes"
    # And it is genuinely the compact one: the exhaustive route is much bigger for the
    # same installation, which is the whole reason this operation exists.
    exhaustive = json.dumps([info.model_dump() for info in core.capabilities()])
    assert len(payload) * 4 < len(exhaustive)


def test_one_section_returns_that_section_and_nothing_else(core):
    """The acceptance criterion: "`capability.describe` with sections: ["metrics"] returns
    metrics and nothing else."

    `name` is the one exception, so an answer can be identified without correlating it back
    to the request. Asserting on the key set rather than on a couple of fields is what makes
    this a real guard: a new section that forgets to be optional fails here.
    """
    payload = core.capability_describe("mock.heat2d", ["metrics"]).model_dump(exclude_none=True)
    assert set(payload) == {"name", "metrics"}
    assert [metric["name"] for metric in payload["metrics"]] == ["t_max", "t_rise", "flux_max"]


def test_the_solvers_route_is_unchanged(client):
    """#43 promised `GET /solvers` keeps its current response. Nothing may have been
    added to it, because a client reading `params_schema` out of a list of four keys is
    entitled to keep finding four keys."""
    payload = client.get("/api/v1/solvers").json()
    for entry in payload:
        assert set(entry) == {
            "name",
            "title",
            "description",
            "geometry_types",
            "params_schema",
        }


# ------------------------------------------------------------------ environment.inspect


def test_environment_reports_what_it_is(core, me):
    env = core.environment(me)
    assert env.protocol == PROTOCOL_VERSION
    assert env.execution_backend == "in-process"
    assert env.event_bus == "in-process"
    assert env.store == "sqlite"
    assert env.capabilities == len(registered_solvers())
    assert env.limits.max_cells == core.jobs.max_cells
    assert env.principal == "tester"
    # #47 has not landed, and saying so as an explicit null is the point: a caller can
    # tell "no cache on this server" from "this server is too old to have an opinion".
    assert env.cache is None


def test_environment_carries_no_schemas(core, me):
    """The spec says "a few hundred bytes, no schemas". The size is a budget; the absence
    is a rule."""
    payload = json.dumps(core.environment(me).model_dump())
    assert "params_schema" not in payload
    assert "properties" not in payload
    assert len(payload) < 2048, f"environment.inspect is {len(payload)} bytes"


def test_environment_reports_this_principals_quotas_and_usage(tmp_path):
    """Per-principal, because a caller wants its own ceiling and its own position.

    A quota with no usage beside it is half an answer: it says what the limit is and not
    whether the next submission will meet it.
    """
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    capped = Principal(id="capped", quotas=Quotas(jobs_per_hour=5))
    before = core.environment(capped)
    assert before.quotas.jobs_per_hour == 5
    assert before.usage.jobs_last_hour == 0

    solve(core, capped, "mock.laplace2d", AIRFOIL, {"resolution": 40, "iterations": 60})
    after = core.environment(capped)
    assert after.usage.jobs_last_hour == 1
    assert after.usage.artifact_bytes > 0
    # And another principal's position is not visible in it.
    assert core.environment(Principal(id="other", quotas=Quotas())).usage.jobs_last_hour == 0


def test_dolfinx_is_reported_as_imported_exactly_when_its_capabilities_exist(core, me):
    """The two answers come from different places — `sys.modules` and the registry — and
    a caller reading either one must reach the same conclusion."""
    env = core.environment(me)
    dolfinx = next(pkg for pkg in env.packages if pkg.name == "dolfinx")
    fenicsx_installed = any(cls.availability == "fenicsx" for cls in registered_solvers())
    assert dolfinx.imported == fenicsx_installed


# ----------------------------------------------------------------- capability.describe


def test_no_sections_returns_everything(core):
    payload = core.capability_describe("mock.laplace2d").model_dump(exclude_none=True)
    assert set(SECTIONS) <= set(payload)
    assert payload["title"] and payload["physics"] == "potential-flow"


def test_an_empty_section_list_returns_only_the_name(core):
    """Legal and useful: it is how a caller asks "does this exist" without paying for prose."""
    assert core.capability_describe("mock.laplace2d", []).model_dump(exclude_none=True) == {
        "name": "mock.laplace2d"
    }


def test_a_misspelled_section_is_refused_not_ignored(core):
    """Silently dropping it would let a caller conclude the capability has no metrics —
    a wrong answer arrived at quietly, which is worse than a refusal."""
    with pytest.raises(errors.UnknownSection) as caught:
        core.capability_describe("mock.laplace2d", ["metric"])
    assert caught.value.unknown == ["metric"]
    assert "metrics" in caught.value.known


def test_an_unknown_capability_is_an_unknown_capability(core):
    with pytest.raises(errors.UnknownCapability):
        core.capability_describe("nope", ["metrics"])
    with pytest.raises(errors.UnknownCapability):
        core.capability_schema("nope")


def test_the_params_section_references_the_schema_rather_than_carrying_it(core):
    section = core.capability_describe("mock.laplace2d", ["params"]).params
    assert section.json_schema is None
    assert section.schema_ref == schema_ref("mock.laplace2d")
    # …and the reference resolves to the same schema `/solvers` embeds.
    assert core.capability_schema("mock.laplace2d") == (
        core.capability("mock.laplace2d").Params.model_json_schema()
    )


def test_inline_schemas_is_opt_in(core):
    section = core.capability_describe(
        "mock.laplace2d", ["params"], inline_schemas=True
    ).params
    assert section.json_schema == core.capability_schema("mock.laplace2d")


def test_the_param_summary_resolves_enum_refs_so_a_caller_does_not_have_to(core):
    """A `Literal` param reaches a caller as `$ref` → `$defs` → `enum`. Following that is
    the indirection the flat summary exists to remove — it is not smaller than the schema,
    it just needs no resolution."""
    section = core.capability_describe("mock.laplace2d", ["params"]).params
    params = {p.name: p for p in section.params}
    assert params["output"].type == "enum"
    assert params["output"].choices == ["grid2d", "mesh2d"]
    assert params["output"].default == "grid2d"
    # Bounds survive the flattening, which is what lets a caller size a request.
    assert (params["resolution"].minimum, params["resolution"].maximum) == (16, 512)
    assert params["resolution"].required is False


def test_the_cost_section_says_whether_a_request_can_be_sized(core):
    cost = core.capability_describe("mock.laplace2d", ["cost"]).cost
    assert cost.estimates_cells is True
    assert cost.max_cells == core.jobs.max_cells
    assert cost.job_timeout_seconds == core.jobs.job_timeout


def test_an_adapter_without_an_estimate_says_so(core):
    """False is a real answer here — it means the wall-clock timeout is the only backstop —
    so it must come from the adapter rather than from a default nobody set."""

    class Bare(Solver):
        name = "test.bare"
        title = "No cost estimate"

        def solve(self, geometry, params, ctx):  # pragma: no cover - never run
            raise NotImplementedError

    assert Bare.estimates_cost() is False
    assert core.capability("mock.laplace2d").estimates_cost() is True


# ------------------------------------------------------- the declaration versus reality


def _requires(solver: str):
    """Skip a FEniCSx case where dolfinx is absent, rather than failing it."""
    if solver.startswith("dolfinx.") and solver not in {
        cls.name for cls in registered_solvers()
    }:
        pytest.skip("requires dolfinx (FEniCSx)")


@pytest.mark.parametrize(
    "solver",
    sorted(SMOKE)
    + [pytest.param(name, marks=pytest.mark.fenics) for name in sorted(FENICS_SMOKE)],
)
def test_declared_metrics_reduce_a_field_the_solver_emits(core, me, solver):
    """The guard that keeps the metric declaration from being aspirational.

    Issue #43 declares metrics and #46 computes them, which leaves a window in which a
    declaration can name a field no solve produces and nothing notices. Where a metric says
    it is a reduction of a field, that field must be in the result — so the two issues
    cannot disagree about what the field is called.
    """
    _requires(solver)
    geometry, params = {**SMOKE, **FENICS_SMOKE}[solver]
    job = solve(core, me, solver, geometry, params)
    assert job.status == "done", job.error
    result = core.result(job.id, me)
    emitted = set(result.data.get("fields") or result.data.get("point_fields") or {})
    assert emitted, "no fields came back at all, so the comparison below proves nothing"
    for metric in core.capability(solver).metrics:
        if metric.field is not None:
            assert metric.field in emitted, (
                f"{solver} declares metric {metric.name!r} over field {metric.field!r}, "
                f"but a solve emits {sorted(emitted)}"
            )


@pytest.mark.parametrize(
    "solver",
    sorted(SMOKE)
    + [pytest.param(name, marks=pytest.mark.fenics) for name in sorted(FENICS_SMOKE)],
)
def test_declared_artifacts_are_the_files_a_solve_writes(core, me, solver):
    _requires(solver)
    geometry, params = {**SMOKE, **FENICS_SMOKE}[solver]
    job = solve(core, me, solver, geometry, params)
    declared = {spec.name for spec in core.capability(solver).artifacts}
    written = {artifact.name for artifact in core.result(job.id, me).artifacts}
    assert written <= declared, f"{solver} wrote undeclared files: {sorted(written - declared)}"
    assert written, "the smoke params ask for the VTK, so something should have been written"


def test_a_metric_declares_a_reduction_exactly_when_it_declares_a_field():
    """The two travel together: a field with no reduction cannot be evaluated, and a
    reduction with no field has nothing to reduce. Either alone is a half-declaration
    #46 would have to guess at."""
    for cls in registered_solvers():
        for metric in cls.metrics:
            assert (metric.field is None) == (metric.reduction is None), (
                f"{cls.name} metric {metric.name!r}: field={metric.field!r} "
                f"reduction={metric.reduction!r}"
            )


def test_metric_names_are_unique_within_a_capability():
    for cls in registered_solvers():
        names = [metric.name for metric in cls.metrics]
        assert len(names) == len(set(names)), f"{cls.name} declares a metric name twice"


def test_paired_adapters_declare_the_same_metrics():
    """A mock and its FEniCSx counterpart are cross-validated against each other, so they
    must answer the same question by the same name. Otherwise the pair is interchangeable
    in the gallery and not in a caller's code."""
    by_physics: dict[str, list[type[Solver]]] = {}
    for cls in registered_solvers():
        by_physics.setdefault(cls.physics, []).append(cls)
    for physics, adapters in by_physics.items():
        vocabularies = {tuple(m.name for m in cls.metrics) for cls in adapters}
        assert len(vocabularies) == 1, (
            f"adapters for {physics} declare different metrics: "
            + ", ".join(f"{cls.name}={[m.name for m in cls.metrics]}" for cls in adapters)
        )


def test_every_shipped_adapter_declares_its_physics_and_availability():
    """The defaults exist so a third-party adapter written against 1.1 keeps working. They
    are not for the adapters in this repository."""
    for cls in registered_solvers():
        assert cls.physics != "unspecified", f"{cls.name} declares no physics"
        assert cls.availability in ("mock", "fenicsx"), f"{cls.name}: {cls.availability!r}"


def test_availability_matches_which_module_the_adapter_lives_in():
    """A declared string can drift from the truth; the module it sits in cannot. `dolfinx_*`
    modules only load when dolfinx imported, so this ties the claim to that fact."""
    for cls in registered_solvers():
        module = cls.__module__.rsplit(".", 1)[-1]
        expected = "fenicsx" if module.startswith("dolfinx_") else "mock"
        assert cls.availability == expected, f"{cls.name} in {module} says {cls.availability!r}"


def test_declared_examples_validate_against_the_params_schema():
    """An example that does not parse is worse than no example: a caller copies it and gets
    a 422 with no idea the server shipped it that way."""
    for cls in registered_solvers():
        for example in cls.examples:
            cls.Params.model_validate(example.params)


# ------------------------------------------------------------------------ HTTP binding


def test_the_http_routes_bind_the_three_operations(client):
    assert client.get("/api/v1/environment").json()["protocol"] == PROTOCOL_VERSION

    summaries = client.get("/api/v1/capabilities").json()
    assert {item["name"] for item in summaries} >= {"mock.laplace2d", "mock.heat2d"}

    described = client.get(
        "/api/v1/capabilities/mock.heat2d", params={"sections": ["metrics"]}
    ).json()
    # The route sets `response_model_exclude_none`, which is what makes "nothing else"
    # literal rather than "nothing else, plus eight nulls".
    assert set(described) == {"name", "metrics"}

    schema = client.get("/api/v1/capabilities/mock.laplace2d/schema").json()
    assert schema["properties"]["resolution"]["default"] == 128


def test_http_reports_a_bad_section_as_422_and_an_unknown_capability_as_404(client):
    bad_section = client.get("/api/v1/capabilities/mock.heat2d", params={"sections": ["nope"]})
    assert bad_section.status_code == 422
    assert "nope" in bad_section.json()["detail"]
    assert client.get("/api/v1/capabilities/nope").status_code == 404
    assert client.get("/api/v1/capabilities/nope/schema").status_code == 404


def test_discovery_is_behind_the_auth_gate(tmp_path, monkeypatch):
    """`/version` is the only route outside it. These three describe the installation —
    its paths, its topology, its limits — which is not information to hand to a stranger."""
    monkeypatch.setenv("FENIXSPOON_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("FENIXSPOON_API_KEYS", "alice:sk-alice")
    with TestClient(create_app()) as guarded:
        for path in (
            "/api/v1/environment",
            "/api/v1/capabilities",
            "/api/v1/capabilities/mock.laplace2d",
            "/api/v1/capabilities/mock.laplace2d/schema",
        ):
            assert guarded.get(path).status_code == 401, path
            assert guarded.get(path, headers={"X-API-Key": "sk-alice"}).status_code == 200, path
        assert guarded.get("/api/v1/version").status_code == 200
