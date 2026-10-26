# BaleBot

Multi-user Bale bot on top of Hermes. Design: see [PROPOSAL.md](PROPOSAL.md).

## Status (phase 0 + 1 + 2)
- Multi-user: `/start` connects the user to the base model set in the admin settings
- 100 free requests/day/user (Asia/Tehran day), refunded when the model call fails
- Base model, prompt and default quota editable through the admin API (`/admin/api/settings`)
- 6-month message retention (`python -m balebot.cli purge`, run daily from cron)
- Webhook at `/bale/webhook/{secret}` (production) or `poll` (development)

- Web panel at `/panel/`: login (forced password change on first login, CSRF, rate limit, 8h session), dashboard, base model settings, users (ban, per-user quota)

- Personal API keys (phase 3): `/addkey` (or saying "I want to give you an API key"), `/keys`, `/delkey N`,
  `/addmodel N name`, model menu `/models` + `/model N` (and inline buttons). Keys are AES-256-GCM
  encrypted (bound to owner + key id), the message holding a key is deleted from the chat, the AI that
  builds a provider config never sees the key, and outbound calls are public-https-only with pinned DNS.
  Requests made with a user's own key do not use the free daily quota. Needs `BALEBOT_VAULT_KEY`
  (`python -m balebot.cli gen-vault-key`). Back that key up: without it stored keys are unreadable.

- Agent sandbox (phase 4): `/agent <task>` runs Hermes (`hermes chat --oneshot`) inside the user's own
  unprivileged Incus container. The sandbox holds no keys: it reaches models only through the LLM proxy
  (`balebot.main:proxy_app_factory`) with a short-lived, call-capped token. Off by default
  (`BALEBOT_SANDBOX_ENABLED`). **Not deployed or tested against a real Incus yet** — see
  [deploy/README.md](deploy/README.md) for the setup order, the isolation check, and known unknowns.

Not yet: Alembic migrations, per-user rate limit on own-key requests, sending user files into sandboxes.

## Setup
```sh
uv venv && uv pip install -e . pytest pytest-asyncio ruff
cp .env.example .env            # fill in token, webhook secret, base model key, session secret
uv run --no-sync pytest
uv run --no-sync python -m balebot.cli create-admin   # prints the admin password once
uv run --no-sync python -m balebot.cli poll           # dev
uv run --no-sync uvicorn balebot.main:app_factory --factory   # production, behind nginx + HTTPS
uv run --no-sync python -m balebot.cli set-webhook    # after DNS/HTTPS for roditeam.ir works
```

## Layout
`gateway/` Bale I/O · `services/` chat, quota, runtime settings, model client · `api/` admin API · `cli.py` operations
