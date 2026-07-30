"""Availability-aware solver registry: name -> Solver class."""

from .base import Solver, SolverInfo

_REGISTRY: dict[str, type[Solver]] = {}


def register(cls: type[Solver]) -> type[Solver]:
    """Class decorator: make a solver visible to the API."""
    if cls.name in _REGISTRY:
        raise ValueError(f"solver name already registered: {cls.name!r}")
    _REGISTRY[cls.name] = cls
    return cls


def get_solver(name: str) -> type[Solver] | None:
    return _REGISTRY.get(name)


def registered_solvers() -> list[type[Solver]]:
    """Every installed solver class, in registration order.

    The classes rather than their :class:`SolverInfo`, because discovery (#43) reports on
    the *declaration* — metrics, artifacts, features, whether `estimate_cells` is
    overridden — and none of that fits in a payload built for a form generator.
    """
    return list(_REGISTRY.values())


def available_solvers() -> list[SolverInfo]:
    return [cls.info() for cls in _REGISTRY.values()]
