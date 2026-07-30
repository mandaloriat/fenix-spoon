"""The JSON-RPC 2.0 server loop over a pair of pipes (roadmap M2.5, issue #45).

Reads requests from one stream, writes responses to another, and never lets a running solve
block either. That last property is the reason this is an asyncio loop and not a
read-dispatch-write `while` over `sys.stdin`: a solve takes seconds to minutes, and a
transport that answered `job.get` only after the solve it started had finished would make
asynchronous jobs a fiction.

Three decisions, each with the reason it went that way:

**Requests are handled concurrently, so responses can come back out of order.** JSON-RPC
correlates by `id`, so this is legal, and it is what makes `job.submit` followed immediately
by `job.get` work while the first is still in the executor. A client that requires ordering
gets it by waiting for each response, which is what a synchronous client does anyway.

**Batches are refused.** The spec has them; this does not. On a channel that already accepts
pipelined requests, a batch buys nothing but questions — is it ordered, is it atomic, what
does a partial failure return — and the answers would be invented here rather than settled
by the spec. MCP dropped batching for the same reason. The refusal is explicit
(`framing._loads` names it), not a parse error the caller has to guess at.

**End of input ends the process.** The parent closing the pipe is how this transport is
supposed to end — there is no shutdown method to call and no socket to leave listening.
"""

import asyncio
import logging
from typing import Any

from ..core import FenixSpoonCore, errors
from ..core.identity import Principal
from . import errors as rpc_errors
from . import framing
from .encoding import jsonable
from .methods import CONNECTION_METHODS, MethodError, _params, resolve

log = logging.getLogger(__name__)

VERSION_FIELD = "2.0"


class RpcServer:
    """Serves the method table over one pair of streams.

    One instance per connection, which for stdio means one per process. It holds the
    subscriptions it started so they can be cancelled when the peer goes away — a leaked
    subscription task would keep a finished process alive waiting for events nobody reads.
    """

    def __init__(
        self,
        core: FenixSpoonCore,
        principal: Principal,
        *,
        headers: bool = False,
    ) -> None:
        self.core = core
        self.principal = principal
        #: Emit LSP-style `Content-Length` frames instead of newline-delimited JSON. Reading
        #: accepts both either way; this is only about what we write, for a client whose
        #: reader cannot do NDJSON.
        self.headers = headers
        self._outbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self._subscriptions: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ lifecycle

    async def serve(self, reader: asyncio.StreamReader, writer) -> None:
        """Read until end of input, then drain what is still in flight and stop."""
        pump = asyncio.create_task(self._pump(writer))
        try:
            await self._read_loop(reader)
        finally:
            # In-flight requests finish before the writer closes: a caller that pipelined
            # three requests and closed its end of the pipe still gets three answers, which
            # is what makes `printf '...' | fenix-spoon rpc --stdio` a usable thing to type.
            for task in list(self._subscriptions.values()):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await self._outbox.put(None)
            await pump

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                message = await framing.read_message(reader)
            except framing.FramingError as exc:
                # `id` is unknown by definition — the frame did not parse — and the spec
                # says to answer with a null id rather than to stay silent.
                await self._send(_error_response(None, rpc_errors.PARSE_ERROR, str(exc)))
                continue
            if message is None:
                return
            task = asyncio.create_task(self._handle(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _pump(self, writer) -> None:
        """Serialize every outbound frame through one writer.

        Concurrent handlers and progress notifications share one pipe, and two coroutines
        writing to it directly would interleave halfway through a frame. A queue makes the
        ordering of *bytes* a property of this loop rather than of the scheduler.
        """
        while True:
            payload = await self._outbox.get()
            if payload is None:
                break
            writer.write(payload)
            drain = getattr(writer, "drain", None)
            if drain is not None:
                await drain()

    async def _send(self, message: dict[str, Any]) -> None:
        encode = framing.encode_with_headers if self.headers else framing.encode
        await self._outbox.put(encode(message))

    # ------------------------------------------------------------------- dispatch

    async def _handle(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        # A request without an `id` is a notification: the spec says answer nothing at all,
        # including when it fails. Failures are logged instead, because dropping them
        # silently would make a misspelled notification indistinguishable from a working one.
        notification = "id" not in message
        if not notification and not _is_valid_id(request_id):
            # Refused up front rather than echoed. An `id` the spec does not allow cannot be
            # sent back in a response without the *response* becoming non-conformant too —
            # so the one error a client would use to find its bug would itself be malformed.
            # `null` is what the spec says to answer with when the id is unusable. Raised in
            # review of #45.
            await self._send(_error_response(
                None,
                rpc_errors.INVALID_REQUEST,
                f"`id` must be a string, a number or null, got {type(request_id).__name__}",
            ))
            return
        try:
            response = await self._invoke(message)
        except Exception:  # noqa: BLE001 — the last line of defence, see below
            # A handler raising something that is neither a `CoreError` nor a `MethodError`
            # is a bug in this server, not a caller error. It becomes `-32603` with no
            # detail: a traceback on the wire tells an attacker about the filesystem and
            # tells an agent nothing it can act on. The traceback goes to the log, where
            # the operator can read it.
            log.exception("unhandled error in %r", message.get("method"))
            if notification:
                return
            await self._send(
                _error_response(request_id, rpc_errors.INTERNAL_ERROR, "internal error")
            )
            return
        if not notification:
            await self._send(response)

    async def _invoke(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        if message.get("jsonrpc") != VERSION_FIELD:
            return _error_response(
                request_id,
                rpc_errors.INVALID_REQUEST,
                f"every request must carry \"jsonrpc\": \"{VERSION_FIELD}\"",
            )
        method = message.get("method")
        if not isinstance(method, str):
            return _error_response(
                request_id, rpc_errors.INVALID_REQUEST, "`method` must be a string"
            )
        try:
            params = _params(message.get("params"))
            if method in CONNECTION_METHODS:
                result = await self._subscription(method, params)
            else:
                handler = resolve(method)
                result = handler(self.core, self.principal, params)
                if asyncio.iscoroutine(result):
                    result = await result
        except MethodError as exc:
            return _error_response(request_id, exc.code, exc.message, exc.data)
        except errors.CoreError as exc:
            return {
                "jsonrpc": VERSION_FIELD,
                "id": request_id,
                "error": rpc_errors.error_object(exc),
            }
        return {"jsonrpc": VERSION_FIELD, "id": request_id, "result": jsonable(result)}

    # --------------------------------------------------------------- subscriptions

    async def _subscription(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Start or stop a progress stream for one job.

        Handled here rather than in the method table because these two are the only methods
        whose effect is on the *connection* rather than on the core: they have no meaning
        for a caller that is not holding this pipe open, and the CLI and Python adapters
        (#50) will not bind them.
        """
        job_id = params.get("job_id")
        if not isinstance(job_id, str):
            raise MethodError(
                rpc_errors.INVALID_PARAMS,
                "missing required parameter 'job_id'",
                {"type": "MissingParameter", "parameter": "job_id"},
            )
        if method == "job.unsubscribe":
            task = self._subscriptions.pop(job_id, None)
            if task is not None:
                task.cancel()
            return {"job_id": job_id, "subscribed": False}

        job = self.core.job(job_id, self.principal)  # ownership check before we stream it
        if job_id not in self._subscriptions:
            task = asyncio.create_task(self._stream(job))
            self._subscriptions[job_id] = task
            task.add_done_callback(lambda _t, jid=job_id: self._subscriptions.pop(jid, None))
        return {"job_id": job_id, "subscribed": True}

    async def _stream(self, job) -> None:
        """Push this job's events as `job.progress` notifications until it ends.

        The same iterator the WebSocket route uses, so replay works identically: history
        first, then live, de-duplicated by sequence number.
        """
        try:
            async for event in self.core.events(job):
                await self._send(
                    {
                        "jsonrpc": VERSION_FIELD,
                        "method": "job.progress",
                        "params": {"job_id": job.id, "event": event},
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # A broken stream must not take the connection with it: the caller can still
            # poll `job.events`, which reads the same log.
            log.exception("progress stream for %s ended early", job.id)


def _is_valid_id(request_id: Any) -> bool:
    """Whether `id` is something the specification allows: String, Number or Null.

    Booleans are excluded deliberately and are the reason this is not a one-line `isinstance`:
    `True` is an `int` in Python, so the obvious check would accept an id JSON-RPC does not
    have. Fractional numbers *are* accepted — the spec only says they SHOULD NOT be used, and
    refusing a client over a recommendation would be inventing a rule to enforce.
    """
    return request_id is None or (
        isinstance(request_id, str | int | float) and not isinstance(request_id, bool)
    )


def _error_response(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": VERSION_FIELD, "id": request_id, "error": error}


async def serve_stdio(core: FenixSpoonCore, principal: Principal, *, headers: bool = False) -> None:
    """Run a server on this process's own stdin and stdout.

    stdout is claimed by the protocol, so anything that writes to it corrupts the stream.
    Logging is configured onto stderr by the CLI before this is called; a solver adapter
    that `print()`s would still break it, which is worth knowing and is why the entry point
    says so.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    import sys

    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    await RpcServer(core, principal, headers=headers).serve(reader, writer)
