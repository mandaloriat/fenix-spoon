# 0002 — The workspace over HTTP

**Status:** accepted — *decisions 1, 2, 5 and 6 are implemented as protocol 1.10, decision 3 as
protocol 1.11; decision 4 is not yet*
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
which means a caller cannot construct a URL whose type and prefix disagree.

*Implementation note, added after the fact and correcting this record rather than the code:*
`design/g-12` does not 404 as written above. It is a **422**, because `parse_ref` refuses the
reference as malformed — a design id starts with `d`, the two halves encode the same fact, and
a mismatch means the caller assembled the string wrongly. Its docstring has said so since #44:
"much better caught here than by a lookup that quietly finds nothing." The stricter answer is
the better one, and the record was wrong to promise the weaker.

**The revision is a query parameter, not a path segment.** `/api/v1/objects/geometry/g-12/3`
would
make a revision look like a sub-resource with its own identity, and it is not one — it is a
*view* of the object at a point in its history. A pinned reference and a head reference return
the same shape from the same route, which is exactly what the local API does.

`PATCH` takes `application/json-patch+json`, the media type RFC 6902 defines. A patch is not
a partial object and the header should not claim it is.

### 2. `POST /api/v1/jobs` accepts a design reference, and that is the actual payoff

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

### 4. Waiting is a convenience, not an operation — so `optimize.run` stops blocking

`optimize.run` blocks for the whole search: eleven solves in the shipped example, minutes on a
FEniCSx adapter. That is fine on a pipe a child process owns and impossible over HTTP, where
the request is held open through whatever proxies, load balancers and browser timeouts sit
between a page and the server, and those close it long before a real search ends.

**The first draft of this record concluded that the two transports must therefore diverge**,
and proposed teaching the conformance suite that the divergence was intended. That was the
most expensive decision in this document and the one flagged for review, so it got looked at
hardest — and it turns out to be unnecessary. The codebase already answered this question, for
jobs, and the answer is better:

> **`job submit` and `study run` wait by default.** […] `--detach` skips the wait.

`job.submit` over JSON-RPC returns *immediately*, with a receipt. The CLI waits — and nobody
calls that a divergence, because the waiting is not part of the operation. It is a **convenience
belonging to the caller-facing layer**, built out of the two primitives underneath: start, then
poll. HTTP declines the convenience because it cannot hold a request open; the CLI offers it
because a person at a terminal wants the answer, not a receipt and a second command.

So the corrected decision is that `optimize.run` joins that pattern rather than breaking it:

- **over every transport it returns as soon as the search is accepted**, exactly as
  `job.submit` and `study.run` do;
- **the CLI and the Python API keep waiting**, through the same `_settle` machinery that
  already waits for the other two, so nothing regresses for the caller who noticed;
- **`optimize.get` is identical everywhere**, and always was.

There is then **no divergence to declare**, no exception to teach the conformance suite, and
no precedent for the next person to lean on. The rule that every transport answers the same
request identically survives intact — which matters more than this feature does, because that
rule is what has kept five transports from drifting apart for two milestones.

*The cost is honest and small: this changes a behaviour that shipped in #22 hours ago.*
`optimize.run` currently returns the finished report over JSON-RPC and will return a receipt.
It has no external consumers yet, and the alternative is carrying the wrong shape forever
because it was written down first.

What remains true from the first draft is the mechanism: the search keeps running after the
call that started it returns, so it becomes a **background task owned by the server process** —
the first thing here that is neither a job nor a request, and the one genuinely new piece of
machinery in this whole design.

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

That has been a footnote for ten releases and becomes a live hazard the moment this ships.

**And the footnote is necessary but not sufficient, which this record got wrong in its first
draft.** It proposed landing the bump with a fixture pinning `"1.10" > "1.9"` — an assertion
that is *false*, and false in exactly the second way the note does not mention. Compared as a
float, `1.10` becomes `1.1` and sorts below `1.9`. Compared as a **string**, `"1.10"` also
sorts below `"1.9"`, because `'1' < '9'` at the third character. Taking "it is a string" as
the advice and comparing strings gets the same wrong answer by a different route, and the
record nearly shipped that as a test.

The rule that survives both is the one the SDK already implements and this pass verified
rather than assumed: `checkProtocolCompatibility` matches `^(\d+)\.(\d+)$` and compares the two
halves **as integers**, refusing anything it cannot parse. So the SDK handles 1.10 correctly
today, with no change — the hazard is entirely for third-party clients, and for prose that
tells them half the story.

Two things belong with the bump, then. The corpus gains 1.10 as a version case, so both suites
exercise the two-integer parse against a minor that breaks naive comparisons; and the
wire-protocol document's sentence widens from "not a number" to "not a number **and not a
string comparison** — split it and compare the halves".

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

**The conformance corpus grows nothing it has never had**, which is the outcome of decision 4
and worth recording as a result rather than a non-event. The first draft was going to teach the
suite its first intended divergence; asking whether that was really necessary produced a shape
where it is not. `optimize.run` gains a conformance case of the ordinary kind — every transport
answers it identically — and the rule that has kept five transports together stays absolute.

**One shipped behaviour changes**, and it is the price of the above: `optimize.run` stops
returning the finished report over JSON-RPC and returns a receipt like its two siblings. Hours
old, no external consumers, and the alternative is keeping a shape because it was written down
first.

**Estimated shape of the work**, in landable pieces rather than one change:

1. Objects on HTTP (decisions 1, 5) + SDK + conformance. The protocol bump lands here.
2. `POST /api/v1/jobs` by reference (decision 2). Small, and the one with the most immediate
   value.
3. Studies: create, run, read (decision 3) + the sweep view in the browser. Closes #21.
4. Optimizations: the background task, and `optimize.run` becoming non-blocking on every
   transport with the wait moving to the CLI and the Python API (decision 4). Closes #22's
   first half in the browser.

Pieces 1 and 2 are worth doing together and are worth doing first; 3 is the demonstrable one;
4 is the one carrying a genuinely new mechanism and should be last, so that the mechanism is
introduced against a surface that already exists.

**What this record does not settle** and the implementation will have to: whether the object
routes are listed in `environment.inspect` so a client can feature-detect them, and whether a
`study.run` that is already running is idempotent or a `409`. Both are small; neither changes
the shape above; both are named here so they are decided rather than defaulted.

*Answered while building piece 3, and the second one was not a decision after all.* A
`study.run` on a study that is already running is **idempotent**, and nothing was added to make
it so: every variation goes through `submit`, and the result cache has matched `queued` and
`running` jobs since #47 — that is how two identical submissions attach to one solve instead of
racing. So the second run returns the same job ids with `reused` counting them, which is what a
caller who pressed the button twice should get. Recording it here because a property that falls
out of an existing rule is easy to mistake for an accident, and the next person to touch the
cache should know a route depends on it.
