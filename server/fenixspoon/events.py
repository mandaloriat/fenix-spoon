"""Progress-event delivery (roadmap M3, issue #12).

Separated from the job manager because this is the piece that has to cross a process
boundary once solves move to workers. The manager publishes an event; subscribers — the
WebSocket handlers — receive it. Whether that hop is an in-memory queue or Redis pub/sub
is the bus's business, not the manager's.

**Every event carries the sequence number the store assigned it.** That number is what
makes replay exact: a subscriber attaches to the live stream *first*, then reads the
stored history, then drops anything live whose seq it already replayed. Without it the
two sources can only be reconciled by counting, which breaks the moment they interleave
differently — and across a process boundary they will.

The seq is internal plumbing: it is stripped before the event reaches a client, so the
wire protocol is unchanged.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

# One event as it moves through the bus: the store's sequence number, and the payload
# that eventually reaches the client unchanged.
Delivery = tuple[int, dict[str, Any]]


class EventBus(ABC):
    """Fan-out for job progress events."""

    #: Short identifier reported by `environment.inspect` (#43). Worth reporting next to
    #: the execution backend: the two have to agree, and a mismatch's only symptom is a
    #: progress bar that never moves.
    kind = "unknown"

    @abstractmethod
    async def publish(self, job_id: str, seq: int, event: dict[str, Any]) -> None:
        """Deliver one event to every current subscriber of ``job_id``."""

    @abstractmethod
    def subscribe(self, job_id: str) -> "Subscription":
        """Start buffering events for ``job_id``.

        Call this *before* reading stored history, or an event published in between is
        lost. The returned object is an async context manager.
        """

    async def close(self) -> None:
        """Release whatever the bus holds. Nothing to do for the in-process one."""
        return None


class Subscription(ABC):
    """A live event feed for one job, with a defined start point."""

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[Delivery]:
        """Yield ``(seq, event)`` as they arrive. Never ends on its own — the caller
        stops on a terminal event."""

    @abstractmethod
    async def aclose(self) -> None:
        """Stop receiving. Idempotent."""


class InProcessEventBus(EventBus):
    """The dev default: one asyncio queue per subscriber, no broker."""

    kind = "in-process"

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    async def publish(self, job_id: str, seq: int, event: dict[str, Any]) -> None:
        for queue in self._subscribers.get(job_id, ()):
            queue.put_nowait((seq, event))

    def subscribe(self, job_id: str) -> "InProcessSubscription":
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(job_id, set()).add(queue)
        return InProcessSubscription(self, job_id, queue)

    def _unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(job_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._subscribers[job_id]


class InProcessSubscription(Subscription):
    def __init__(self, bus: InProcessEventBus, job_id: str, queue: asyncio.Queue) -> None:
        self._bus = bus
        self._job_id = job_id
        self._queue: asyncio.Queue | None = queue

    async def __aiter__(self) -> AsyncIterator[Delivery]:
        while self._queue is not None:
            yield await self._queue.get()

    async def aclose(self) -> None:
        if self._queue is not None:
            self._bus._unsubscribe(self._job_id, self._queue)
            self._queue = None
