"""Content-addressed identity for a solve (roadmap M2.5, issue #47).

In an iterative loop most resubmissions are identical to something already computed: an agent
patches one control point, re-solves, patches it back, re-solves. Giving a solve an identity
derived from its *inputs* makes the second of those cost a database lookup instead of a solve.

## What goes into the hash

Everything that determines the answer, and nothing else:

- **the solver name and its declared `version`** — the version is the adapter author's
  statement that the numbers changed. Nothing can detect that automatically: an adapter can
  be rewritten from Jacobi to multigrid without a single signature changing, and the cache
  would happily serve the old answer forever. See :attr:`Solver.version`.
- **the geometry and the params, as validated models rather than as submitted.** This is the
  detail that makes the cache actually hit: a caller that omits a defaulted parameter and one
  that states it explicitly have sent different JSON and want the same answer. Hashing
  `Params.model_validate(...)` output normalises defaults *and* types, so `{"resolution": 64}`
  and `{"resolution": 64.0}` land on one key.
- **the environment** — the versions of the packages this capability declares it needs, plus
  numpy, which is what the mock solvers *are*. A dolfinx upgrade changes answers, and a cache
  that survived one would be serving results from a different program.

## What stays out, and why

The `fenixspoon` package version. It is a tempting proxy for "did anything change", and it is
wrong in both directions: a patch release that touches the HTTP layer would invalidate every
cached solve, and a release that changes an adapter's maths without bumping its `version`
would not be caught anyway. The adapter's own declaration is the precise statement; the
package version is a guess wearing a number.

## Caching is opt-in

:attr:`Solver.deterministic` defaults to **False**, so an adapter is not cached until its
author says it may be. Serving a cached answer for a solver that does not reproduce is a
wrong answer delivered fast, which is the worst outcome available here; a missed cache hit is
merely a solve. Thread counts, MPI rank decomposition and iterative solvers seeded from
wall-clock state all break reproducibility, and none of them is visible from here.
"""

import hashlib
import json
from typing import Any

#: Bumping this invalidates every stored key, which is the point: it is the escape hatch for
#: a change to the *hashing* itself. If `canonical` ever starts serialising differently, old
#: keys describe inputs by a rule that no longer applies, and silently mixing the two would
#: mean cache hits between things that were never compared.
SCHEME = "fs-cache-1"

#: Hex characters kept from the SHA-256 digest — 128 bits.
#:
#: Truncated because the key is reported in every result's provenance, and 64 characters of
#: hex is a third of a compact answer's byte budget for information a caller only ever
#: compares for equality. 128 bits puts a collision somewhere past 10^19 distinct solves in
#: one workspace, which is not a risk anybody is running. Truncating the *stored* key rather
#: than only the displayed one matters: reporting a prefix of a longer key would mean two
#: results could show the same key and not be the same solve.
DIGEST_CHARS = 32


def canonical(value: Any) -> str:
    """Serialise a value so that equal inputs produce byte-identical text.

    Two rules do the work. `sort_keys` removes dict ordering, which JSON does not define and
    which pydantic does not promise across model changes. And `-0.0` is folded to `0.0`,
    because they are the same number and different bytes — a geometry that reflects a point
    through the origin should not miss the cache over a sign bit.

    Float *formatting* needs no special handling: Python's `repr` is the shortest string that
    round-trips, and `json.dumps` uses it, so `0.1` is `0.1` on every platform and version
    this runs on. That is a property worth knowing rather than assuming, and
    `test_the_key_is_stable_across_representations` pins it.
    """
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), default=str)


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    # `-0.0 == 0.0` is True, so `or 0.0` maps both zeroes to the positive one and leaves
    # every other float alone. Booleans are ints in Python and must not be touched.
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def cache_key(
    *,
    solver: str,
    solver_version: str,
    geometry: dict[str, Any],
    params: dict[str, Any],
    environment: dict[str, str],
    conditions: dict[str, dict[str, float]] | None = None,
) -> str:
    """The identity of one solve: a hex digest over its canonical inputs.

    Labelled with :data:`SCHEME` and with each part named, so the digest cannot collide
    between two different arrangements of the same bytes — `{"a": "bc"}` and `{"ab": "c"}`
    serialise differently here even though a naive concatenation would not distinguish them.

    ``conditions`` is the resolved load case (#85), and it is **part of the identity, not
    metadata**. Two solves of one geometry with one parameter set and two different load
    cases are two different problems, and leaving them out here would serve the clamped
    answer to the caller who asked about the free one — a wrong answer delivered instantly,
    which is the single worst thing a cache can do. Omitted from the payload entirely when
    empty, so every key computed before #85 is still the key that solve gets today: a
    condition-free solve is what "no conditions" has always meant, and cooling the whole
    cache to record that would buy nothing.
    """
    payload = canonical(
        {
            "scheme": SCHEME,
            "solver": solver,
            "solver_version": solver_version,
            "geometry": geometry,
            "params": params,
            "environment": environment,
            **({"conditions": conditions} if conditions else {}),
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:DIGEST_CHARS]


def environment_fingerprint(requires: list[str]) -> dict[str, str]:
    """Versions of the packages a solve's answer depends on.

    `numpy` unconditionally — it is the implementation of every mock solver and the array
    layer under the FEniCSx ones — plus whatever the capability declared in `requires`. A
    package that is not imported contributes ``"absent"`` rather than being left out, so
    "solved before dolfinx was installed" and "solved with dolfinx" are different keys
    instead of the same one.
    """
    import sys

    fingerprint = {}
    for name in dict.fromkeys(["numpy", *requires]):
        module = sys.modules.get(name)
        if module is None:
            fingerprint[name] = "absent"
        else:
            fingerprint[name] = str(getattr(module, "__version__", "unknown"))
    return fingerprint
