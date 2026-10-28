# BaleBot — Project Overview & Reference

- **Local Path (User PC `f0f0fd0b-6b6d-4b4a-9156-5c0380fd7108`):** `D:\2 Curent project git\BaleBot`
- **GitHub Repository:** `alishafiee1/BaleBot` (`main` branch)
- **Target Server:** `rodi` (`hp@185.255.211.219`, domain `roditeam.ir`)
- **Last Checked:** 2026-10-27 (Europe/Madrid)

---

## 1. Summary & Purpose
`BaleBot` is a multi-user [Bale](https://bale.ai) messenger bot built on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It replaces the earlier single-user `bale_bridge.py` with a full multi-tenant service where:
- Every user gets automatic access to a **free base model** (default `glm-4.7-flash` on Z.ai) with a daily quota (default **100 requests/day**, resetting at midnight `Asia/Tehran`, automatically refunded on model failure).
- Users can bring their own **personal API keys (BYOK)** from any OpenAI-compatible provider (known providers or AI-discovered connection configs from public docs), encrypted with `AES-256-GCM` (`vault.py`) and bound to `(user_id, key_id)`.
- Users can switch between the free base model and models discovered under their personal keys via an interactive **model menu** (`/models`, `/model N`).
- Users can run **Hermes agent tools** (`/agent <task>`) inside an isolated, per-user unprivileged **Incus container sandbox**, which communicates with LLMs exclusively via a host-side **LLM Proxy** using short-lived, call-capped HMAC tokens (no API keys ever enter the sandbox).
- Operators manage the bot through a **Web Admin Panel** (`/panel/`) and **Admin JSON API** (`/admin/api/settings`).

---

## 2. Tech Stack & Quality
- **Language & Framework:** Python 3.12+, FastAPI, Uvicorn
- **Database:** SQLAlchemy 2 (async), PostgreSQL (`asyncpg`) in production, SQLite (`aiosqlite`) in dev
- **Security & Crypto:** `cryptography` (`AES-256-GCM` vault), `argon2-cffi` (admin passwords), `itsdangerous` (sessions/CSRF), custom `SafeHttp` (SSRF-safe, DNS-pinned outbound HTTPS)
- **Templates:** Jinja2 (server-rendered admin panel at `/panel/`)
- **Sandbox / Containers:** Incus (per-user unprivileged containers, btrfs storage pool `sandbox`, bridge `sbxbr0` `10.77.0.1/24`, network ACL `sbx-egress`, ufw rules)
- **Testing & Linting:** `pytest` + `pytest-asyncio` (96 tests using fakes `FakeBale`, `FakeModel`, `FakeProvider`, `FakeIncus` — no network or secrets needed), `ruff`, `mypy --strict`
- **Code Search:** Graphify (`graphifyy` CLI, tree-sitter local knowledge graph)

---

## 3. Project Status (Phases)
- **Phases 0–1 (Done & Tested):** Multi-user bot, 100 free requests/day with refunds on error, runtime-editable base model & quota, 6-month (183 days) message retention, webhook + long-polling modes.
- **Phase 2 (Done & Tested):** Web Admin Panel (`/panel/`) with forced first-login password change, dashboard metrics, base model configuration, user management (search, ban/unban, custom per-user daily quota), CSRF protection, login rate limiting, `SameSite=Strict` cookies.
- **Phase 3 (Done & Tested):** Personal API keys (`/addkey`, `/keys`, `/delkey`, `/addmodel`, `/models`, `/model`, `/cancel`), immediate deletion of chat messages containing keys, `AES-256-GCM` vault, AI-assisted provider config discovery from docs, and `SafeHttp` with DNS pinning.
- **Phases 4–5 (Deployed, Verified & Live on `rodi`):** Per-user Incus agent sandbox (`/agent <task>`), LLM proxy (`10.77.0.1:8001`) with `glm-4.7-flash` priority, SSE streaming translation (`to_sse`), and multi-IP failover (`SafeHttp`), `sandbox-reap` lifecycle management, systemd units, `verify-isolation.sh` (`ALL CHECKS PASSED`), and live HTTPS webhook on `https://node.roditeam.ir`.

---

## 4. Architecture & Code Layout
```
src/balebot/
  main.py             FastAPI app factory (webhook, healthz, panel) & proxy_app_factory
  cli.py              Operator CLI (create-admin, gen-vault-key, set-webhook, poll, purge, sandbox-reap)
  config.py           Pydantic settings with BALEBOT_ env prefix
  db.py               Async SQLAlchemy engine & sessionmaker
  models.py           DB tables: users, app_settings, usage_daily, messages, admin_users,
                      user_keys, user_models, pending_actions, sandboxes
  vault.py            AES-256-GCM encryption/decryption with AAD = "userkey:<user_id>:<key_id>"
  security.py         Argon2 password hashing & random password generation
  state.py            AppState container
  texts.py            All Persian user-facing strings
  gateway/
    bale.py           Bale API client (send/delete message, inline keyboards, files, webhook/updates)
    updates.py        Webhook/poll update dispatcher -> ChatService
  services/
    chat.py           Main message router, conversation history (10 turns), quota consumption/refund
    keys.py           Personal key onboarding flow, model discovery, /models menu, routing
    providers.py      Known providers & AI-extracted declarative JSON connection config + validation
    model_client.py   OpenAI-compatible chat completion client
    quota.py          Atomic daily quota counter (Asia/Tehran day) & refund logic
    settings.py       Runtime settings stored in DB (override .env defaults)
  net/
    safe_http.py      SSRF-safe HTTP client (HTTPS only, port 443, public IPs only, DNS pinning)
  sandbox/
    incus.py          Incus CLI wrapper (argument lists only, never shell strings)
    manager.py        Per-user sandbox lifecycle (clone agent-base, limits, run hermes --oneshot, collect up to 5 files, reap)
    proxy.py          LLM Proxy bound to 10.77.0.1:8001 (verifies token, enforces model & call cap, injects real key)
    tokens.py         Short-lived, run-scoped HMAC tokens for sandbox-to-proxy auth
  panel/
    routes.py         Admin web UI routes (/panel/)
    templates/        Jinja2 HTML templates
  api/
    admin.py          HTTP Basic Auth JSON API (GET/PUT /admin/api/settings)
deploy/
  incus-host-setup.sh Host storage pool, sbxbr0 bridge, sbx-egress ACL, balebot project, TLS cert, ufw rules
  agent-base-setup.sh Template container setup with pinned Hermes commit
  verify-isolation.sh Throwaway sandbox security probe (must pass before enabling /agent)
  systemd/            balebot.service, balebot-proxy.service, balebot-reap.service, balebot-reap.timer
docs/
  architecture.md, bot-commands.md, configuration.md, operations.md, security.md, code-search.md, history/proposal-fa.md
```

---

## 5. User Commands & Flows
| Command | Description |
|---|---|
| `/start` | Register/connect user to the base model and show today's quota |
| `/usage` | Show used / limit / remaining free requests today |
| `/new` | Reset conversation context (`context_reset_at` updated; old messages not sent to model) |
| `/addkey` | Start personal API key onboarding (also triggered by natural phrase like saying you want to add an API key) |
| `/keys` | List user's saved keys (shows only name, host, and last 4 chars) |
| `/delkey N` | Delete personal key `N` and its associated models |
| `/addmodel N name` | Manually register a model name under key `N` (when provider lacks `/models` endpoint) |
| `/models [page]` | Interactive inline-button menu of available models (base model + user's personal models) |
| `/model N` | Select model number `N` from `/models` |
| `/agent <task>` | Run Hermes one-shot in the user's private Incus sandbox (costs `AGENT_COST` = 5 free units, or 0 on personal key) |
| `/cancel` | Cancel an active `/addkey` flow |

---

## 6. Operator CLI Commands
Run via `uv run --no-sync python -m balebot.cli <cmd>`:
- `create-admin [--username admin]` — Creates an admin user and prints the generated password once (forced change on first login).
- `gen-vault-key` — Generates a 32-byte urlsafe-base64 `BALEBOT_VAULT_KEY`.
- `set-webhook` — Registers `<WEBHOOK_BASE_URL>/bale/webhook/<WEBHOOK_SECRET>` with Bale.
- `poll` — Runs long-polling mode for local development without a webhook.
- `purge` — Deletes stored messages older than `MESSAGE_RETENTION_DAYS` (183 days).
- `sandbox-reap` — Stops sandboxes idle > `SANDBOX_IDLE_MINUTES` (30m) and destroys sandboxes unused > `SANDBOX_DESTROY_DAYS` (14d).
