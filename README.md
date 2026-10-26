# BaleBot

A multi-user [Bale](https://bale.ai) bot on top of [Hermes](https://github.com/NousResearch/hermes-agent).
Every user chats with a free base model (100 requests a day), can bring their own API keys for any
OpenAI-compatible provider, and can run Hermes tools inside a private, locked-down sandbox.

| | |
|---|---|
| Stack | Python 3.12+, FastAPI, SQLAlchemy 2 (async), PostgreSQL (SQLite for dev), Incus |
| Tests | 92 tests, `pytest`; lint with `ruff` |
| Status | Phases 0-3 done and tested. Phase 4 (sandbox) is built and tested against fakes; the real server is being set up and **isolation is not yet verified** (see [security](docs/security.md)). |

## What it does
- **Free base model.** `/start` connects the user to the base model chosen in the admin panel. 100 requests per day
  per user (Asia/Tehran day), refunded when the model fails.
- **Personal API keys.** The user says "I want to give you an API key". The bot works out how to talk to that provider,
  asks for the key, deletes the message that held it, and stores it encrypted. Other users never see it.
- **Model menu.** Each user picks from the base model plus the models of their own keys.
- **Agent sandbox.** `/agent <task>` runs Hermes tools in the user's own unprivileged container. The container holds no keys.
- **Admin panel.** Web UI at `/panel/` for the base model, quotas and users.

## Quick start (development)
```sh
uv venv && uv pip install -e . pytest pytest-asyncio ruff
cp .env.example .env                                    # fill in what you need
uv run --no-sync pytest                                  # all tests, no network, no secrets
uv run --no-sync python -m balebot.cli create-admin      # prints the admin password once
uv run --no-sync python -m balebot.cli poll              # talk to a real bot without a webhook
```
Production setup, the sandbox and rollback are in [docs/operations.md](docs/operations.md).

## Documentation
| Document | Read it to |
|---|---|
| [docs/architecture.md](docs/architecture.md) | understand the parts and how a message flows |
| [docs/configuration.md](docs/configuration.md) | find every setting |
| [docs/bot-commands.md](docs/bot-commands.md) | see what users and admins can do |
| [docs/security.md](docs/security.md) | see the threat model, the controls and what is **not** proven yet |
| [docs/operations.md](docs/operations.md) | deploy, back up, debug and roll back |
| [docs/code-search.md](docs/code-search.md) | search the code as a graph with Graphify |
| [CONTRIBUTING.md](CONTRIBUTING.md) | set up, test, and the rules for changes |
| [CHANGELOG.md](CHANGELOG.md) | see what changed |
| [docs/history/proposal-fa.md](docs/history/proposal-fa.md) | read the original design proposal (Persian, historical) |

## Project layout
```
src/balebot/
  gateway/    Bale API client and update handling
  services/   chat, quota, personal keys, provider configs, model client, runtime settings
  sandbox/    Incus runner, per-user sandbox manager, LLM proxy, proxy tokens
  net/        SSRF-safe outbound HTTP
  panel/      admin web panel (Jinja2, server rendered)
  api/        admin JSON API
  vault.py    AES-256-GCM encryption for user keys
deploy/       server setup scripts and systemd units
tests/        pytest (fakes for Bale, models, providers and Incus)
```
