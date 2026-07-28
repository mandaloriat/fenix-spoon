# Contributing to Fenix Spoon

Thanks for your interest! The project is in its kickstart phase, so contributions of every size are
welcome — from typo fixes to whole milestone items.

## Where to start

1. Read the [architecture](docs/02-architecture.md) and the [roadmap](docs/03-roadmap.md).
2. Pick an open issue (roadmap items are tracked as issues) or open one to discuss an idea first.
3. For anything that touches the [wire protocol](docs/04-wire-protocol.md), open a discussion
   before coding — the protocol is the contract everything else depends on.

## Development setup

Server (Python ≥ 3.11):

```bash
cd server
pip install -e ".[dev]"
pytest            # run the test suite
ruff check .      # lint
```

The mock solver keeps the server fully functional without FEniCSx installed. To work on the
FEniCSx adapter, use the Docker image:

```bash
docker compose up --build
```

## Pull requests

- Keep PRs focused; one logical change per PR.
- Add or update tests for behavior changes. The mock-solver path must always stay green without
  FEniCSx installed.
- New solvers implement the `Solver` protocol in `server/fenixspoon/solvers/base.py` and register
  themselves in the registry; see `mock_laplace.py` for a template.

## Code style

- Python: `ruff` (configured in `server/pyproject.toml`), type hints on public APIs.
- Keep the wire protocol JSON-first and language-neutral: the server must never leak Python-specific
  types into API payloads.

## License

By contributing you agree that your contributions are licensed under the MIT license.
