"""The `fenix-spoon` command (roadmap M2.5, issue #45).

One subcommand so far — `fenix-spoon rpc --stdio`, the entry point the JSON-RPC transport
documents. The broader CLI, where every operation is a subcommand printing JSON, is #50; this
is deliberately not that. Adding half of it here would mean designing the command vocabulary
twice, and the JSON-RPC adapter needs exactly one command to be startable.

    $ printf '{"jsonrpc":"2.0","id":1,"method":"capability.list"}\\n' | fenix-spoon rpc --stdio

**stdout belongs to the protocol.** Logging goes to stderr, and the log level is a flag
rather than an inherited default, because a library that logs at INFO into stdout would
corrupt every frame after it. A solver adapter that `print()`s would do the same, which is
not something this can prevent — only state.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__


def _build_core():
    """A core wired the way the HTTP app wires one, minus the app.

    Imported inside the function so `--help` costs nothing: constructing a `JobManager`
    opens the store and reads the environment, which a caller asking for usage text did not
    ask for.
    """
    from .core import FenixSpoonCore
    from .jobs import JobManager

    return FenixSpoonCore(JobManager())


def _principal():
    """Who a local caller is.

    Being able to start the process *is* the authentication here — the child runs as the
    same user, with the same filesystem access, and could have run the solver directly. What
    it is not is a bypass: the principal is a real one, so jobs are owned, counted against
    whatever quotas are configured, and swept by the same retention policy as any other. See
    the [security posture](../../docs/07-local-agent-interface.md#12-security).

    `FENIXSPOON_RPC_PRINCIPAL` names it, so two agents sharing a data directory get separate
    histories, quotas and result caches rather than reading each other's jobs.
    """
    from .core.identity import ANONYMOUS, Principal, Quotas

    return Principal(
        id=os.environ.get("FENIXSPOON_RPC_PRINCIPAL", ANONYMOUS),
        quotas=Quotas.from_env(),
    )


def _run_rpc(args: argparse.Namespace) -> int:
    from .rpc import serve_stdio

    if not args.stdio:
        print(
            "fenix-spoon rpc currently serves stdio only; pass --stdio.\n"
            "There is no network listener on purpose: exposing this over a socket goes "
            "through the HTTP API's auth path, not around it.",
            file=sys.stderr,
        )
        return 2

    core = _build_core()
    stranded = core.jobs.reconcile()
    if stranded:
        logging.getLogger(__name__).warning(
            "failed %d job(s) left running by a previous process", len(stranded)
        )
    try:
        asyncio.run(serve_stdio(core, _principal(), headers=args.framing == "headers"))
    except KeyboardInterrupt:
        return 130
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fenix-spoon",
        description=__doc__.split("\n\n")[0],
    )
    parser.add_argument("--version", action="version", version=f"fenix-spoon {__version__}")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("FENIXSPOON_LOG_LEVEL", "WARNING"),
        help="stderr log level (default: WARNING, so the protocol stream stays quiet)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rpc = sub.add_parser(
        "rpc",
        help="serve JSON-RPC 2.0 over stdio",
        description=(
            "Serve the JSON-RPC 2.0 vocabulary on stdin/stdout. Call `rpc.describe` for the "
            "method list. The process exits when its input closes, so it dies with whatever "
            "spawned it."
        ),
    )
    rpc.add_argument("--stdio", action="store_true", help="serve on this process's pipes")
    rpc.add_argument(
        "--framing",
        choices=["ndjson", "headers"],
        default="ndjson",
        help=(
            "what to write: newline-delimited JSON (default), or LSP-style Content-Length "
            "frames. Both are accepted on input regardless."
        ),
    )
    rpc.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="job and workspace directory (default: $FENIXSPOON_DATA_DIR, else ./.fenix-spoon)",
    )
    rpc.set_defaults(handler=_run_rpc)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # stderr explicitly: the default handler writes to stderr already, but "already" is not a
    # guarantee, and this stream is the one thing that must never reach stdout.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if getattr(args, "data_dir", None) is not None:
        # Set before the core is built, because `JobManager` reads it at construction. A
        # flag rather than only an environment variable so a caller spawning the process can
        # point two agents at two workspaces without mutating its own environment.
        os.environ["FENIXSPOON_DATA_DIR"] = str(args.data_dir)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
