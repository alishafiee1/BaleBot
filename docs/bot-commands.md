# Bot commands and the admin panel

## Users
| Command | What it does |
|---|---|
| `/start` | connect to the base model and show today's quota |
| `/usage` | used / limit / remaining today |
| `/new` | start a fresh conversation (history is no longer sent to the model) |
| `/addkey` | add a personal API key. Saying "I want to give you an API key" starts the same flow |
| `/keys` | list your keys (only the last 4 characters are shown) |
| `/delkey N` | delete key `N` and its models |
| `/addmodel N name` | add a model by hand to key `N` (when the provider has no model list) |
| `/models [page]` | list available models with buttons; the chosen one is marked |
| `/model N` | choose model number `N` from `/models` (works without buttons) |
| `/agent <task>` | run Hermes tools in your private sandbox. Costs `AGENT_COST` free units; free on your own key |
| `/cancel` | leave the add-key conversation |

Rules users should know:
- Free-model requests count against the daily quota; **requests on your own key do not**.
- A failed model call is not charged.
- The message that contains your key is deleted by the bot immediately. If deletion fails, delete it yourself.
- A key is only ever sent to the API host the bot showed and you confirmed.
- Add-key steps expire after 15 minutes. At most 5 keys per user.

## Admin panel (`/panel/`)
| Page | Use |
|---|---|
| Login | username + password; the first login forces a password change (12+ characters) |
| Dashboard | users, active today, requests today, banned, stored messages, current base model |
| Base model | API URL (https only), model name, default daily quota, system prompt. Applied immediately |
| Users | search, ban / unban, per-user daily quota (empty = default) |
| Password | change your password |

The base model's API key is **not** editable in the panel; it is `BALEBOT_BASE_MODEL_API_KEY` on the server.

## Admin JSON API
`GET` / `PUT` `/admin/api/settings` with HTTP Basic auth (same admin accounts). Fields: `base_url` (https),
`base_model`, `system_prompt`, `daily_quota`.

## Operator commands (`python -m balebot.cli ...`)
| Command | Use |
|---|---|
| `create-admin [--username admin]` | create an admin. The random password is shown **once** |
| `gen-vault-key` | make a `VAULT_KEY` |
| `set-webhook` | register the webhook with Bale |
| `poll` | development: long-polling instead of a webhook |
| `purge` | delete messages older than the retention period |
| `sandbox-reap` | stop idle sandboxes and destroy old ones |
