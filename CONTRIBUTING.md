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

## Changing the wire protocol

The protocol is versioned `MAJOR.MINOR`. Which one you are bumping — if either — is decided by
the table in [the wire protocol doc](https://mandaloriat.github.io/fenix-spoon/04-wire-protocol/#what-is-a-breaking-change),
not by how large the change feels. Adding an optional field is a MINOR bump however much code
it took; renaming one is a MAJOR bump however small the diff.

**Most protocol changes need no bump at all.** A new field description, a clearer error
message, a new solver — none of those change the contract. Bump only when a client's view of
it changes.

### A MINOR bump (additive)

1. `PROTOCOL_VERSION` in `server/fenixspoon/protocol.py`.
2. `PROTOCOL_VERSION` in `client/packages/client/src/types.ts`.
3. `protocol_version` in `protocol/fixtures/version.json`, plus fixtures covering the new
   shape — a valid case, and an invalid one showing what the rule rejects.
4. Regenerate the protocol reference: `make protocol-reference`.
5. Update `docs/04-wire-protocol.md` prose if the change is visible to a reader.

Steps 1–3 are not optional and not independent: both test suites assert against the fixture,
so changing one alone fails CI on the other side. That is deliberate — it is the only thing
stopping the server and the SDK from drifting apart quietly.

### A MAJOR bump (breaking)

Everything above, plus:

1. Change the router prefix in `server/fenixspoon/api.py` — the major version *is* the path
   segment, and `test_the_major_version_matches_the_path` enforces it.
2. Keep the previous major mounted alongside the new one for a deprecation window. Two majors
   are meant to coexist; that is what putting the major in the path is for.
3. Version the fixture corpus so it holds cases for both, rather than replacing the old ones.
   A corpus that only describes the current version cannot prove the old one still works.
4. Say what breaks and what to do about it, in the release notes and in the docs.

An older client meeting a newer major should get a comprehensible refusal, not a parse error.
`checkProtocolCompatibility` in the SDK is the client-side half of that.

## Code style

- Python: `ruff` (configured in `server/pyproject.toml`), type hints on public APIs.
- Keep the wire protocol JSON-first and language-neutral: the server must never leak Python-specific
  types into API payloads.

## License

By contributing you agree that your contributions are licensed under the MIT license.
