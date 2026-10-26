# Architecture

## Parts
```
 Bale ──webhook──▶ FastAPI app (balebot.main:app_factory, 127.0.0.1:8000, behind nginx + HTTPS)
                     │  gateway/updates.py     one Bale update ─▶ one reply
                     ▼
                  ChatService (services/chat.py)  ── commands and normal chat
                     ├─ KeyService (services/keys.py)  personal keys, model menu, routing
                     │     ├─ Vault (vault.py)               encrypt / decrypt user keys
                     │     └─ SafeHttp (net/safe_http.py)    the only way to reach user-chosen hosts
                     ├─ quota (services/quota.py)            daily counter, atomic
                     ├─ ModelClient (services/model_client.py) the free base model
                     └─ SandboxManager (sandbox/manager.py)  /agent ─▶ Incus
                                              │
 Admin ──browser──▶ /panel/  (panel/routes.py)│
                                              ▼
              one unprivileged Incus container per user ── Hermes ── LLM proxy
                                                       (balebot.main:proxy_app_factory,
                                                        bound to the sandbox bridge only)
```

## A normal message
1. Bale posts the update to `/bale/webhook/{secret}`; the handler returns at once and processes it in the background.
2. `process_update` turns it into `ChatService.handle_message(...)`. It never raises, so one bad update cannot stop the bot.
3. The user row is loaded, banned users are refused, and the live settings are read (admin panel values win over `.env`).
4. If the text is a key/model command, or the user is in the middle of adding a key, `KeyService` answers.
5. Otherwise the route is chosen: the user's own key, or the free base model.
   The free route takes one unit from today's quota first and gives it back if the model call fails.
6. The two new messages are saved (kept 6 months), and the reply is sent.

**No database session is held across a network call.** Each step opens a short session.

## Adding a personal key
`/addkey` ─ site? ─ (known provider **or** the AI reads the public docs and returns a JSON connection config) ─
bot shows the API host, user confirms ─ user sends the key ─ the message is deleted ─ the key is checked against the
provider's model list ─ stored encrypted ─ models appear in `/models`.
The AI step happens **before** the key exists and only ever outputs data (host, paths, auth style), never code.

## The sandbox and the LLM proxy
- `/agent` charges the quota (own-key runs are free), creates or starts the user's container, pushes the task as a file,
  and runs `hermes chat --oneshot` as the unprivileged `agent` user.
- The container has no provider keys. It gets a **short-lived, call-capped token** and calls the **LLM proxy**
  (`sandbox/proxy.py`). The proxy checks the token, forces the user's selected model, adds the real key, and calls the provider.
- Result text and up to 5 small output files are sent back; idle sandboxes are stopped and old ones destroyed (`sandbox-reap`).

## Data (tables in `models.py`)
`users` · `app_settings` (admin-panel values) · `usage_daily` · `messages` · `admin_users` ·
`user_keys` (only ciphertext) · `user_models` · `pending_actions` (add-key conversation, never holds a key) · `sandboxes`.

## Design decisions worth knowing
| Decision | Why |
|---|---|
| Python/FastAPI | Hermes and the first bridge were Python; the code is reusable |
| Webhook in production, polling in development | no public URL needed to develop |
| One container per user | strongest isolation Incus gives; costs disk (see capacity in [operations](operations.md)) |
| LLM proxy instead of keys in the sandbox | a compromised sandbox can only make a bounded number of calls as that user |
| Pinned-IP outbound HTTP | the host that receives a user's key was chosen by a user or by an AI, so it is treated as untrusted |
| `create_all` for tables | fine for development; **production needs Alembic migrations** (not done yet) |
