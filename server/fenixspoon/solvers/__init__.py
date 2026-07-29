"""Solver adapters. Importing this package populates the registry with every
solver whose dependencies are available in the current environment."""

from . import mock_heat as _mock_heat  # noqa: F401  (registers itself)
from . import mock_laplace as _mock_laplace  # noqa: F401
from . import mock_magnetostatics as _mock_magnetostatics  # noqa: F401
from .base import (
    JobCancelled,
    ProgressEvent,
    Solver,
    SolverContext,
    SolverInfo,
    SolverResult,
)
from .registry import available_solvers, get_solver, register

# The FEniCSx adapters register only if dolfinx + gmsh import cleanly.
try:
    from . import dolfinx_heat as _dolfinx_heat  # noqa: F401
    from . import dolfinx_magnetostatics as _dolfinx_magnetostatics  # noqa: F401
    from . import dolfinx_poisson as _dolfinx_poisson  # noqa: F401
except ImportError:  # pragma: no cover - exercised only without dolfinx
    pass

__all__ = [
    "JobCancelled",
    "ProgressEvent",
    "Solver",
    "SolverContext",
    "SolverInfo",
    "SolverResult",
    "available_solvers",
    "get_solver",
    "register",
]
