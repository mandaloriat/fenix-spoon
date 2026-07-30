"""The CLI and Python adapters (#50).

The acceptance criterion has two halves. *The vertical-slice loop is reproducible as a shell
script and as a Python script* — :func:`test_the_slice_runs_as_a_shell_script` and
:func:`test_the_slice_runs_as_a_python_script`. And *the CLI's `--json` output for a solve is
byte-comparable in meaning to the JSON-RPC result for the same request* —
:func:`test_the_json_output_is_the_json_rpc_result`, which asserts something stronger than
the criterion asks: the same *value*, field for field. Not the same bytes — the CLI
pretty-prints and the transport frames compactly — and saying "bytes" was an overclaim until
review of #50 caught it.

The shell test really runs a shell. That is slow and it is the point: the thing being claimed
is that somebody can paste these lines into CI, and a test that called the functions directly
would prove the functions work while the claim was about the command.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fenixspoon import local
from fenixspoon.commands import EXIT_CODES, render
from fenixspoon.core import errors
from fenixspoon.rpc import errors as rpc_errors

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


@pytest.fixture()
def workspace(tmp_path):
    """A data directory for one test, and the environment every command inherits."""
    env = {**os.environ, "FENIXSPOON_DATA_DIR": str(tmp_path / "ws")}
    return env


def run(env, *argv, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fenixspoon", *argv],
        cwd=REPO_SERVER,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


def ok(env, *argv, stdin: str | None = None):
    result = run(env, *argv, "--json", stdin=stdin)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture()
def session(tmp_path):
    with local.open_workspace(tmp_path / "ws") as fs:
        yield fs


# ------------------------------------------------------------------------ acceptance


def test_the_slice_runs_as_a_shell_script(workspace, tmp_path):
    """Discover, design, patch, solve, read — as a shell script, in a real shell.

    Written the way a CI step would be: `set -e`, `--json` piped through a parser, exit codes
    doing the branching. If this passes, the lines can be pasted.
    """
    # Documents go in as files and the ref is substituted by a helper script, the way a CI
    # step would do it with `jq`. Building nested JSON inside a shell string is a quoting
    # exercise that ends up testing the test rather than the command.
    (tmp_path / "geometry.json").write_text(json.dumps(AIRFOIL))
    (tmp_path / "compose.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'solver': 'mock.laplace2d', 'geometry': sys.argv[1],\n"
        f"                  'params': {FAST!r}}}))\n"
    )
    (tmp_path / "field.py").write_text(
        "import json, sys\nprint(json.load(sys.stdin)[sys.argv[1]])\n"
    )
    py = sys.executable

    script = f"""
    set -euo pipefail
    fs() {{ "{py}" -m fenixspoon "$@"; }}
    field() {{ "{py}" "{tmp_path}/field.py" "$1"; }}

    fs capability list --json > /dev/null
    fs capability describe mock.laplace2d --sections params --json > /dev/null

    GEOM=$(fs object create geometry --json < "{tmp_path}/geometry.json" | field ref)
    "{py}" "{tmp_path}/compose.py" "$GEOM" > "{tmp_path}/design.json"
    DESIGN=$(fs object create design --json < "{tmp_path}/design.json" | field ref)

    echo '[{{"op":"replace","path":"/params/resolution","value":48}}]' \
        | fs object patch "$DESIGN" --json > /dev/null

    JOB=$(fs job submit --design "$DESIGN" --json < /dev/null | field job_id)

    test "$(fs job get "$JOB" --json | field status)" = done
    fs result get "$JOB" --json > "{tmp_path}/result.json"
    fs result query "$JOB" --field psi --op max --json > "{tmp_path}/peak.json"
    """
    completed = subprocess.run(
        ["bash", "-c", script], env=workspace, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr

    result = json.loads((tmp_path / "result.json").read_text())
    assert result["metrics"], "no metrics in the compact result"
    assert "fields" not in result, "the default answer carried the arrays"
    peak = json.loads((tmp_path / "peak.json").read_text())
    assert isinstance(peak["result"]["value"], int | float)


def test_the_slice_runs_as_a_python_script(session):
    """The same loop through the Python API, returning typed objects rather than dicts.

    The types are the assertion: this is the thing that replaces "import the internals and
    hope", so what comes back has to be the models every other transport carries.
    """
    from fenixspoon.core.discovery import CapabilitySummary
    from fenixspoon.core.workspace import ObjectView

    assert all(isinstance(item, CapabilitySummary) for item in session.capabilities())

    geometry = session.create("geometry", AIRFOIL)
    assert isinstance(geometry, ObjectView)
    design = session.create(
        "design",
        {"solver": "mock.laplace2d", "geometry": geometry.ref, "params": FAST},
    )
    patched = session.patch(
        design.ref, [{"op": "replace", "path": "/params/resolution", "value": 48}]
    )
    assert patched.revision == 2

    job = session.submit(design=design.ref).wait()
    assert job.status().status == "done"
    assert job.metrics()["speed_max"] > 0
    assert isinstance(job.query("psi", "max").result["value"], int | float)
    assert job.provenance().solver == "mock.laplace2d"


def test_the_json_output_is_the_json_rpc_result(workspace, tmp_path):
    """Stronger than "byte-comparable in meaning": the same value, field for field.

    The CLI dispatches through the JSON-RPC method table, so this is a property of the design
    rather than a coincidence to be maintained — and it is what makes the CLI usable as the
    debugging surface for what an agent sees. A CLI that reformatted, rounded or renamed
    anything would be showing something the protocol does not say.
    """
    from fenixspoon.core import FenixSpoonCore
    from fenixspoon.core.identity import Principal, Quotas
    from fenixspoon.jobs import JobManager
    from fenixspoon.rpc.encoding import jsonable
    from fenixspoon.rpc.methods import METHODS

    through_cli = ok(workspace, "capability", "describe", "mock.laplace2d",
                     "--sections", "params", "--sections", "metrics")

    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "rpc"))
    through_rpc = jsonable(
        METHODS["capability.describe"](
            core,
            Principal(id="anonymous", quotas=Quotas()),
            {"name": "mock.laplace2d", "sections": ["params", "metrics"]},
        )
    )
    assert through_cli == through_rpc

    # …and the same for a solve, which is the case the criterion names.
    geometry = ok(workspace, "object", "create", "geometry", stdin=json.dumps(AIRFOIL))
    design = ok(workspace, "object", "create", "design", stdin=json.dumps(
        {"solver": "mock.laplace2d", "geometry": geometry["ref"], "params": FAST}
    ))
    created = ok(workspace, "job", "submit", "--design", design["ref"], stdin="")
    assert set(created) == {"job_id", "status", "cached"}, "submit reply differs from JSON-RPC"


# ------------------------------------------------------------------------------- CLI


def test_submit_waits_because_the_process_owns_the_solve(workspace):
    """The design decision the first implementation got wrong, and a test that would have
    caught it.

    With the in-process backend a solve runs on this process's thread pool. A `job submit`
    that returned immediately would kill the work it just started and leave a row saying
    `running` — which the next startup reconciles to *failed*. Fire-and-forget is a shell
    script's most natural instinct and, here, its most reliable way to get nothing.
    """
    geometry = ok(workspace, "object", "create", "geometry", stdin=json.dumps(AIRFOIL))
    created = ok(workspace, "job", "submit", "--solver", "mock.laplace2d",
                 "--param", f"params={json.dumps(FAST)}",
                 stdin=json.dumps({"geometry": AIRFOIL}))
    assert created["status"] == "done", "submit returned before the solve finished"
    del geometry


def test_detach_says_what_it_costs(workspace):
    """`--detach` exists for a worker backend and is honest about the local one."""
    helptext = run(workspace, "job", "submit", "--help").stdout
    assert "--detach" in helptext
    assert "dies with this command" in helptext


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("job", "get", "j-nope"), EXIT_CODES[rpc_errors.NOT_FOUND]),
        (("capability", "describe", "physics.nope"), EXIT_CODES[rpc_errors.NOT_FOUND]),
        (("capability", "describe", "mock.laplace2d", "--sections", "nope"),
         EXIT_CODES[rpc_errors.INVALID_PARAMS]),
    ],
)
def test_exit_codes_let_a_script_branch_without_parsing_prose(workspace, argv, expected):
    result = run(workspace, *argv)
    assert result.returncode == expected, result.stderr
    assert result.stdout == "", "a refusal printed to stdout"


def test_a_result_that_is_not_ready_is_a_different_exit_code_from_a_failure(workspace):
    """"Distinguish failed from not finished from invalid request" — the three the issue
    names, and they are three different numbers."""
    assert EXIT_CODES[rpc_errors.CONFLICT] != EXIT_CODES[rpc_errors.INVALID_PARAMS]
    assert EXIT_CODES[rpc_errors.CONFLICT] != EXIT_CODES[rpc_errors.NOT_FOUND]

    geometry = ok(workspace, "object", "create", "geometry", stdin=json.dumps(AIRFOIL))
    created = ok(workspace, "job", "submit", "--solver", "mock.laplace2d",
                 "--param", f"params={json.dumps(FAST)}",
                 stdin=json.dumps({"geometry": AIRFOIL}))
    del geometry
    # A finished job asked for a level it has: fine. The conflict case is a *cancelled* job,
    # which is a refusal to answer rather than a missing one.
    assert run(workspace, "result", "get", created["job_id"]).returncode == 0


def test_every_exit_code_is_reachable():
    """A code nothing can produce is a promise to a script that will never be kept.

    Two of the mapped codes come from the *transport* rather than from a domain error —
    `-32600` for a malformed request and `-32601` for an unknown method — and neither
    corresponds to a `CoreError` because neither is one. They are named here rather than
    excluded silently, so the exemption is a statement instead of a gap.
    """
    from fenixspoon import http_errors

    from_domain = {rpc_errors.CODES[cls] for cls in http_errors.STATUS}
    from_transport = {rpc_errors.INVALID_REQUEST, rpc_errors.METHOD_NOT_FOUND}
    for code in EXIT_CODES:
        assert code in from_domain | from_transport, (
            f"exit code for {code} cannot be produced by anything"
        )
    # …and every domain code has an exit code, which is the direction that actually rots:
    # adding an error to the core should not silently become exit 1.
    assert from_domain <= set(EXIT_CODES), (
        f"unmapped domain codes: {sorted(from_domain - set(EXIT_CODES))}"
    )


def test_a_flag_cannot_shadow_a_positional_of_the_same_command(workspace):
    """`object create` takes `type` positionally, and `--type` is the listing filter.

    Registering both would let `object create geometry --type design` build a request whose
    `type` disagrees with the body on stdin — a command that *succeeds* and creates the wrong
    thing, which is the worst outcome available. Raised in review of #50.
    """
    assert "--type" not in run(workspace, "object", "create", "--help").stdout
    # …and it is still there where it means something.
    assert "--type" in run(workspace, "workspace", "list", "--help").stdout


@pytest.mark.parametrize("param", ["notakeyvalue", "=novalue"])
def test_a_malformed_param_is_an_invalid_request_not_an_empty_key(workspace, param):
    """Accepting `--param foo` silently set an empty-named parameter to `""`, which the server
    then refused for a reason that had nothing to do with the typo. Raised in review of #50."""
    result = run(workspace, "capability", "list", "--param", param)
    assert result.returncode == EXIT_CODES[rpc_errors.INVALID_PARAMS]
    assert "KEY=VALUE" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_bare_string_param_still_works(workspace):
    """The counter-case: `--param field=psi` must not need JSON quoting. Requiring it would be
    a worse command line than the one the refusal above protects."""
    geometry = ok(workspace, "object", "create", "geometry", stdin=json.dumps(AIRFOIL))
    assert geometry["type"] == "geometry"
    listed = ok(workspace, "workspace", "list", "--param", "type=geometry")
    assert [item["ref"] for item in listed] == [geometry["ref"]]


def test_stdin_of_the_wrong_shape_is_refused_with_a_reason(workspace):
    """A JSON array where an object belongs was silently dropped, turning a caller's mistake
    into a confusing "missing parameter" further down. Raised in review of #50."""
    result = run(workspace, "job", "submit", stdin="[1, 2, 3]")
    assert result.returncode == EXIT_CODES[rpc_errors.INVALID_PARAMS]
    assert "object on stdin" in result.stderr
    assert "Traceback" not in result.stderr


def test_human_output_is_a_rendering_not_a_different_answer(workspace):
    """`--json` switches the presentation. Anything a human can see, a script can get."""
    human = run(workspace, "capability", "list").stdout
    machine = ok(workspace, "capability", "list")
    for capability in machine:
        assert capability["name"] in human, "a row is missing from the human rendering"


def test_a_float_is_readable_in_the_rendering_and_exact_in_the_json():
    """Six significant figures is enough to compare two solves; the full value is one flag
    away, which is the point of having both."""
    assert render({"value": 0.123456789}) == "value: 0.123457"
    assert json.loads(json.dumps({"value": 0.123456789}))["value"] == 0.123456789


def test_a_ragged_list_degrades_to_the_keys_the_rows_agree_on():
    """A table of rows that do not share a shape would otherwise be a grid full of blanks."""
    rendered = render([{"a": 1, "b": 2}, {"a": 3}])
    assert "b" not in rendered.split("\n")[0]
    assert "a" in rendered.split("\n")[0]


# ---------------------------------------------------------------------- Python API


def test_the_session_keeps_one_loop_so_a_job_can_be_awaited_later(session):
    """The reason this API owns a background loop rather than calling `asyncio.run` per method.

    Submit, do something else, *then* wait. A per-call `asyncio.run` would close the loop that
    the in-process backend completes the solve on, so the status would never move — and the
    failure would look like a hang rather than like a design mistake.
    """
    geometry = session.create("geometry", AIRFOIL)
    job = session.submit(solver="mock.laplace2d", geometry=AIRFOIL, params=FAST)

    # Unrelated work between submitting and waiting — the shape a notebook has.
    assert session.capabilities()
    assert session.get(geometry.ref).ref == geometry.ref

    assert job.wait().status().status == "done"


def test_two_sessions_on_one_workspace_are_two_callers(tmp_path):
    """Nothing stops a second session; it behaves like another caller, because it is one."""
    with local.open_workspace(tmp_path / "ws") as first:
        geometry = first.create("geometry", AIRFOIL)
        with local.open_workspace(tmp_path / "ws") as second:
            assert second.get(geometry.ref).ref == geometry.ref


def test_a_different_principal_cannot_see_another_s_objects(tmp_path):
    """Identity is not bypassed in-process either."""
    with local.open_workspace(tmp_path / "ws", principal="alice") as alice:
        geometry = alice.create("geometry", AIRFOIL)
    with local.open_workspace(tmp_path / "ws", principal="bob") as bob:
        with pytest.raises(errors.ObjectNotFound):
            bob.get(geometry.ref)


def test_submitting_a_design_and_a_geometry_at_once_is_refused(session):
    """The same refusal every other transport makes, for the same reason."""
    with pytest.raises(ValueError, match="not both"):
        session.submit(design="design:d-1", solver="mock.laplace2d", geometry=AIRFOIL)


def test_the_cache_reaches_this_transport_too(session):
    """Not a feature of any adapter — a property of the core, which is why it is here for
    free."""
    session.create("geometry", AIRFOIL)
    first = session.submit(solver="mock.laplace2d", geometry=AIRFOIL, params=FAST).wait()
    again = session.submit(solver="mock.laplace2d", geometry=AIRFOIL, params=FAST)
    assert again.id == first.id
    assert again.provenance().cached is True


def test_a_study_runs_and_reports_through_the_python_api(session):
    geometry = session.create("geometry", AIRFOIL)
    design = session.create(
        "design", {"solver": "mock.laplace2d", "geometry": geometry.ref, "params": FAST}
    )
    study = session.create("study", {
        "kind": "mesh_convergence", "design": design.ref,
        "parameter": "resolution", "values": [24.0, 32.0],
    })
    run_result = session.run_study(study.ref)
    assert run_result.submitted == 2
    report = session.wait_for_study(study.ref)
    assert [rung.status for rung in report.rungs] == ["done", "done"]
    assert report.convergence


def test_a_job_repr_does_not_reach_into_the_core(tmp_path):
    """`__repr__` runs in debuggers, logs and tracebacks — often while something is already
    wrong — so one that called `status()` would raise from inside the report of the original
    failure. Checked against a *closed* session, which is the case that would break it.
    Raised in review of #50."""
    session = local.open_workspace(tmp_path / "ws")
    job = local.Job(session, "j-whatever")
    session.close()
    assert "j-whatever" in repr(job)


def test_closing_a_session_releases_the_execution_backend(tmp_path):
    """Not just the store. The in-process backend holds a `ThreadPoolExecutor`, and
    `concurrent.futures` registers an atexit hook that *joins* its threads — so an abandoned
    executor turns a long solve into a process that will not exit. A notebook is exactly where
    that bites. Raised in review of #50.

    Asserted on the executor's own state rather than on process behaviour, because "does the
    interpreter exit" is not something a test can ask without becoming a subprocess test for a
    one-line property.
    """
    session = local.open_workspace(tmp_path / "ws")
    executor = session.core.jobs.backend._executor
    session.close()
    assert executor._shutdown, "the solver thread pool outlived the session"


def test_the_python_api_does_not_import_fastapi():
    """An in-process entrypoint has no business needing a web framework — and the shutdown
    sequence it shares with the HTTP app is the seam where that nearly happened, which is why
    the sequence moved onto `JobManager`. Raised in review of #50."""
    code = "import sys; import fenixspoon.local; sys.exit(1 if 'fastapi' in sys.modules else 0)"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_SERVER, capture_output=True, text=True
    )
    assert result.returncode == 0, f"fenixspoon.local imported fastapi\n{result.stderr}"


def test_closing_a_session_twice_is_harmless(tmp_path):
    """A notebook re-runs cells; `close()` has to survive that."""
    session = local.open_workspace(tmp_path / "ws")
    session.close()
    session.close()


def test_waiting_past_the_timeout_raises_rather_than_hanging(session):
    """A solve that will not finish must not become a wedged notebook."""
    job = session.submit(
        solver="mock.laplace2d", geometry=AIRFOIL,
        params={"resolution": 256, "iterations": 20000},
    )
    with pytest.raises(TimeoutError):
        job.wait(timeout=0.2)
    # Cancelled *and* waited for: closing the session under a running solve leaves a task
    # pending on a loop that is about to stop, which asyncio reports as noise on every
    # subsequent test run.
    job.cancel().wait(timeout=30)
