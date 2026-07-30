"""JSON-RPC 2.0 over stdio — the base local transport (roadmap M2.5, issue #45).

A local caller should not have to run a web server and open a port to solve a problem on its
own machine. This adapter puts the same operations the HTTP API exposes onto the process's
own pipes: the agent spawns `fenix-spoon rpc --stdio`, writes requests to its stdin and reads
responses from its stdout. No port is opened, no origin policy applies, and the child dies
with its parent.

It is an adapter over :class:`~fenixspoon.core.FenixSpoonCore` and contains no application
logic — the same rule the HTTP layer follows, and the reason MCP (#49) can be layered on
these operations rather than underneath them. Nothing here imports FastAPI, which
`test_the_rpc_adapter_does_not_import_fastapi` enforces: a caller that wants a pipe should
not have to install a web framework to get one.

The pieces:

- :mod:`.framing` — how a message is delimited, and why newline-delimited JSON.
- :mod:`.methods` — the vocabulary: method names and the core call behind each.
- :mod:`.errors` — domain errors to JSON-RPC codes, the counterpart of `http_errors`.
- :mod:`.encoding` — core objects to JSON-encodable data.
- :mod:`.server` — the loop, and why a running solve never blocks the channel.
"""

from . import encoding, errors, framing, methods, server
from .methods import METHODS, MethodError
from .server import RpcServer, serve_stdio

__all__ = [
    "METHODS",
    "MethodError",
    "RpcServer",
    "encoding",
    "errors",
    "framing",
    "methods",
    "serve_stdio",
    "server",
]
