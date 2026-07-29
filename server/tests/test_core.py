"""The transport-neutral core (#42).

Two of these are the issue's acceptance criteria rather than unit tests.
:func:`test_the_core_does_not_import_fastapi` is the one that keeps the layering honest —
it caught a real leak while this was being written, because `core/service.py` imported
`Principal` from `auth.py`, and `auth.py` imports FastAPI. Every other test in the suite ran
with FastAPI already loaded, so nothing else could have noticed.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from geometries import SOLENOID

from fenixspoon.core import FenixSpoonCore, errors
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.geometry import Domain2D, Polygon2D
from fenixspoon.http_errors import FALLBACK_STATUS, STATUS, status_for
from fenixspoon.jobs import JobManager

# A `domain2d` for the potential-flow solver; `geometries.py` only ships the solenoid.
AIRFOIL = Domain2D(
    bounds=(-1.0, -1.0, 2.0, 1.0),
    obstacle=Polygon2D(points=[(0.0, 0.0), (0.35, 0.09), (1.0, 0.0), (0.35, -0.06)]),
)


@pytest.fixture()
def core(tmp_path):
    return FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))


@pytest.fixture()
def me():
    return Principal(id="tester", quotas=Quotas())


FAST = {"resolution": 48, "iterations": 60, "write_vtk": False}


async def solve(core, me, **overrides):
    job = await core.submit("mock.laplace2d", AIRFOIL, {**FAST, **overrides}, me)
    deadline = asyncio.get_running_loop().time() + 20
    while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
        assert asyncio.get_running_loop().time() < deadline, "solve did not finish"
        await asyncio.sleep(0.02)
    return core.job(job.id, me, with_result=True)


def test_the_core_does_not_import_fastapi():
    """Importing the core must not pull in a web framework.

    Run in a subprocess because this test process has FastAPI loaded already — asserting on
    `sys.modules` in-process would pass no matter what the core imports. The chain that
    broke this was `core.service` -> `auth` -> `fastapi`; the fix was splitting the identity
    *rule* into `core.identity` and leaving the HTTP *binding* in `auth`.
    """
    code = (
        "import sys; import fenixspoon.core; "
        "sys.exit(1 if 'fastapi' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"importing fenixspoon.core imported fastapi\n{result.stderr}"


def test_a_plain_script_can_solve(core, me):
    """The other acceptance criterion: submit and read a result, no HTTP anywhere."""
    job = asyncio.run(solve(core, me))
    assert job.status == "done"
    result = core.result(job.id, me)
    assert result.kind == "grid2d"
    assert result.data["fields"]["psi"]
    assert result.stats["cells"] > 0


def test_artifacts_are_paths_not_urls(core, me):
    """The core hands back something a local caller can open.

    A `url` here would make this an HTTP core wearing a different name — only the HTTP
    adapter knows that a file is reachable at a route it serves.
    """
    job = asyncio.run(solve(core, me, write_vtk=True))
    result = core.result(job.id, me)
    assert [a.name for a in result.artifacts] == ["solution.vtk"]
    handle = result.artifacts[0]
    assert handle.path.is_file()
    assert "api/v1" not in str(handle.path)
    assert handle.path.read_text().startswith("# vtk DataFile")
    # And resolvable by name through the core, with the same path.
    assert core.artifact(job.id, "solution.vtk", me).path == handle.path


@pytest.mark.parametrize(
    ("label", "solver", "geometry", "params", "expected"),
    [
        ("unknown solver", "nope", AIRFOIL, {}, errors.UnknownCapability),
        ("wrong geometry", "mock.magnetostatics2d", AIRFOIL, {}, errors.GeometryKindMismatch),
        ("bad params", "mock.laplace2d", AIRFOIL, {"resolution": -5}, errors.InvalidParams),
        ("geometry for the other solver", "mock.laplace2d", SOLENOID, {},
         errors.GeometryKindMismatch),
    ],
)
def test_submit_raises_domain_errors(core, me, label, solver, geometry, params, expected):
    del label
    with pytest.raises(expected):
        asyncio.run(core.submit(solver, geometry, params, me))


def test_another_principals_job_is_not_found_rather_than_forbidden(core, me):
    """Indistinguishable on purpose: confirming an id exists is itself a leak."""
    job = asyncio.run(solve(core, me))
    stranger = Principal(id="stranger", quotas=Quotas())
    with pytest.raises(errors.JobNotFound):
        core.job(job.id, stranger)
    with pytest.raises(errors.JobNotFound):
        core.result(job.id, stranger)
    with pytest.raises(errors.JobNotFound):
        core.artifact(job.id, "solution.vtk", stranger)


def test_result_before_it_exists_says_which(core, me):
    async def run():
        job = await core.submit("mock.laplace2d", AIRFOIL, {**FAST, "iterations": 4000}, me)
        with pytest.raises(errors.JobNotFinished) as caught:
            core.result(job.id, me)
        assert caught.value.status in ("queued", "running")
        await core.cancel(job.id, me)

    asyncio.run(run())


def test_cancelling_a_finished_job_is_an_error_not_a_silent_no_op(core, me):
    job = asyncio.run(solve(core, me))
    with pytest.raises(errors.JobAlreadyFinished):
        asyncio.run(core.cancel(job.id, me))


def test_a_cancelled_job_has_no_result(core, me):
    async def run():
        job = await core.submit("mock.laplace2d", AIRFOIL, {**FAST, "iterations": 8000}, me)
        await core.cancel(job.id, me)
        deadline = asyncio.get_running_loop().time() + 20
        while core.job(job.id, me).status not in ("done", "failed", "cancelled"):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        if core.job(job.id, me).status == "cancelled":
            with pytest.raises(errors.JobDidNotSucceed):
                core.result(job.id, me)

    asyncio.run(run())


def test_the_cell_budget_is_enforced_by_the_core(tmp_path, me):
    """Not by the HTTP layer — a local caller must not be able to skip it."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs", max_cells=100))
    with pytest.raises(errors.CellBudgetExceeded) as caught:
        asyncio.run(core.submit("mock.laplace2d", AIRFOIL, {"resolution": 256}, me))
    assert caught.value.limit == 100
    assert caught.value.estimate > 100


def test_quotas_are_enforced_by_the_core(tmp_path):
    """Also a domain rule, so it holds for every transport."""
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    capped = Principal(id="capped", quotas=Quotas(jobs_per_hour=1))

    async def run():
        await core.submit("mock.laplace2d", AIRFOIL, FAST, capped)
        with pytest.raises(errors.QuotaExceeded) as caught:
            await core.submit("mock.laplace2d", AIRFOIL, FAST, capped)
        # Waiting genuinely helps for an hourly cap, so the hint is set.
        assert caught.value.retry_after == 3600

    asyncio.run(run())


def test_history_is_scoped_and_paginated(core, me):
    async def run():
        for _ in range(3):
            await solve(core, me)
        jobs, total = core.history(me)
        assert total == 3 and len(jobs) == 3
        page, total = core.history(me, limit=2)
        assert len(page) == 2 and total == 3
        stranger_jobs, stranger_total = core.history(Principal(id="x", quotas=Quotas()))
        assert stranger_jobs == [] and stranger_total == 0

    asyncio.run(run())


# ----------------------------------------------------------------- HTTP mapping


def test_every_core_error_has_a_status():
    """A new domain error must be given a status deliberately, not fall back to 400.

    The fallback exists so an unmapped error degrades rather than crashes; this test is
    what stops it becoming the silent default.
    """
    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    unmapped = [cls.__name__ for cls in subclasses(errors.CoreError) if cls not in STATUS]
    assert unmapped == [], f"no HTTP status mapped for: {unmapped}"


def test_status_lookup_walks_the_mro():
    """A subclass of a mapped error inherits its status rather than falling back."""

    class PickierJobNotFound(errors.JobNotFound):
        pass

    assert status_for(PickierJobNotFound()) == 404
    assert status_for(errors.CoreError("something new")) == FALLBACK_STATUS


def test_the_error_handler_re_raises_what_it_cannot_render():
    """It must not paper over a wrong registration, and must not rely on `assert`.

    `assert` is stripped under `python -O`, so a handler bound to the wrong type would have
    failed on `.detail` with an `AttributeError` saying nothing about the real problem.
    Re-raising gives the framework's default handler the original exception instead.
    """
    from fenixspoon.http_errors import core_error_handler

    boom = ValueError("not a domain error")
    with pytest.raises(ValueError, match="not a domain error"):
        asyncio.run(core_error_handler(None, boom))

    # …and it still renders a real one.
    response = asyncio.run(core_error_handler(None, errors.JobNotFound()))
    assert response.status_code == 404


def test_the_handler_narrowing_survives_optimised_python():
    """The specific regression: `python -O` removes asserts, so the check must not be one."""
    code = (
        "import asyncio, sys;"
        "from fenixspoon.http_errors import core_error_handler;"
        "\ntry:\n"
        "    asyncio.run(core_error_handler(None, ValueError('x')))\n"
        "except ValueError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"under -O the handler did not re-raise\n{result.stderr}"
