"""How a JSON-RPC message is delimited on a pipe (roadmap M2.5, issue #45).

The issue left framing open and asked for it to be decided "by what the MCP adapter needs
and whether any payload can contain a raw newline". Both questions have answers:

**Nothing we emit can contain a raw newline.** `json.dumps` escapes U+000A inside strings
as `\\n`, and with the default `ensure_ascii=True` it also escapes U+2028 and U+2029 — the
two characters that are *not* newlines to Python's `readline` but *are* line terminators to
several other languages' line splitters. So the encoder guarantees one frame is one line
under any reader's definition of a line, not merely under ours. That guarantee is what a
newline delimiter needs to be safe, and it is asserted in
`test_no_encodable_value_can_break_the_frame` rather than assumed.

**MCP's stdio transport is newline-delimited**, and #49 layers on this one. Choosing header
framing here would mean the MCP adapter either re-frames every message or diverges from the
transport it is supposed to be a thin layer over.

So: **newline-delimited JSON is what we write**. What we *read* is either — the header form
costs about fifteen lines, and a caller built against an LSP-style client should not have to
care. :func:`read_message` detects per message, so the two can even be mixed on one stream.

The cost of ASCII escaping is real but small: it inflates non-ASCII text, and this
vocabulary is numbers, identifiers and English prose. Compactness that could corrupt a frame
in someone else's line reader is not a trade worth making.
"""

import json
from typing import Any

#: Compact and ASCII-safe. `separators` drops the spaces `json.dumps` adds by default, which
#: is free; `ensure_ascii` is the guarantee above, and is the default spelled out because it
#: is load-bearing here rather than incidental.
_ENCODE: dict[str, Any] = {"separators": (",", ":"), "ensure_ascii": True}


class FramingError(Exception):
    """A frame could not be read as a message. Answered with JSON-RPC `-32700`."""


def encode(message: dict[str, Any]) -> bytes:
    """One message as one line, newline included."""
    return json.dumps(message, **_ENCODE).encode("utf-8") + b"\n"


def encode_with_headers(message: dict[str, Any]) -> bytes:
    """One message in LSP/MCP header framing, for a caller that asked for it."""
    body = json.dumps(message, **_ENCODE).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


async def read_message(reader) -> dict[str, Any] | None:
    """Read one message off an `asyncio.StreamReader`, or `None` at end of input.

    Detection is a consequence of the two framings rather than a guess about them: a JSON
    value on the wire starts with `{` or `[`, and a header block starts with a field name,
    so the first byte of the first line separates them with nothing left over. Blank lines
    between frames are skipped, which is what makes a sender that terminates a header body
    with CRLF interoperable with one that does not.

    `None` means the writer closed the pipe — for a stdio server that is the parent process
    going away, which is the documented way this transport ends.
    """
    line = await reader.readline()
    while line in (b"\n", b"\r\n"):
        line = await reader.readline()
    if not line:
        return None

    stripped = line.strip()
    if stripped[:1] in (b"{", b"["):
        return _loads(stripped)

    headers: dict[bytes, bytes] = {}
    while stripped:
        name, sep, value = stripped.partition(b":")
        if not sep:
            raise FramingError(f"expected a JSON value or a header line, got {line[:60]!r}")
        headers[name.strip().lower()] = value.strip()
        stripped = (await reader.readline()).strip()

    raw_length = headers.get(b"content-length")
    if raw_length is None:
        raise FramingError("header-framed message has no Content-Length")
    try:
        length = int(raw_length)
    except ValueError:
        raise FramingError(f"Content-Length is not a number: {raw_length!r}") from None
    # `readexactly` raises `IncompleteReadError` on a truncated body. That is a framing
    # failure like any other and is reported as one, rather than as end of input — the
    # difference matters, because end of input means "shut down" and this means "the peer
    # sent something wrong".
    try:
        body = await reader.readexactly(length)
    except EOFError as exc:  # asyncio.IncompleteReadError subclasses EOFError
        raise FramingError(f"stream ended {length} bytes into a message body") from exc
    return _loads(body)


def _loads(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FramingError(f"not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        # A JSON-RPC *batch* is a list, and this is where that lands. Refusing it here
        # rather than in the dispatcher keeps the reason in one place — see
        # `server.RpcServer` for why batches are not supported.
        raise FramingError(
            "expected a JSON-RPC request object; "
            f"got a bare {type(value).__name__}. Batches are not supported"
        )
    return value
