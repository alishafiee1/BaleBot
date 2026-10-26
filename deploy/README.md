# deploy/

Server-side files for the agent sandbox. The explanation, order, status, troubleshooting and rollback live in
[docs/operations.md](../docs/operations.md) — read that first.

| File | Run as | Purpose |
|---|---|---|
| `incus-host-setup.sh` | root | storage pool, sandbox bridge, ACL, restricted project, bot certificate, narrow ufw rules |
| `agent-base-setup.sh` | root | build the `agent-base` template with Hermes (pinned commit) |
| `verify-isolation.sh` | root | prove the boundary from inside a throwaway sandbox; must print `ALL CHECKS PASSED` |
| `systemd/balebot.service` | | web app + panel (127.0.0.1:8000) |
| `systemd/balebot-proxy.service` | | LLM proxy, bound to the sandbox bridge only (10.77.0.1:8001) |
| `systemd/balebot-reap.{service,timer}` | | stop idle / destroy old sandboxes every 10 minutes |

All scripts start with `exec </dev/null` on purpose. Never run them through a helper that passes a password on stdin.
