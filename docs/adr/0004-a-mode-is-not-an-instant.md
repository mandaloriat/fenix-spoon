# 0004 — A mode is not an instant

**Status:** accepted — *implemented as protocol 1.14,
[#101](https://github.com/mandaloriat/fenix-spoon/issues/101)*
**Affects:** `ArtifactRef`, `ResultEnvelope`, `@fenix-spoon/client`, the conformance corpus

## Context

An eigensolve returns an ordered family: mode 1, mode 2, mode 3, each with a frequency and a
shape. Protocol 1.7 already carries an ordered family of fields — a transient's stored
instants — and it carries it well:

> the artifact carries the instant it holds in `t`, and the result's `frames` lists them in
> time order … an index naming something the result does not serve is not a case to validate,
> it is unrepresentable.

That mechanism is exactly right for modes. **The question is whether the field is.** `t` is a
float on an artifact, and a mode number would fit in it: the first draft of the eigensolver
wrote `ctx.artifact("mode_4.vtk", t=4.0)` and everything downstream worked — the index built,
the ordering was right, the files were served, and no test failed.

[#101](https://github.com/mandaloriat/fenix-spoon/issues/101) flagged it anyway, as *"a
decision rather than an oversight"*, which is why it gets a record rather than a commit
message.

## Decision

### 1. A mode number gets its own field. `t` keeps meaning time.

`ArtifactRef.mode` and a derived `ResultEnvelope.modes`, mirroring `t` and `frames` in every
respect except the name and the type.

`t` is documented as *"the instant this frame holds, in the solver's time unit"* — a quantity,
with a unit, on which differences are meaningful. A mode number is an **ordinal**:
dimensionless, 1-based, and with no metric on it. The gap between mode 1 and mode 2 is not a
duration, an interval, or anything else a consumer may do arithmetic on. Two things that
disagree about all of that do not belong in one slot merely because both are floats.

The failure this avoids is concrete rather than aesthetic. A viewer with a time slider — the
consumer 1.7 was built for — would label mode 3 as three seconds. `frames`, which every 1.7
client already reads, would silently start listing modes. And a consumer receiving a result
with a populated `frames` would have no way to tell which of the two families it was holding
without knowing which capability produced it, which is precisely the *"read the quantity off
the context"* failure this protocol keeps refusing (see [ADR 0001](0001-explorable-viewer.md),
decisions 2 and 3).

### 2. The two are mutually exclusive on one file, and the model refuses the combination.

A file holds an instant or a mode. One carrying both would appear in two derived indices,
ordered two ways, and a consumer reading either would be told something the other denies. So
`ArtifactRef` raises, and so does `SolverContext.artifact` — at *registration*, because the
compact levels and the local API never build an envelope and a rule enforced only on the wire
is one half the callers walk past. That is the same reasoning #86 gave for putting the frame
cap where the file is registered.

### 3. Rejected: one generalised index with a declared quantity.

The tempting third option was to replace both with something like
`index: {quantity: "mode", value: 4}`, so the next ordered family — a load step, a frequency
in a harmonic sweep, an iteration of an optimisation — needs no third field.

It was rejected on the same grounds this repository rejects an expression language for
selectors: it buys generality nobody has asked for, at the cost of making the *existing*
shape harder to consume. Every 1.7 client would have to change to keep reading time, a
`quantity` string would be an open vocabulary a consumer has to branch on, and the two
families that actually exist are both closed and both known. Two named fields are two lines
of model and zero lines of consumer logic; the general form is the reverse.

If a third ordered family ever arrives, this decision should be revisited *then*, with three
cases in hand instead of one hypothetical.

### 4. The frequency does not travel on the artifact. The join key is the mode number.

A `ModeRef` carries the mode number and the artifact name, and nothing else. The frequency
lives in the spectrum — a `series1d` whose abscissa **is** the mode number — so a caller joins
"mode 4 is at 537 Hz" to mode 4's shape by that number on both sides.

Copying the frequency onto the artifact would put one number in two places, which is the
arrangement in which they eventually disagree. Relying on the two lists being ordered alike
would be positional identity, which `Series1DData` already refuses for the same reason it
requires a `name`.

## Consequences

- 1.14 is additive: a client that never receives a `mode` sees exactly what 1.13 showed it,
  and `frames` on a transient is unchanged.
- An eigensolve needed **no new result kind, no new route, and no new level**. The spectrum is
  a `series1d`, the shapes are artifacts, and the index is derived — which is the outcome
  #101 predicted and the reason the capability came first.
- Rigid-body modes are part of the index like any other mode. They are not filtered out of
  the spectrum, because their *count* is the check that says the model is the one its author
  described — three for an unrestrained plane structure, zero for a restrained one.
- A future ordered family will need its own field and its own derived list, and will have to
  argue for itself here. That is the cost of decision 3, accepted knowingly.
