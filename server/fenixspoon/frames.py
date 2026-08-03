"""The time index: which instants a solve stored, and where each one lives (#86).

A leaf module for the same reason :mod:`fenixspoon.series` is one — ``protocol`` imports
``solvers.base`` for :class:`ProgressEvent`, so anything both of them need has to live below
both or the import graph closes on itself.

## The whole time dimension is an index

A transient produces two things of different natures, and the design already had an opinion
about each. **Scalars against time** are curves: `T_max(t)`, a probe history, energy
accumulated — `series1d` has carried those since #69, and they answer most transient
questions without a field crossing the wire. **Fields against time** are the large thing, and
large things cross as *references*.

So an adapter writes one file per stored instant and the result carries a list of
``(t, artifact)`` pairs. No new result kind, no arrays added to any payload, and a viewer's
time slider becomes "fetch the instant you are looking at" — the only workable shape anyway,
since a browser cannot hold fifty fields in memory.

## The limit, accepted rather than discovered

There is **no server-side reduction at an arbitrary instant**: "what is the peak at t = 120 s"
is not answerable without downloading a frame. The reasoning is that a scalar-against-time
question is already answered by the curve, and a field question means you are looking at a
picture. If a caller ever needs the reduction, this index is what it would be built on.
"""

from pydantic import BaseModel, Field

#: How many instants one result may index. Generous for anything a caller would page through
#: and far below the point where the index becomes the payload: a thousand-step run is
#: expected to save every tenth step, not every one. The cap exists for the same reason the
#: series limits do — a list of references is cheap right up until it is used to smuggle the
#: history past the compact-answer rule.
MAX_FRAMES = 512


class FrameRef(BaseModel):
    """One stored instant of a time-dependent solve: when it is, and which file holds it."""

    t: float = Field(description="The instant this frame holds, in the solver's time unit.")
    artifact: str = Field(
        description="Name of the artifact holding it; must be one this result lists."
    )


def frames_of(artifacts) -> list[FrameRef]:
    """The time index of a result: its artifacts that carry an instant, in time order.

    **Derived, never stored.** The instant lives on the artifact that holds it, so the index
    and the files cannot disagree and a frame pointing at something this result does not
    serve is not a bug to guard against — it is unrepresentable. That is worth more than the
    marginal directness of a second list, and it is why no storage column was added for this:
    the artifact list is already persisted.

    Accepts anything with `name` and `t` attributes, which is both `ArtifactRef` on the wire
    and `ArtifactView` in the compact levels.
    """
    framed = [item for item in artifacts if getattr(item, "t", None) is not None]
    return [
        FrameRef(t=float(item.t), artifact=item.name)
        for item in sorted(framed, key=lambda item: item.t)
    ]


def check_frame_count(artifacts) -> None:
    """Refuse an index long enough to be the payload it is supposed to replace."""
    framed = sum(1 for item in artifacts if getattr(item, "t", None) is not None)
    if framed > MAX_FRAMES:
        raise ValueError(
            f"{framed} frames exceeds the {MAX_FRAMES} a result may index; save fewer "
            "instants rather than moving the history into the envelope"
        )
