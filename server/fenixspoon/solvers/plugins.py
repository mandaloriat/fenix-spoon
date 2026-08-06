"""Loading solver adapters that do not live in this repository (#105).

The `Solver` protocol has always been implementable from outside — `name`, `version`,
`requires`, `describe()`, `solve()`, and a registry that knows nothing about where a class
came from. What was missing was a way to get such a class *imported*, because nothing an
operator controls runs before `fenixspoon.solvers` populates the registry. So every new
physics had to land in this repository, and
[ADR 0005](../../../docs/adr/0005-thin-about-physics-thick-about-claims.md) decision 5 — that
breadth is bought with adapters and depth with protocol — had no outlet.

Two sources, one loader:

- **an entry point** in the `fenixspoon.solvers` group, which is how a *distribution* says it
  carries adapters;
- **`FENIXSPOON_SOLVER_MODULES`**, a comma-separated list of module paths, for an adapter that
  lives in the application's own tree and is not worth packaging.

Both are loaded *after* the built-ins, which is the whole of the shadowing rule: a plugin
claiming `dolfinx.poisson` loses, and does not win by importing first.

**A failure here is data.** Three things can go wrong and each has a tempting wrong answer.
Letting an import error propagate takes down a server whose other capabilities are fine.
Letting a name collision propagate turns a plugin author's bug into an operator's outage.
And saying nothing leaves an operator unable to tell "not installed" from "installed and
exploded" — a missing capability with no reason attached, which is the exact failure the
declaration machinery exists to prevent. So each source is caught, classified and reported,
and `environment.inspect` carries the report (protocol 1.15).

One consequence is worth stating rather than hiding: a module that registers two solvers and
raises between them leaves the first one registered. The load is reported as `failed` and
lists what it did add, because a half-loaded plugin an operator can see beats a rollback that
would need the registry to support removal for the sake of a case that is already a bug.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Literal

from .registry import registered_solvers

#: The entry-point group a distribution declares to ship adapters.
#:
#: ```toml
#: [project.entry-points."fenixspoon.solvers"]
#: acoustics = "myphysics.adapters"
#: ```
#:
#: The value is a *module*: importing it is what runs the `@register` calls. Pointing at a
#: class would work for one adapter and force a second entry point for the next, and the unit
#: an author actually maintains is the module.
PLUGIN_GROUP = "fenixspoon.solvers"

#: Module paths to import, comma-separated. The unpackaged half of the same mechanism.
MODULES_ENV = "FENIXSPOON_SOLVER_MODULES"

#: Set to a truthy value to load neither source. For a deployment that wants the installed
#: set to be exactly what this repository ships.
DISABLE_ENV = "FENIXSPOON_DISABLE_PLUGINS"

_TRUTHY = {"1", "true", "yes", "on"}

PluginStatus = Literal["loaded", "failed", "disabled"]
PluginOrigin = Literal["entry-point", "environment"]


@dataclass(frozen=True)
class PluginLoad:
    """What one third-party source did when it was imported."""

    source: str
    """What the operator configured, named back to them.

    The entry-point name, or the module path as `FENIXSPOON_SOLVER_MODULES` spelled it — and on
    the two entries that are not a module at all, the environment variable responsible:
    `FENIXSPOON_DISABLE_PLUGINS` when loading is switched off, `PLUGIN_GROUP` when the installed
    metadata could not be read.
    """

    module: str
    """The module that was imported. Same as `source` for the environment variable, and empty
    on an entry that reports a condition rather than an import."""

    origin: PluginOrigin
    status: PluginStatus

    detail: str | None = None
    """Why it failed, as the exception said it. `None` on success.

    The exception's `str()` rather than a traceback: "No module named 'slepc4py'" is the
    sentence an operator needs, and a traceback in a discovery payload is a wall that also
    discloses more of the filesystem than the question warrants.
    """

    capabilities: tuple[str, ...] = ()
    """The solver names this source added to the registry — possibly non-empty on a failure.

    A tuple because the dataclass is frozen and `plugin_loads()` hands these objects out: a
    mutable list here would be a frozen record with an editable field, and a caller that
    appended to it would be editing what `environment.inspect` reports.
    """


_LOADS: list[PluginLoad] = []


def plugin_loads() -> list[PluginLoad]:
    """Every third-party load attempt of this process, in the order they ran."""
    return list(_LOADS)


def _disabled(env: Mapping[str, str]) -> bool:
    return env.get(DISABLE_ENV, "").strip().lower() in _TRUTHY


def _entry_point_sources() -> tuple[list[tuple[str, str]], PluginLoad | None]:
    """`(name, module)` for every declared entry point, plus a report if none could be read.

    A broken distribution in `site-packages` can make `entry_points()` itself raise, and this
    function exists to *find* plugins — so failing here would deny the operator the very report
    that would explain the failure. But returning an empty list and nothing else would be worse
    than either: unreadable metadata would look exactly like an installation that declares no
    plugins, which is the silence this whole module argues against. So the failure comes back as
    a load of its own.
    """
    try:
        points = metadata.entry_points(group=PLUGIN_GROUP)
    except Exception as exc:
        return [], PluginLoad(
            source=PLUGIN_GROUP,
            module="",
            origin="entry-point",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return [(point.name, point.value) for point in points], None


def _environment_sources(env: Mapping[str, str]) -> list[tuple[str, str]]:
    raw = env.get(MODULES_ENV, "")
    return [(part, part) for part in (chunk.strip() for chunk in raw.split(",")) if part]


def load_plugins(
    *,
    env: Mapping[str, str] | None = None,
    entry_points: list[tuple[str, str]] | None = None,
) -> list[PluginLoad]:
    """Import every third-party source and record what each one did.

    Replaces the recorded report rather than appending to it, so a test may run this more than
    once. `env` and `entry_points` are injectable for exactly that reason, and passing
    `entry_points=[]` is also how a caller says *no entry points* rather than *whatever this
    machine happens to have installed* — `None` reads the real metadata, which is hermetic only
    by luck.
    """
    env = os.environ if env is None else env
    _LOADS.clear()

    if _disabled(env):
        # Reported as a load rather than as an empty list: "switched off" and "nothing
        # installed" are different answers, and an operator debugging a missing capability
        # needs to be told which one this is.
        _LOADS.append(
            PluginLoad(
                source=DISABLE_ENV,
                module="",
                origin="environment",
                status="disabled",
                detail=f"{DISABLE_ENV} is set, so no plugin source was read",
            )
        )
        return plugin_loads()

    if entry_points is None:
        declared, unreadable = _entry_point_sources()
        if unreadable is not None:
            _LOADS.append(unreadable)
    else:
        declared, unreadable = list(entry_points), None

    sources: list[tuple[str, str, PluginOrigin]] = [
        (name, module, "entry-point") for name, module in declared
    ]
    sources += [(name, module, "environment") for name, module in _environment_sources(env)]

    for source, module, origin in sources:
        before = {cls.name for cls in registered_solvers()}
        status: PluginStatus = "loaded"
        detail: str | None = None
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - a stranger's import may raise anything
            # The registry is read *after* this either way: a module that registered one
            # adapter and then raised really did add it, and the report says so.
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        added = {cls.name for cls in registered_solvers()} - before
        _LOADS.append(
            PluginLoad(
                source=source,
                module=module,
                origin=origin,
                status=status,
                detail=detail,
                capabilities=tuple(sorted(added)),
            )
        )

    return plugin_loads()
