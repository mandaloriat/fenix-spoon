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
from dataclasses import dataclass, field
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
    """The entry-point name, or the module path as the environment spelled it."""

    module: str
    """The module that was imported. Same as `source` for the environment variable."""

    origin: PluginOrigin
    status: PluginStatus

    detail: str | None = None
    """Why it failed, as the exception said it. `None` on success.

    The exception's `str()` rather than a traceback: "No module named 'slepc4py'" is the
    sentence an operator needs, and a traceback in a discovery payload is a wall that also
    discloses more of the filesystem than the question warrants.
    """

    capabilities: list[str] = field(default_factory=list)
    """The solver names this source added to the registry — possibly non-empty on a failure."""


_LOADS: list[PluginLoad] = []


def plugin_loads() -> list[PluginLoad]:
    """Every third-party load attempt of this process, in the order they ran."""
    return list(_LOADS)


def _disabled(env: Mapping[str, str]) -> bool:
    return env.get(DISABLE_ENV, "").strip().lower() in _TRUTHY


def _entry_point_sources() -> list[tuple[str, str]]:
    """`(name, module)` for every declared entry point, or nothing if the metadata is unreadable.

    A broken distribution in `site-packages` can make `entry_points()` itself raise, and this
    function exists to *find* plugins — failing here would deny the operator the very report
    that would explain the failure.
    """
    try:
        points = metadata.entry_points(group=PLUGIN_GROUP)
    except Exception:  # pragma: no cover - depends on a corrupt installation
        return []
    return [(point.name, point.value) for point in points]


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
    once. `env` and `entry_points` are injectable for exactly that reason — the alternative is
    monkeypatching `importlib.metadata`, which tests the patch.
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

    sources: list[tuple[str, str, PluginOrigin]] = [
        (name, module, "entry-point")
        for name, module in (
            _entry_point_sources() if entry_points is None else list(entry_points)
        )
    ]
    sources += [(name, module, "environment") for name, module in _environment_sources(env)]

    for source, module, origin in sources:
        before = {cls.name for cls in registered_solvers()}
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - a stranger's import may raise anything
            added = {cls.name for cls in registered_solvers()} - before
            _LOADS.append(
                PluginLoad(
                    source=source,
                    module=module,
                    origin=origin,
                    status="failed",
                    detail=f"{type(exc).__name__}: {exc}",
                    capabilities=sorted(added),
                )
            )
            continue
        added = {cls.name for cls in registered_solvers()} - before
        _LOADS.append(
            PluginLoad(
                source=source,
                module=module,
                origin=origin,
                status="loaded",
                capabilities=sorted(added),
            )
        )

    return plugin_loads()
