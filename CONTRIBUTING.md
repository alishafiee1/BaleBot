# Contributing

## Set up
```sh
uv venv && uv pip install -e . pytest pytest-asyncio ruff
cp .env.example .env
```
Python 3.12+. Tests need no network, no Bale token and no Incus.

## Before every commit
```sh
uv run --no-sync ruff format src tests
uv run --no-sync ruff check src tests
uv run --no-sync pytest -q
```
All three must be clean. Do not commit with failing tests.

## How the code is organised
See [docs/architecture.md](docs/architecture.md). Quick rules:
- User-facing text goes in `texts.py` (Persian). Code, identifiers and docs are English.
- Settings go in `config.py` **and** [docs/configuration.md](docs/configuration.md) **and** `.env.example`.
- Never hold a database session across a network call.
- Anything that talks to a host a user or an AI chose must go through `SafeHttp`.
- Anything that runs a program passes an **argument list**, never a shell string.

## Security rules (non-negotiable)
- No secret in code, tests, logs, error messages, docs or commit messages. Tests use obvious fake values.
- A key must never enter a prompt, a log line, the pending-action table, or a sandbox.
- Do not log full URLs from the Bale client (the token is in the URL) or exception messages from HTTP calls.
- A change to the vault, `safe_http`, the proxy, the sandbox manager or the deploy scripts needs a test that fails without it.
- Deploy scripts start with `exec </dev/null`. Test shell scripts with `bash -n` at least.

## Tests
- `tests/conftest.py` has the fakes: `FakeBale`, `FakeModel`, `FakeProvider` (a pretend API host) and `FakeIncus`.
- Prefer testing behaviour through `ChatService` / the HTTP apps over testing private helpers.
- A fake can hide real-world problems (the DNS pinning bug was only found against a real host). When you touch networking, also try it live once.

## Searching the code
Use Graphify: [docs/code-search.md](docs/code-search.md).

## Commits and changes
- Small, focused commits with a clear message in the imperative ("Add ...", "Fix ...").
- Update [CHANGELOG.md](CHANGELOG.md) for anything a user or operator would notice.
- Update the docs in the same commit as the behaviour they describe.

## Database changes
Tables are created with `create_all` for now. Before production, add Alembic and make every schema change a migration;
never edit a deployed table by hand.
