"""Loading adapters that do not live in this repository (#105).

The `Solver` protocol was always implementable from outside — the registry knows nothing about
where a class came from — but nothing an operator controlled ran before `fenixspoon.solvers`
populated the registry, so every new physics had to land here. That made
[ADR 0005](../../docs/adr/0005-thin-about-physics-thick-about-claims.md) decision 5 unmeetable:
breadth is bought with adapters, and there was no way to add one.

Most of what is asserted below is **failure handling**, and that is the right proportion. The
happy path is one import; the interesting part is that each of the three ways a stranger's module
can go wrong has a tempting handling that is wrong — propagate (their bug becomes the operator's
outage), or swallow (a missing capability with no reason attached, which is the thing the
declaration machinery exists to prevent everywhere else).

The sample modules in `plugin_samples/` stand in for a distribution. They are pointed at the way
an entry point would point at them, which is why nothing here needs a package to be built.
"""

import sys

import pytest

from fenixspoon.core import FenixSpoonCore
from fenixspoon.core.discovery import EnvironmentInfo
from fenixspoon.core.identity import Principal, Quotas
from fenixspoon.jobs import JobManager
from fenixspoon.solvers import get_solver, load_plugins, plugin_loads, plugins, registry
from fenixspoon.solvers.plugins import DISABLE_ENV, MODULES_ENV

SAMPLES = "plugin_samples"

ANYONE = Principal(id="tester", quotas=Quotas())


@pytest.fixture()
def isolated():
    """Undo everything a load does, so these tests do not leak capabilities into the suite.

    Reaches into `registry._REGISTRY` on purpose. The registry has no removal — nothing in the
    server's life needs one, and adding an API for a test's benefit would be the tail wagging
    the dog — so the fixture restores the mapping wholesale instead.
    """
    before = dict(registry._REGISTRY)
    loaded = set(sys.modules)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)
    for name in set(sys.modules) - loaded:
        if name.startswith(SAMPLES):
            del sys.modules[name]
    load_plugins(env={}, entry_points=[])


def load(modules: str = "", **env: str):
    """Load through the environment variable, with the entry-point source explicitly empty.

    `entry_points=None` would read the *real* installed metadata, which is hermetic only by
    luck: any package on the runner declaring `fenixspoon.solvers` would add reports and
    capabilities these assertions do not expect. Raised in review of #105 — passing `[]` is
    what says "no entry points" rather than "whatever this machine has".
    """
    return load_plugins(env={MODULES_ENV: modules, **env}, entry_points=[])


# --------------------------------------------------------------------- the happy path


def test_an_entry_point_puts_a_stranger_in_the_registry(isolated):
    """The mechanism a distribution uses: a `fenixspoon.solvers` entry point naming a module."""
    (report,) = load_plugins(env={}, entry_points=[("sample", f"{SAMPLES}.good")])

    assert report.status == "loaded"
    assert report.origin == "entry-point"
    assert report.capabilities == ("sample.plugin2d",)
    assert get_solver("sample.plugin2d") is not None


def test_the_environment_variable_loads_an_unpackaged_module(isolated):
    """The other half: an adapter in the application's own tree, no packaging involved."""
    (report,) = load(f"{SAMPLES}.good")

    assert (report.status, report.origin) == ("loaded", "environment")
    assert report.source == report.module == f"{SAMPLES}.good"
    assert get_solver("sample.plugin2d") is not None


def test_a_loaded_plugin_is_an_ordinary_capability(isolated, tmp_path):
    """Discovery does not special-case where a class came from, and this is what says so.

    The claim being tested is the one on the front page — *a solver is one class; the server
    needs no changes to accept it* — which was true about the protocol and false about getting
    one loaded.
    """
    load(f"{SAMPLES}.good")
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))

    summary = next(item for item in core.capability_list() if item.name == "sample.plugin2d")
    assert summary.physics == "sample"
    described = core.capability_describe("sample.plugin2d", sections=["params"])
    assert described.params is not None
    assert [param.name for param in described.params.params] == ["resolution"]


# ------------------------------------------------------------------ the three failures


def test_a_broken_import_is_reported_rather_than_raised(isolated):
    """The commonest real failure, and the one where propagating costs the most.

    A missing dependency in one plugin must not remove the capabilities that do work — so the
    assertion is as much about what is *still* registered as about the report.
    """
    before = len(registry.registered_solvers())
    (report,) = load(f"{SAMPLES}.broken")

    assert report.status == "failed"
    assert report.detail == "ImportError: No module named 'slepc4py'"
    assert report.capabilities == ()
    assert len(registry.registered_solvers()) == before


def test_the_incumbent_keeps_a_contested_name(isolated):
    """A plugin claiming a shipped name loses, and the loss is legible.

    `register` refuses the second claim, which raises out of the plugin's import — so the
    built-in is still there afterwards, and the operator is told why the plugin is not.
    """
    incumbent = get_solver("mock.laplace2d")
    (report,) = load(f"{SAMPLES}.colliding")

    assert report.status == "failed"
    assert "already registered" in report.detail
    assert get_solver("mock.laplace2d") is incumbent


def test_a_half_loaded_module_says_what_it_managed(isolated):
    """The state that cannot be undone, reported rather than papered over.

    A module that registers one adapter and raises before the second leaves the first in the
    registry. Pretending otherwise would need the registry to support removal for the sake of a
    case that is already a bug, so the report carries both facts: it failed, *and* this is what
    it added.
    """
    (report,) = load(f"{SAMPLES}.partial")

    assert report.status == "failed"
    assert report.capabilities == ("sample.partial2d",)
    assert get_solver("sample.partial2d") is not None


# -------------------------------------------------------------------------- the switch


def test_disabled_is_not_the_same_answer_as_nothing_installed(isolated):
    """Both send no plugins; only one of them is a decision, and a caller can tell.

    Without the entry, an operator debugging a missing capability on a locked-down deployment
    would see an empty list and conclude nothing was configured.
    """
    (report,) = load(f"{SAMPLES}.good", **{DISABLE_ENV: "1"})

    assert report.status == "disabled"
    assert get_solver("sample.plugin2d") is None
    assert load_plugins(env={}, entry_points=[]) == []


def test_unreadable_entry_point_metadata_is_a_report_rather_than_a_silence(isolated, monkeypatch):
    """A corrupt `site-packages` must not look like an installation that declares no plugins.

    Raised in review of #105, and the right catch: the first version returned an empty list, so
    the one condition an operator could do nothing about was the one the report would not
    mention — in the module whose whole argument is that a silence is the bug.
    """

    def unreadable(**_):
        raise ValueError("invalid distribution metadata in site-packages")

    monkeypatch.setattr(plugins.metadata, "entry_points", unreadable)
    (report,) = load_plugins(env={})

    assert report.status == "failed"
    assert report.source == plugins.PLUGIN_GROUP
    assert "invalid distribution metadata" in report.detail


def test_a_recorded_load_cannot_be_edited_by_a_caller(isolated):
    """`plugin_loads()` hands out the stored records, so they are frozen all the way down.

    Raised in review of #105: a mutable `capabilities` list on a frozen dataclass is a record
    with an editable field, and appending to one would edit what `environment.inspect` reports.
    """
    (report,) = load(f"{SAMPLES}.good")

    with pytest.raises(AttributeError):
        report.status = "failed"
    assert isinstance(plugin_loads()[0].capabilities, tuple)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_switch_takes_the_usual_spellings(isolated, value):
    assert load(f"{SAMPLES}.good", **{DISABLE_ENV: value})[0].status == "disabled"


def test_an_unset_switch_does_not_disable(isolated):
    assert load(f"{SAMPLES}.good", **{DISABLE_ENV: ""})[0].status == "loaded"


# ------------------------------------------------------------------------ on the wire


def test_the_environment_payload_carries_the_report(isolated, tmp_path):
    """Protocol 1.15's whole content: why a capability is absent, not merely that it is."""
    load(f"{SAMPLES}.broken")
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))

    environment = core.environment(principal=ANYONE)
    assert isinstance(environment, EnvironmentInfo)
    (plugin,) = environment.plugins
    assert (plugin.status, plugin.module) == ("failed", f"{SAMPLES}.broken")
    assert "slepc4py" in plugin.detail


def test_an_installation_with_no_plugins_sends_an_empty_list(isolated, tmp_path):
    load_plugins(env={}, entry_points=[])
    core = FenixSpoonCore(JobManager(data_dir=tmp_path / "jobs"))
    assert core.environment(principal=ANYONE).plugins == []


# ------------------------------------------------------------------------- cache identity


def test_a_plugins_version_is_part_of_its_cache_key(isolated):
    """The mechanism already existed; this is the test that says it covers a stranger.

    `Solver.version` and `requires` are declared per adapter and the content address reads
    both, so a third-party adapter that changes its maths and bumps its version stops matching
    its own cached results. Nothing in `cache.py` needed to change for #105 — which is worth a
    test rather than a sentence, because "it should already work" is how a cache serves a stale
    answer forever.
    """
    from geometries import PLATE_WITH_HOLE

    from fenixspoon.cache import cache_key, environment_fingerprint

    load(f"{SAMPLES}.good")
    solver = get_solver("sample.plugin2d")
    geometry = PLATE_WITH_HOLE

    def key(version: str) -> str:
        return cache_key(
            solver=solver.name,
            solver_version=version,
            geometry=geometry.model_dump(),
            params={"resolution": 16},
            environment=environment_fingerprint(list(solver.requires)),
        )

    assert key("1.0.0") == key("1.0.0")
    assert key("1.0.0") != key("1.1.0")
