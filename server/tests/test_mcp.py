"""Model Context Protocol adapter (#49).

The acceptance criterion has two halves and they pull in opposite directions: *an MCP host
connects, discovers the tool set and runs the vertical-slice loop*, **and** *the core's test
suite still passes in an environment where the MCP package is not installed*. The first is
:func:`test_the_vertical_slice_runs_through_the_tool_vocabulary`; the second is
:func:`test_nothing_but_the_adapter_imports_mcp`, which is the only test here that runs when
the extra is absent — everything above it is skipped by the `importorskip` below.

The groups: **vocabulary** (what a host sees, and what it deliberately does not), **delegation**
(that a tool is an RPC method plus a schema, so the two transports cannot diverge), **errors**
(that a domain failure is a tool result a model can act on rather than a protocol fault), and
**resources** (artifacts, and the one place this refuses to do what MCP would technically
allow).
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fenixspoon.core import FenixSpoonCore
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.jobs import JobManager

REPO_SERVER = Path(__file__).resolve().parents[1]

AIRFOIL = {
    "type": "domain2d",
    "bounds": [-1.0, -1.0, 2.0, 1.0],
    "obstacle": {
        "type": "polygon2d",
        "points": [[0.0, 0.0], [0.35, 0.09], [1.0, 0.0], [0.35, -0.06]],
    },
}
FAST = {"resolution": 40, "iterations": 200, "write_vtk": False}


# ------------------------------------------------------------------ optional extra


def test_nothing_but_the_adapter_imports_mcp():
    """The half of the acceptance criterion that is about what is *not* there.

    "MCP must not become a dependency of the core." Asserted in a subprocess for the reason
    the FastAPI purity tests use one: this test process may well have `mcp` installed — it
    does in CI's mcp job and in any developer environment that ran `pip install -e '.[mcp]'` —
    so an in-process `sys.modules` check would pass no matter what the server imports.

    Note this is the one test in this module that is *not* skipped when the extra is missing.
    It is exactly the case it exists to protect.
    """
    code = (
        "import sys; import fenixspoon.main, fenixspoon.rpc, fenixspoon.core; "
        "sys.exit(1 if 'mcp' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_SERVER, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"importing the server pulled in mcp; it is an optional extra\n{result.stderr}"
    )


def test_the_cli_says_how_to_install_the_extra_rather_than_crashing():
    """A missing optional extra is an ordinary outcome of running the command, not a bug.

    Run with an import hook that hides `mcp`, so this holds in an environment that *has* the
    extra — otherwise the test would silently stop checking anything the moment CI installed
    it.
    """
    # `ModuleNotFoundError` with `name` set, because that is what Python actually raises for
    # an absent package — and `name` is exactly what the CLI switches on. A hook that raised a
    # bare `ImportError` would be simulating something that never happens, and would have this
    # test failing over the difference rather than over the behaviour.
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'mcp' or name.startswith('mcp.'):\n"
        "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from fenixspoon.cli import main\n"
        "sys.exit(main(['mcp', '--stdio']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_SERVER, capture_output=True, text=True
    )
    assert result.returncode == 3, result.stderr
    assert "pip install" in result.stderr
    assert "Traceback" not in result.stderr, "a missing extra should not be a traceback"


def test_a_bug_inside_the_adapter_is_not_reported_as_a_missing_extra():
    """Catching every `ImportError` would send whoever hit a real bug to install something
    they already have. Only a failure to import `mcp` itself means the extra is absent.

    Simulated by making `mcp_adapter` fail on a *different* module, which is what a typo in an
    import or a moved dependency would look like. Raised in review of #49.
    """
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'numpy':\n"
        "            raise ModuleNotFoundError('No module named numpy', name='numpy')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from fenixspoon.cli import main\n"
        "sys.exit(main(['mcp', '--stdio']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_SERVER, capture_output=True, text=True
    )
    assert result.returncode != 3, "a bug inside the adapter was reported as a missing extra"
    assert "pip install" not in result.stderr


mcp_types = pytest.importorskip("mcp.types", reason="requires the `mcp` extra")

from fenixspoon import mcp_adapter  # noqa: E402  (after the skip, on purpose)

pytestmark = pytest.mark.mcp


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


async def acall(core, me, tool, arguments=None):
    return await mcp_adapter.call_tool(core, me, tool, arguments or {})


async def aok(core, me, tool, arguments=None):
    result = await acall(core, me, tool, arguments)
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def call(core, me, tool, arguments=None):
    """One tool call in its own loop. Only for calls that start no job — see `finished`."""
    return asyncio.run(acall(core, me, tool, arguments))


def ok(core, me, tool, arguments=None):
    result = call(core, me, tool, arguments)
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def finished(core, me, job_id, timeout: float = 60.0) -> str:
    """Poll a job to `done` — *inside* the caller's loop, which is the whole point.

    A solve is completed by callbacks on the event loop that submitted it. Polling from a
    fresh `asyncio.run` per call closes that loop under the running solve, so the status never
    moves and the loop spins until the deadline. The same constraint shapes the harnesses in
    `test_rpc.py` and `test_studies.py`; it is a property of the in-process backend, not of
    any one transport.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = (await aok(core, me, "inspect_job", {"job_id": job_id}))["status"]
        if status in ("done", "failed", "cancelled"):
            assert status == "done", status
            return job_id
        assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------------ acceptance


def test_the_vertical_slice_runs_through_the_tool_vocabulary(core, me):
    """Discover, design, solve, read — using only tools a host would see.

    This is the loop the milestone exists for, expressed the way an MCP host would drive it:
    no core calls, no RPC frames, nothing but tool names and JSON arguments.
    """
    async def slice_():
        environment = await aok(core, me, "inspect_environment")
        assert environment["capabilities"] >= 3

        capabilities = await aok(core, me, "list_capabilities")
        assert any(c["name"] == "mock.laplace2d" for c in capabilities)

        described = await aok(core, me, "describe_capability",
                              {"name": "mock.laplace2d", "sections": ["params", "metrics"]})
        assert set(described) >= {"params", "metrics"}

        geometry = await aok(core, me, "create_object", {"type": "geometry", "body": AIRFOIL})
        design = await aok(core, me, "create_object", {
            "type": "design",
            "body": {"solver": "mock.laplace2d", "geometry": geometry["ref"], "params": FAST},
        })
        patched = await aok(core, me, "patch_object", {
            "ref": design["ref"],
            "patch": [{"op": "replace", "path": "/params/resolution", "value": 48}],
        })
        assert patched["revision"] == 2

        created = await aok(core, me, "submit_job", {"design": design["ref"]})
        await finished(core, me, created["job_id"])

        summary = await aok(core, me, "get_result", {"job_id": created["job_id"]})
        assert summary["metrics"], "no metrics came back"
        assert "fields" not in summary, "the default answer carried the arrays"

        peak = await aok(core, me, "query_result",
                         {"job_id": created["job_id"], "field": "psi", "op": "max"})
        assert isinstance(peak["result"]["value"], int | float)

    asyncio.run(slice_())


# ------------------------------------------------------------------------ vocabulary


def test_no_tool_is_per_solver_or_per_physics(core, me):
    """"A new solver adapter must become usable without touching the MCP surface."

    Asserted against the *installed* capabilities rather than a hard-coded list, so adding an
    adapter named `mock.plasma2d` would fail this the moment somebody wrote a `solve_plasma`
    tool to go with it.
    """
    names = set(mcp_adapter.TOOLS)
    for capability in core.capability_list():
        physics = capability.physics.replace("-", "_")
        leaf = capability.name.split(".")[-1]
        assert not any(physics in tool or leaf in tool for tool in names), (
            f"a tool is named after the capability {capability.name!r}"
        )


def test_the_tool_list_is_a_curated_subset_not_the_whole_rpc_table():
    """A host's tool list is read every turn, so it is the one surface where exposing
    everything is actively wrong."""
    from fenixspoon.rpc.methods import METHODS

    methods = {method for method, _d, _s in mcp_adapter.TOOLS.values()}
    assert methods < set(METHODS), "the tool set should be a strict subset"
    assert len(mcp_adapter.TOOLS) < len(METHODS) / 1.5, "barely a subset is not curation"


#: Where a person is told how many tools this adapter exposes. The count is the *argument* —
#: "thirteen, not twenty-six" is the curation claim in miniature — which makes it exactly the
#: kind of number that has to stay true, and exactly the kind nothing was checking.
TOOL_COUNT_CLAIMS = {
    "README.md": r"host as ([0-9]+|[a-z-]+) tools",
    "docs/09-mcp.md": r"## ([0-9]+|[A-Za-z-]+) tools, not",
    "docs/03-roadmap.md": r"\*([0-9]+|[A-Za-z-]+) tools, not",
    "docs/07-local-agent-interface.md": r"— ([0-9]+|[a-z-]+) tools over the JSON-RPC",
}


@pytest.mark.parametrize("filename", sorted(TOOL_COUNT_CLAIMS))
def test_the_prose_counts_the_tools_this_adapter_actually_exposes(filename):
    """Four documents state this number and none of them was checked against the table.

    The JSON-RPC suite makes the same check for its method count and owns the parsing, since
    both documents write counts as digits in one place and as words in another. This test
    lives here rather than there because `mcp_adapter` cannot be imported without the extra —
    so the guard runs in the `mcp` CI job, which is the job that installs it.
    """
    from .test_rpc import stated_count

    root = Path(__file__).resolve().parents[2]
    text = (root / filename).read_text()
    assert stated_count(text, TOOL_COUNT_CLAIMS[filename]) == len(mcp_adapter.TOOLS), (
        f"{filename} states a tool count the adapter does not have; "
        f"there are {len(mcp_adapter.TOOLS)}"
    )


def test_every_tool_names_a_method_that_exists():
    """The table is the only place a tool and a method are tied together, so a typo here
    would be a tool that fails at call time rather than at import."""
    from fenixspoon.rpc.methods import METHODS

    for tool, (method, description, schema) in mcp_adapter.TOOLS.items():
        assert method in METHODS, f"{tool} points at unknown method {method}"
        assert description.strip(), f"{tool} has no description for a model to read"
        assert schema["type"] == "object", f"{tool} takes something other than named arguments"


def test_a_tool_schema_requires_what_its_handler_requires():
    """A schema that says an argument is optional when the handler refuses without it is a
    tool that fails at call time for a reason its own schema said would not happen.

    `query_result` is the case that was wrong: its schema is `FieldQuery`'s plus the `job_id`
    the handler takes separately, and the first version merged `properties` by hand and left
    `required` alone. Raised in review of #49.
    """
    _method, _description, schema = mcp_adapter.TOOLS["query_result"]
    assert "job_id" in schema["properties"]
    assert "job_id" in schema["required"]
    assert "field" in schema["required"] and "op" in schema["required"], (
        "the model's own required fields were lost in the merge"
    )


def test_every_tool_is_advertised_with_a_schema():
    tools = {tool.name: tool for tool in mcp_adapter.tool_list()}
    assert set(tools) == set(mcp_adapter.TOOLS)
    for tool in tools.values():
        assert tool.input_schema.get("type") == "object"
        assert tool.description


# ------------------------------------------------------------------------ delegation


def test_a_tool_is_an_rpc_method_plus_a_schema(core, me):
    """The property the whole design rests on: the same request over MCP and over JSON-RPC
    produces the same answer, by construction rather than by coincidence.

    Compared as payloads rather than by inspecting the call, because "it calls the same
    function" is what the implementation says and this is what a caller would observe.
    """
    from fenixspoon.rpc.encoding import jsonable
    from fenixspoon.rpc.methods import METHODS

    arguments = {"name": "mock.laplace2d", "sections": ["params"]}
    through_mcp = ok(core, me, "describe_capability", arguments)
    through_rpc = jsonable(METHODS["capability.describe"](core, me, arguments))
    assert through_mcp == through_rpc


def test_a_tool_result_carries_both_text_and_structured_content(core, me):
    """Hosts still differ on which they read, and emitting one would make this adapter's
    usefulness depend on the host's version."""
    result = call(core, me, "list_capabilities")
    assert result.content and result.content[0].text
    assert result.structured_content is not None
    assert json.loads(result.content[0].text) == result.structured_content["result"]


# ---------------------------------------------------------------------------- errors


def test_a_domain_error_is_a_tool_result_not_a_protocol_fault(core, me):
    """A model that asked for a capability that does not exist needs to read the list of ones
    that do, not to see the connection fault."""
    result = call(core, me, "describe_capability", {"name": "physics.from_the_future"})
    assert result.is_error
    body = json.loads(result.content[0].text)
    assert body["type"] == "UnknownCapability"
    assert body["name"] == "physics.from_the_future"


def test_a_malformed_argument_is_reported_the_way_the_rpc_transport_reports_it(core, me):
    """MCP inherits #45's parameter typing for free; this is the test that it really is the
    same path rather than a second one that happens to look similar."""
    result = call(core, me, "describe_capability",
                  {"name": "mock.laplace2d", "inline_schemas": "false"})
    assert result.is_error
    body = json.loads(result.content[0].text)
    assert body["type"] == "WrongParameterType"
    assert body["parameter"] == "inline_schemas"


def test_an_error_carries_structured_content_too(core, me):
    """"Both forms" has to mean both outcomes. Setting only `content` on failures would give a
    host successes it can parse and failures it cannot, which is the wrong way round. Raised
    in review of #49."""
    result = call(core, me, "describe_capability", {"name": "physics.from_the_future"})
    assert result.is_error
    assert result.structured_content is not None
    assert result.structured_content == json.loads(result.content[0].text)
    assert result.structured_content["type"] == "UnknownCapability"


def test_an_unknown_tool_lists_the_ones_that_exist(core, me):
    result = call(core, me, "solve_magnetostatics", {})
    assert result.is_error
    body = json.loads(result.content[0].text)
    assert body["type"] == "UnknownTool"
    assert "submit_job" in body["tools"]


def test_one_principals_work_is_not_visible_to_another(core, me):
    """The local transport is a different door, not a bypass — and an MCP host is a caller
    like any other."""
    geometry = ok(core, me, "create_object", {"type": "geometry", "body": AIRFOIL})
    stranger = Principal(id="somebody-else", quotas=Quotas())
    result = call(core, stranger, "get_object", {"ref": geometry["ref"]})
    assert result.is_error
    assert json.loads(result.content[0].text)["type"] == "ObjectNotFound"


# ------------------------------------------------------------------------- resources


def solve_with_artifact(core, me) -> str:
    async def go():
        created = await aok(core, me, "submit_job", {
            "solver": "mock.laplace2d", "geometry": AIRFOIL,
            "params": {**FAST, "write_vtk": True},
        })
        return await finished(core, me, created["job_id"])

    return asyncio.run(go())


def test_artifacts_are_listed_as_resources(core, me):
    job_id = solve_with_artifact(core, me)
    resources = mcp_adapter.artifact_resources(core, me)
    assert any(str(r.uri).endswith(f"{job_id}/solution.vtk") for r in resources)
    assert all(str(r.uri).startswith(f"{mcp_adapter.SCHEME}://") for r in resources)
    assert all(r.size and r.size > 0 for r in resources)


def test_a_resource_list_is_scoped_to_its_principal(core, me):
    """A resource list is a job listing wearing a different name, and a job id is itself a
    disclosure."""
    solve_with_artifact(core, me)
    stranger = Principal(id="somebody-else", quotas=Quotas())
    assert mcp_adapter.artifact_resources(core, stranger) == []


def test_a_large_artifact_is_described_rather_than_base64_encoded(core, me, monkeypatch):
    """The deliberate refusal, and the reason it is not a missing feature.

    Base64 makes a file a third larger and lands it in a context window that cannot use it.
    What comes back is where to find it — this is a *local* transport, so the path is real.
    """
    job_id = solve_with_artifact(core, me)
    uri = f"{mcp_adapter.SCHEME}://job/{job_id}/solution.vtk"
    handle = core.artifact(job_id, "solution.vtk", me)

    # The boundary is moved rather than crossed. A test that relied on the fixture's artifact
    # happening to exceed 64 kB would exercise whichever branch the mock solver's resolution
    # left it on — and this one sat on the *inline* side, so the assertion below was passing
    # without ever running the code it describes. Both branches are checked explicitly now.
    monkeypatch.setattr(mcp_adapter, "INLINE_LIMIT", handle.size - 1)
    read = mcp_adapter.read_resource(core, me, uri)
    body = read.contents[0].text

    described = json.loads(body)
    assert Path(described["path"]).is_file()
    assert described["size"] == handle.size
    assert "not inlined" in described["note"]
    # The description is JSON and must say so. Keeping the artifact's own type here would
    # label it `model/vnd.vtk` and hand a host something that breaks the moment it believes
    # the label; the real type is inside, under `content_type`. Raised in review of #49.
    assert read.contents[0].mime_type == "application/json"
    assert described["content_type"] == handle.content_type
    # …and below the limit the same artifact arrives inline, with its own type.
    monkeypatch.setattr(mcp_adapter, "INLINE_LIMIT", handle.size)
    inline = mcp_adapter.read_resource(core, me, uri)
    assert inline.contents[0].text.startswith("# vtk DataFile")
    assert inline.contents[0].mime_type == handle.content_type


def test_reading_an_artifact_that_does_not_exist_is_a_domain_error(core, me):
    from fenixspoon.core import errors

    job_id = solve_with_artifact(core, me)
    with pytest.raises(errors.ArtifactNotFound):
        mcp_adapter.read_resource(core, me, f"{mcp_adapter.SCHEME}://job/{job_id}/nope.vtk")


def test_the_server_advertises_instructions_a_model_can_act_on(core, me):
    """The instructions field is the one piece of prose every host shows a model, so it says
    how to use the vocabulary rather than what the project is."""
    server = mcp_adapter.build_server(core, me)
    assert server.instructions
    assert "describe_capability" in server.instructions
