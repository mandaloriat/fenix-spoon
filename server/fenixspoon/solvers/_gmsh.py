"""Process-wide serialization for the Gmsh Python API.

Gmsh keeps its state in process globals: ``initialize()``/``finalize()`` and every
``gmsh.model.*`` call act on one shared model. The job manager runs solvers on a thread
pool (see ``jobs.py``), so two concurrent meshing calls — from the same adapter or from
two different ones — would interleave on that shared state and can corrupt a mesh or
crash the process.

Every adapter must therefore do all of its Gmsh work inside :func:`gmsh_session`, which
holds a module-level lock for the whole session. Meshing is consequently serialized
across the process; the solve itself still runs concurrently. Moving meshing to worker
processes is the way to get true parallelism, and belongs with the out-of-process job
backend (roadmap M3).
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import gmsh

_GMSH_LOCK = threading.Lock()


@contextmanager
def gmsh_session(verbose: bool = False) -> Iterator:
    """Exclusive, initialized Gmsh session; finalized (and released) on exit.

    ``interruptible=False`` skips Gmsh's signal handlers, which cannot be installed
    from a non-main thread — exactly where the job manager runs solvers.
    """
    with _GMSH_LOCK:
        gmsh.initialize(interruptible=False)
        try:
            gmsh.option.setNumber("General.Terminal", 1 if verbose else 0)
            yield gmsh
        finally:
            gmsh.finalize()
