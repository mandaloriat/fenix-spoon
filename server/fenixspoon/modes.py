"""The mode index: which mode shapes a solve stored, and where each one lives (#101).

A leaf module beside :mod:`fenixspoon.frames`, and deliberately its twin — same shape, same
derivation, one field different. Read that module first; this one is the answer to the
question it raises by existing.

## Why not `t`

An eigensolve produces the same *kind* of output a transient does: a scalar per member of an
ordered family (frequencies, temperatures over time) and a field per member, too large to
inline. 1.7 solved the second half for time — the artifacts carrying an instant *are* the
index — and the mechanism is exactly right here. The name is not.

`t` is "the instant this frame holds, in the solver's time unit". A mode number is an
**ordinal**: dimensionless, 1-based, and with no metric on it — the gap between mode 1 and
mode 2 is not a quantity. Putting one in the other's slot would make `frames` sort modes by
"time", a viewer's time slider read "mode 3" as three seconds, and every consumer of 1.7 have
to ask which of the two meanings a given result meant. That is the failure this repository
names most often: a number whose meaning has to be guessed from context.

So the ordering gets a field of its own, and the two are **mutually exclusive** on one file —
a file holds an instant or a mode, never both, and :class:`~fenixspoon.protocol.ArtifactRef`
refuses the combination rather than picking one. See
[ADR 0004](../../../docs/adr/0004-a-mode-is-not-an-instant.md).

## What is *not* here

The frequency. A mode's frequency is a number in the spectrum the result already carries as a
`series1d`, whose abscissa is the mode number — so the join between "mode 7's shape" and
"mode 7's frequency" is by that number on both sides, never by position in two lists. Copying
the frequency onto the artifact would be a second place for it to live, and the two could
disagree.
"""

from pydantic import BaseModel, Field

#: How many mode shapes one result may index.
#:
#: The same reasoning as :data:`~fenixspoon.frames.MAX_FRAMES` and a smaller number, because a
#: modal model is smaller than a history: fifty modes is a large plant model (the adaptive
#: mirror in #101 wants exactly that), and a caller asking for two hundred is describing a
#: different kind of study than this index was built for.
MAX_MODES = 256


class ModeRef(BaseModel):
    """One stored mode shape: which mode it is, and which file holds it."""

    mode: int = Field(
        description=(
            "1-based mode number, ascending in frequency and **counting rigid-body modes**. "
            "It is the same number the spectrum's abscissa carries, which is what makes the "
            "shape and its frequency joinable without relying on list order."
        )
    )
    artifact: str = Field(
        description="Name of the artifact holding it; must be one this result lists."
    )


def modes_of(artifacts) -> list[ModeRef]:
    """The mode index of a result: its artifacts that carry a mode number, in mode order.

    **Derived, never stored**, for the reason `frames_of` is: the mode lives on the file that
    holds it, so an index naming a file the result does not serve is unrepresentable rather
    than tested for.

    Accepts anything with `name` and `mode` attributes — `ArtifactRef` on the wire and
    `ArtifactView` in the compact levels.
    """
    indexed = [item for item in artifacts if getattr(item, "mode", None) is not None]
    return [
        ModeRef(mode=int(item.mode), artifact=item.name)
        for item in sorted(indexed, key=lambda item: item.mode)
    ]


def check_mode_count(artifacts) -> None:
    """Refuse an index long enough to be the payload it is supposed to replace."""
    indexed = sum(1 for item in artifacts if getattr(item, "mode", None) is not None)
    if indexed > MAX_MODES:
        raise ValueError(
            f"{indexed} mode shapes exceeds the {MAX_MODES} a result may index; ask for "
            "fewer modes rather than moving the modal basis into the envelope"
        )
