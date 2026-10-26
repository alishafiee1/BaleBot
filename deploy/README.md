# Deploying the agent sandbox (phase 4)

Nothing here has been run on any server yet. The scripts were only syntax-checked. Run them in
order on the Incus host, read each one first, and stop at the first FAIL.

## What the design guarantees (and what proves it)
| Risk | Control | Proof |
|---|---|---|
| Agent runs as root next to bot secrets | one unprivileged container per user, agent user uid 1000, nothing of the bot mounted | `verify-isolation.sh`, `tests/test_sandbox.py` |
| Agent steals provider keys | sandbox holds none; it gets a short-lived, call-capped token for the LLM proxy | `test_agent_never_gets_provider_keys`, `test_proxy_*` |
| Agent attacks the host / LAN / other users | restricted Incus project + egress ACL + port isolation | `verify-isolation.sh` (must be run) |
| Agent fills the disk or RAM | per-sandbox disk/CPU/memory/process limits; project-wide caps; refuses to run without a disk quota | `test_disk_quota_failure_never_runs_unlimited` |
| Bot compromise = host compromise | bot talks to Incus with a restricted TLS cert on 127.0.0.1 (not the root-equivalent socket) | `incus-host-setup.sh` final checks |
| Agent changes bot code | code is outside every sandbox; services run with `ProtectSystem=strict` | systemd units |

## Order
1. `sudo bash incus-host-setup.sh`  (storage pool, bridge, ACL, restricted project, bot certificate)
2. `sudo bash agent-base-setup.sh`  (template container with Hermes; runs the official installer inside it)
3. Install the bot to `/opt/balebot`, create user `balebot`, `/var/lib/balebot`, `/etc/balebot/env`
   (mode 0640 root:balebot) with at least: `BALEBOT_SANDBOX_ENABLED=true`, `BALEBOT_PROXY_SECRET=<32+ random chars>`,
   `BALEBOT_PROXY_URL=http://10.77.0.1:8001/v1`, plus the existing token / vault / base-model settings.
4. Copy `systemd/*` to `/etc/systemd/system/`, then `systemctl enable --now balebot balebot-proxy balebot-reap.timer`.
5. `sudo bash verify-isolation.sh`  -> must end with `ALL CHECKS PASSED`.
6. Only then tell users about `/agent`.

## Known unknowns (decide after the first real run)
- **Approvals:** Hermes may ask for approval before risky commands. In `--oneshot` mode nobody can answer.
  Whether it denies or hangs was not verified. The run is bounded by `--run-budget` and `timeout`, so a
  hang costs one timeout. If it is a problem, consider adding `--yolo` in `SandboxManager._run`; inside the
  sandbox the container, not the prompt, is the boundary.
- **Toolsets:** `BALEBOT_AGENT_TOOLSETS` is empty (Hermes defaults). Narrow it once you have seen the names.
- **ACL rule order:** the ACL was written for Incus' "drop, then reject, then allow" evaluation. `verify-isolation.sh`
  is the check; do not skip it.

## Roll back
```sh
systemctl disable --now balebot-proxy balebot-reap.timer
incus list --project balebot -f csv -c n | xargs -r -n1 incus delete --project balebot --force
incus project delete balebot
incus network acl delete sbx-egress
incus network delete sbxbr0
incus storage delete sandbox
incus config trust remove "$(incus config trust list -f csv | grep balebot | cut -d, -f4)"
# optional: incus config unset core.https_address
```
