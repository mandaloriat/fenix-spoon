# 0005 — Thin about physics, thick about claims

**Status:** accepted — *no protocol change; this record is an admission test for the ones that
come after it*
**Affects:** every future addition to the wire protocol, and the argument a new geometry kind,
result kind or field has to win before it is written

## Context

This project is not meant to be a cookbook. A cookbook grows by accumulating recipes: one more
geometry kind because a user drew that shape, one more field because an adapter found it handy,
one more result kind per physics. It ends with a protocol whose surface is the union of
everybody's convenience, where nothing can be refused because every addition was justified by
someone wanting it.

The stated alternative is that Fenix Spoon should be a **transparent** protocol for a wide range
of ordinary FEniCS use. That word carries two readings and they pull opposite ways:

- **thin** — the protocol adds as little as possible over FEniCS, and every addition is a
  liability;
- **inspectable** — the protocol carries enough that a consumer can see what an answer assumes,
  and an omission is a liability.

Both are defensible and they cannot both be the rule, because they disagree about the sign of a
new field. What resolves it is a commitment the protocol has already made in one place and now has
to make everywhere: `Assumption.excludes` exists so that asking a potential-flow solver about drag
gets *"a definite **no** rather than a plausible zero"* ([wire protocol](../04-wire-protocol.md)).
That is the whole project in one field. A protocol that reports a number without carrying what the
number assumes produces answers that look right, which is the specific failure the design exists
to avoid — so where thin and inspectable disagree, the verifiable reading wins.

Until now the answer has been given one case at a time, and the records show it: the axis label
belongs to the kind ([ADR 0003](0003-axisymmetric-axis-label.md)) but a mode number gets a field
of its own ([ADR 0004](0004-a-mode-is-not-an-instant.md)), and a generalised index was rejected
while a whole new geometry kind was accepted. Those are consistent, but nothing written down says
*why*, so the next case starts the argument from nothing.

## Decision

### 1. The protocol is thin about physics.

It adds no physical semantics FEniCS does not already have. It does not invent a material model,
does not define what a contact is, does not translate a PDE into a private vocabulary, and does
not put UFL on the wire in either direction. Where FEniCS has a notion, the protocol names it and
stops; where FEniCS has none, the protocol does not invent one as a courtesy to a caller.

That is the reading of *transparent* that governs anything a solve is made of.

### 2. The protocol is thick about claims.

What a capability asserts, what it refuses, and how a caller checks either is protocol, and being
thin about it is a defect rather than a virtue. `MetricSpec`, `Assumption`, `ConditionSpec`,
`ArtifactSpec`, named boundaries, the conformance corpus, the cross-validated adapter pair — none
of that is physics. It is the structure that makes an answer checkable instead of plausible.

A protocol that passes a number without saying what the number assumes is exactly the protocol
that produces plausible answers. Thinness there buys nothing and costs the property the project
is for.

### 3. The admission test: an addition earns its place when it makes something *refusable or declarable* that would otherwise be implicit.

Decisions 1 and 2 are not in tension until an addition would give the protocol semantics FEniCS
lacks. That is when the test applies, and it is one question:

> Is there something the server can now **refuse**, or a consumer can now **check**, that without
> this addition lives only inside an adapter's source?

If yes, the addition is contract. If no — if it only makes a case more convenient to express — it
is a recipe, and it belongs in an adapter or in a caller.

### 4. Worked against the case that looks like a counter-example.

`axisymmetric2d` (1.13) **does** add semantics FEniCS lacks. A dolfinx mesh in two dimensions has
coordinates called `x[0]` and `x[1]`; nothing in the kernel knows whether the first is an abscissa
or a radius, because the revolution lives only in the weak form the adapter writes — the `r`
weight, the `2π` in the energy. FEniCS cannot refuse `rmin < 0`, since for FEniCS there is no `r`.

It passes the test, and by a wide margin. `rmin >= 0` becomes validatable; a region may sit on
r = 0 *because* that edge is an axis and nowhere else; a plane solver refuses a meridian section
with a `422` instead of answering a different problem confidently. Every one of those is a refusal
that did not exist before and could not exist in the adapter alone. The kind was added in order to
be able to say no.

The contrast makes the test sharp:

- A hypothetical `beam1d`, added because beams are common, fails. It refuses nothing a caller
  could not already be refused, and the convenience is a recipe.
- `spline2d` and `step3d` are still listed as planned kinds. The test says the question is not
  "does someone want to draw that shape" but "what can be refused or declared once the protocol
  knows the shape is a spline". That question has not been answered, and this record is what makes
  answering it a prerequisite rather than an afterthought.
- Nonlinearity passes, and it is vocabulary rather than an adapter. A nonlinear solve that does not
  report the residual it reached, the iterations it took and whether it converged is a plausible
  answer by construction. The declaration is the feature; the solver is the easy half.
- 3D passes as a result kind — but the renderer is not protocol. The WebGL viewer is product, and
  keeping the two separate is what stops "we need a 3D viewer" from being an argument about the
  wire.

### 5. The test does not decide breadth, and that is deliberate.

Nothing above says which physics should exist. Breadth is bought with adapters, and an adapter is
not a protocol change — which is precisely why the cookbook pressure has to be relieved somewhere
other than here.

Today it cannot be. `solvers/__init__.py` imports its adapters by hand, there are no entry points,
and [the solver guide](../start-write-a-solver.md) offers no better instruction than importing the
module at startup. So every new physics must land *in this repository*, and the only available way
to serve a wider range of use is to accumulate recipes in the thing that is supposed to refuse
them. That contradiction is named here rather than resolved here, because it is a feature with an
issue of its own to write, but this record is the reason it is not optional.

## Consequences

- A proposed addition to the wire protocol now has a question to answer in its issue, and "someone
  needs it" is not an answer. The three records above are re-readable as instances of it: 0003
  admits a kind that buys refusals, 0004 admits a field that keeps two families distinguishable,
  and 0004 decision 3 rejects a generalisation that bought neither.
- The test is about *capability to refuse*, not about size. It permits a large addition that makes
  a class of wrong payload impossible, and forbids a small one that only saves a caller some
  typing. That is the opposite ranking from "keep the protocol small", and it is the intended one.
- Third-party adapter loading stops being a nice-to-have. Without it, decision 5 has no outlet and
  the pressure returns to the protocol, one recipe at a time.
- This record is a criterion, not a rule with a validator. Nothing in CI enforces it, and it fails
  the way prose fails: quietly, by not being read. The mitigation is that it is short and that new
  kinds are rare enough for a reviewer to ask the question by hand.
- If the project ever does want to be a cookbook, this is the record to supersede — and reversing
  it deliberately is a great deal better than drifting into it.
