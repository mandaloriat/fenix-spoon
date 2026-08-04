# 0002 — The workspace over HTTP

**Status:** proposed — *this record is the design pass, and no code implements it yet*
**Affects:** the wire protocol (`/api/v1`), `@fenix-spoon/client`, the conformance corpus,
[#21](https://github.com/mandaloriat/fenix-spoon/issues/21) and
[#22](https://github.com/mandaloriat/fenix-spoon/issues/22), both waiting on it

## Context

The workspace — versioned `geometry`, `material`, `load_case`, `design`, `study` and
`optimization` objects under stable ids, patched rather than resent — has been reachable from
a local process since [#44](https://github.com/mandaloriat/fenix-spoon/issues/44) and from
nothing else. `/api/v1` has jobs, results and discovery; it has no idea the workspace exists,
beyond a path in `environment.inspect`.

That was a decision rather than an omission, and #44 wrote down its reason:

> …binding it here first would mean designing an object API that the transport it exists for
> might want differently.

**That reason has expired, and its expiry is the reason to open this now.** JSON-RPC got the
object API first, has carried it through five releases, and three consumers have been built
on it — the CLI, the Python API and the MCP adapter. There is no longer a shape to guess at.
There is a shape that works, in production use on the local side, and the question is what it
looks like bound to HTTP.

Two shipped features are waiting on the answer, and both are visible from where they stopped:

- **Sweeps** ([#21](https://github.com/mandaloriat/fenix-spoon/issues/21)) return a response
  curve as `Series1DData` — the exact model `<fs-plot>` draws — and no browser can obtain one.
- **Optimization** ([#22](https://github.com/mandaloriat/fenix-spoon/issues/22)) returns a
  convergence history in the same shape, with the same gap.

So the toolkit's flagship client is the one client that cannot reach the last two milestones'
worth of work. This record decides the shape; the implementation is deliberately separate.

## What this is not

It is **not** "expose the core over HTTP". The MCP adapter's curation argument applies here in
a different key: a browser is not an agent, and the operations a page needs are a subset. Two
things stay off HTTP in this design and say why below — `workspace.open` and object deletion,
the second because it does not exist anywhere.

It is also not a second job system. Everything below resolves to the same
`FenixSpoonCore` calls the other four transports use, or it is wrong.

## Decision

### 1. Objects are a resource collection, and a reference is not a path segment

A reference is `geometry:g-12`, optionally `geometry:g-12@3`. Putting that in a path means
percent-encoding a colon and an at-sign into every URL a page constructs, and getting
`geometry%3Ag-12%403` in every log line and every browser address bar. The alternative reads
better and is what the identifier already decomposes into:

```
POST   /api/v1/objects/{type}                 create, 201 + the object
GET    /api/v1/objects/{type}/{id}            the head revision
GET    /api/v1/objects/{type}/{id}?revision=3 a pinned one
GET    /api/v1/objects/{type}/{id}/revisions  which revisions exist
PATCH  /api/v1/objects/{type}/{id}            RFC 6902, 200 + the new revision
GET    /api/v1/objects                        list, `?type=` to filter
```

`{type}` and `{id}` are the two halves of the reference and nothing else. `parse_ref` already
splits them, so a route builds the canonical reference back from its own path parameters —
which means a caller cannot construct a URL whose type and prefix disagree (`design/g-12`
resolves to `design:g-12`, which does not exist, and 404s honestly).

**The revision is a query parameter, not a path segment.** `/objects/geometry/g-12/3` would
make a revision look like a sub-resource with its own identity, and it is not one — it is a
*view* of the object at a point in its history. A pinned reference and a head reference return
the same shape from the same route, which is exactly what the local API does.

`PATCH` takes `application/json-patch+json`, the media type RFC 6902 defines. A patch is not
a partial object and the header should not claim it is.

### 2. `POST /jobs` accepts a design reference, and that is the actual payoff

Everything above is plumbing until this:

```jsonc
{ "design": "design:d-18" }                      // instead of solver + geometry + params
```

The browser path today resends the entire outline on every iteration. The workspace exists
because that is ruinous for a caller whose context is scarce — an agent — and merely wasteful
for one whose isn't. But a page that moves one control point and re-solves gets the same
benefit for a different reason: **the result cache can only recognise an identical solve, and
provenance can only name object revisions if the caller submitted some.** A browser submitting
inline geometry can never say *which design revision this picture came from*.

`SubmitParams` on the JSON-RPC side already accepts either form and refuses the ambiguous
combination. The HTTP model gains the same field with the same refusal, because two transports
disagreeing about what a submission may contain is exactly what the conformance suite exists
to prevent.

### 3. A study and an optimization are objects that *run*, not endpoints that compute

#21 sketched `POST /api/v1/sweeps` with a parameter grid in the body. This design refuses that
shape, and the reason is the one #48 built the study abstraction on:

> a study's job list is a pure function of its object revision.

A sweep posted as a body is a computation with no identity. You cannot pin it, cannot re-run
it and get the cache, cannot ask what it found later, and cannot show two colleagues the same
sweep. The object *is* the question — so the browser creates one like any other object and
then runs it:

```
POST /api/v1/objects/study                    create the question
POST /api/v1/studies/{id}/run       → 202     start it
GET  /api/v1/studies/{id}                     the table and the curves
```

Same for `optimizations`. Two routes per kind rather than one, and the extra route is the
thing that makes the answer re-readable a week later.

### 4. `optimize.run` cannot cross HTTP the way it crosses a pipe

**This is the one place the local shape does not survive the binding, and it is worth being
loud about it.** `optimize.run` blocks for the whole search — eleven solves in the shipped
example, minutes on a FEniCSx adapter. That is fine on a pipe a child process owns. Over HTTP
it is a request held open through whatever proxies, load balancers and browser timeouts sit
between a page and the server, and those will close it long before a real search ends.

So the HTTP binding **diverges deliberately**: `POST /optimizations/{id}/run` answers `202`
immediately, and the trajectory is polled from `GET /optimizations/{id}` — which already
works, because `optimize.get` replays the search from the jobs rather than reading a stored
run. The design that made the optimizer need no run record is what makes it pollable.

Two consequences to accept with open eyes. First, the search has to keep running after the
request that started it returns, which means it becomes a **background task owned by the
server process** — the first thing in this codebase that is neither a job nor a request.
Second, `optimize.run` over JSON-RPC and `POST /run` over HTTP will return *different things*
(a report and a receipt), which is the first real divergence between transports and must be
written into the conformance corpus as an intended one, or the suite will read it as drift.

An alternative was considered and rejected: making an optimization *be* a job, so the existing
lifecycle carries it. It fails on the cell budget and the quota — an optimization is N jobs and
each must meet those individually, which is the property that stops a study being a way around
them. Wrapping the sequence in one job would make the wrapper's cost a lie.

### 5. Ownership already exists. What changes is that it starts mattering

The first draft of this record claimed per-principal isolation was the expensive part of the
work. Reading the code says otherwise: every workspace method already takes an owner, every
read goes through one check, and somebody else's reference already returns "not found" rather
than "forbidden", with the same reasoning jobs use — confirming that an id exists is itself a
disclosure. HTTP inherits all of it by construction, because the route calls the same core.

So the honest statement is narrower and less comfortable: **that check has never been under
pressure.** On a machine where every caller is the same principal it is unfalsifiable — it
could have been wrong for a year and nothing would have failed. Exposing objects to the web is
the moment it becomes load-bearing, so the work is not to build it but to *prove* it: the
tests that earn their place here are the negative ones, one per verb, each asserting that
another principal's reference is not readable, not patchable, and not runnable.

**And there is a real gap next to it.** Quotas count concurrent jobs, jobs per hour and
artifact bytes — nothing counts objects, because until now creating one was free and local.
Over HTTP an authenticated caller can create objects without bound. That is not a reason to
delay this, but it is a decision to take deliberately rather than discover: either an object
quota joins the other three, or the deployment recipe says plainly that the object store is
unmetered and belongs behind a trusted boundary. This record's recommendation is the former,
sized like the artifact-bytes quota — a count, refused at create, with no `Retry-After`,
because waiting an hour does not relieve it.

### 6. This is protocol 1.10, and the version string finally earns its argument

New endpoints are additive: MINOR, same `/api/v1`, and a 1.0 client is unaffected. Which makes
this **1.10** — and the wire-protocol document has warned since 1.0 that

> `protocol` is a **string**, not a number: as a float, `1.10` parses to `1.1` and sorts below
> `1.9`.

That has been a footnote for ten releases. It becomes a live hazard the moment this ships, and
any client that ever parsed the version with a float comparison breaks here. The bump should
land with a fixture pinning `"1.10" > "1.9"` under whatever comparison the SDK uses, so the
first version where the reasoning bites is also the version that tests it.

### 7. What stays off HTTP, and why

- **`workspace.open`** — it answers "where is the workspace directory on this machine". Over
  HTTP that is either meaningless or a filesystem disclosure. The parts a browser legitimately
  wants (object counts, the types available) are in `environment.inspect` already.
- **Deletion** — there is no `object.delete` on any transport. Objects are kept forever, on
  purpose, and #44 gave the reason: a job is a computation and losing it costs a re-run; an
  object is something a person authored, and losing it is data loss. HTTP does not get a verb
  the local API declined to have.
- **`design.resolve`** — the shape a browser needs is on the job it submitted, in `provenance`.
  It can be added later if a page turns out to want a dry run; nothing here forecloses it.

## Consequences

**Two half-finished milestone items become finishable**, and neither needs a new idea after
this: #21's browser half is `<fs-plot>` reading a `Series1DData` it can now fetch, and #22's
is a progress view over a poll that already exists.

**The security surface does not grow so much as start being used.** Per-principal isolation
on objects is already written and already correct; what it has never been is *tested against an
adversary*, because on a local machine there is not one. The negative tests are the deliverable
here, and the unmetered object store (decision 5) is the one genuine gap.

**The SDK grows an object client**, with the same runtime validators the result path has. That
is the part that keeps a browser from having to know the reference grammar.

**The conformance corpus grows a case it has never had**: an intended divergence between
transports (decision 4). Every previous case asserts that five renderings agree; this one has
to assert that two of them differ, and say why, or the suite becomes a reason not to do the
right thing.

**Estimated shape of the work**, in landable pieces rather than one change:

1. Objects on HTTP (decisions 1, 5) + SDK + conformance. The protocol bump lands here.
2. `POST /jobs` by reference (decision 2). Small, and the one with the most immediate value.
3. Studies: create, run, read (decision 3) + the sweep view in the browser. Closes #21.
4. Optimizations, including the background-task question (decision 4). Closes #22's first half
   in the browser.

Pieces 1 and 2 are worth doing together and are worth doing first; 3 is the demonstrable one;
4 is the one carrying a genuinely new mechanism and should be last, so that the mechanism is
introduced against a surface that already exists.

**What this record does not settle** and the implementation will have to: whether the object
routes are listed in `environment.inspect` so a client can feature-detect them, and whether a
`study.run` that is already running is idempotent or a `409`. Both are small; neither changes
the shape above; both are named here so they are decided rather than defaulted.
