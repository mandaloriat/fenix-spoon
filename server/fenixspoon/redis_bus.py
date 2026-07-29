"""Redis pub/sub event bus (roadmap M3, issue #12).

The in-process bus fans events out through asyncio queues, which works exactly as long as
the publisher and the subscriber are the same process. Once solves run in worker
containers they are not: the worker publishes, an API process holds the WebSocket, and
neither knows the other exists. Redis pub/sub is the hop between them.

One channel per job, ``fenixspoon:events:<job_id>``, carrying ``{"seq": n, "event": {…}}``.
Pub/sub is fire-and-forget — a subscriber that attaches late gets nothing from before it
attached — which is exactly why :meth:`JobManager.subscribe` attaches *first* and then
replays the durable log from the store. The bus is the live edge; the store is the
history. Neither has to be reliable alone.

Imports ``redis.asyncio``, so this module is only loaded when a Redis bus is configured;
``pip install fenixspoon[redis]``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as aioredis

from .events import Delivery, EventBus, Subscription

log = logging.getLogger(__name__)

CHANNEL_PREFIX = "fenixspoon:events:"


def channel_for(job_id: str) -> str:
    return f"{CHANNEL_PREFIX}{job_id}"


class RedisEventBus(EventBus):
    """Publishes to and subscribes from Redis pub/sub.

    Holds one connection pool. Each subscription opens its own pubsub connection, which
    is how redis-py works — a connection in subscribe mode can do nothing else.
    """

    def __init__(self, url: str, client: aioredis.Redis | None = None) -> None:
        self.url = url
        self._client = client if client is not None else aioredis.from_url(url)

    async def publish(self, job_id: str, seq: int, event: dict[str, Any]) -> None:
        payload = json.dumps({"seq": seq, "event": event})
        await self._client.publish(channel_for(job_id), payload)

    def subscribe(self, job_id: str) -> "RedisSubscription":
        return RedisSubscription(self._client, job_id)

    async def close(self) -> None:
        await self._client.aclose()


class RedisSubscription(Subscription):
    def __init__(self, client: aioredis.Redis, job_id: str) -> None:
        self._client = client
        self._job_id = job_id
        self._pubsub: Any = None
        self._ready = asyncio.Event()

    async def __aenter__(self) -> "RedisSubscription":
        # Subscribing must complete before the caller reads stored history, or an event
        # published in the gap is lost from both paths. Awaiting it here is what makes
        # "attach first, then replay" true rather than merely intended.
        self._pubsub = self._client.pubsub()
        await self._pubsub.subscribe(channel_for(self._job_id))
        self._ready.set()
        return self

    async def __aiter__(self) -> AsyncIterator[Delivery]:
        if self._pubsub is None:
            raise RuntimeError("use the subscription as an async context manager")
        while self._pubsub is not None:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message is None:
                continue  # timeout tick: lets cancellation land promptly
            try:
                payload = json.loads(message["data"])
                yield int(payload["seq"]), payload["event"]
            except (ValueError, KeyError, TypeError):
                # A malformed message on the channel must not kill a live stream; some
                # other process is misbehaving, and this subscriber can only skip it.
                log.warning("dropping malformed event on %s", channel_for(self._job_id))

    async def aclose(self) -> None:
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            await pubsub.unsubscribe(channel_for(self._job_id))
            await pubsub.aclose()
