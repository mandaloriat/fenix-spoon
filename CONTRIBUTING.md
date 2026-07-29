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

## Repository setup that CI cannot do itself

**GitHub Pages, once per fork.** The `Docs` workflow deploys the site on every merge to
`main`, but it cannot turn Pages on: `actions/configure-pages` will only do that given a
token other than `GITHUB_TOKEN` — a PAT with `repo` scope, or a GitHub App with
`administration:write`. A long-lived credential in repository secrets is a worse trade
than one click, so the click stays:

> Settings → Pages → Build and deployment → Source → **GitHub Actions**

Until that is set, the workflow builds the site correctly and then fails on the last step
with `Get Pages site failed`. Everything else in CI is self-contained.

## Code style

- Python: `ruff` (configured in `server/pyproject.toml`), type hints on public APIs.
- Keep the wire protocol JSON-first and language-neutral: the server must never leak Python-specific
  types into API payloads.

## License

By contributing you agree that your contributions are licensed under the MIT license.
