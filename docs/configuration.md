# Configuration

All settings are environment variables with the prefix `BALEBOT_`, read from the process environment or a `.env` file.
The source of truth is [`src/balebot/config.py`](../src/balebot/config.py). Never commit `.env`.

Values the admin can change at runtime (base model URL and name, system prompt, default daily quota) live in the
database and **override** the `DEFAULT_*` values here. Change them in the panel, not in `.env`.

## Core
| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./balebot.db` | use `postgresql+asyncpg://user:pass@host/db` in production |
| `BALE_TOKEN` | empty | bot token from Bale. Only in `.env`/environment, never in code or logs |
| `WEBHOOK_BASE_URL` | `https://roditeam.ir` | public base URL; `cli set-webhook` registers `<base>/bale/webhook/<secret>` |
| `WEBHOOK_SECRET` | empty | random string in the webhook path. Empty disables the webhook |
| `BASE_MODEL_API_KEY` | empty | key for the free base model. Never shown in the panel |
| `TIMEZONE` | `Asia/Tehran` | the quota day ends at midnight in this zone |

## Base model defaults (overridden by the panel)
| Variable | Default |
|---|---|
| `DEFAULT_BASE_URL` | `https://api.z.ai/api/paas/v4` |
| `DEFAULT_BASE_MODEL` | `glm-4.7-flash` |
| `DEFAULT_SYSTEM_PROMPT` | short Persian-friendly assistant prompt |
| `DEFAULT_DAILY_QUOTA` | `100` |

## Chat limits
| Variable | Default | Meaning |
|---|---|---|
| `MESSAGE_RETENTION_DAYS` | `183` | `cli purge` deletes older messages. Run it daily from cron |
| `HISTORY_TURNS` | `10` | earlier exchanges sent to the model |
| `MAX_INPUT_CHARS` | `4000` | longer messages are refused |

## Admin panel
| Variable | Default | Meaning |
|---|---|---|
| `SESSION_SECRET` | empty | long random string. **Empty = random per start**, so everyone is logged out on restart |
| `COOKIE_SECURE` | `true` | keep `true` behind HTTPS. Only set `false` for local HTTP development |

## Personal keys
| Variable | Default | Meaning |
|---|---|---|
| `VAULT_KEY` | empty | urlsafe-base64 of 32 bytes: `python -m balebot.cli gen-vault-key`. Empty switches personal keys **off**. **Back it up outside the server: without it every stored key is unreadable.** |

## Agent sandbox (off by default)
| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_ENABLED` | `false` | master switch for `/agent` |
| `INCUS_BIN` | `incus` | CLI used to talk to Incus |
| `INCUS_PROJECT` | `balebot` | restricted Incus project the bot may manage |
| `SANDBOX_IMAGE` | `agent-base` | template container every sandbox is cloned from |
| `SANDBOX_CPU` / `SANDBOX_MEMORY` / `SANDBOX_PROCESSES` | `1` / `512MiB` / `256` | per-sandbox limits |
| `SANDBOX_DISK` | `3GiB` | root disk quota. **Must equal the template's root size.** Needs a btrfs/zfs pool, not `dir` |
| `SANDBOX_IDLE_MINUTES` | `30` | idle sandboxes are stopped |
| `SANDBOX_DESTROY_DAYS` | `14` | unused sandboxes are deleted |
| `AGENT_COST` | `5` | free-quota units one `/agent` run costs (own-key runs are free) |
| `AGENT_TIMEOUT` | `600` | seconds per run |
| `AGENT_MAX_TURNS` | `20` | Hermes turn limit |
| `AGENT_MAX_LLM_CALLS` | `40` | model calls per run, enforced by the proxy |
| `AGENT_MAX_CONCURRENT` | `3` | runs at the same time across all users |
| `AGENT_TOOLSETS` | empty | comma list passed to `hermes -t`. Empty = Hermes defaults |
| `PROXY_SECRET` | empty | 16+ random chars signing the sandbox tokens. Empty disables `/agent` |
| `PROXY_URL` | `http://10.77.0.1:8001/v1` | how a sandbox reaches the proxy (the sandbox bridge address) |

## Generating secrets
```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"      # WEBHOOK_SECRET, SESSION_SECRET, PROXY_SECRET
uv run --no-sync python -m balebot.cli gen-vault-key              # VAULT_KEY
```
