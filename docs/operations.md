# Operations

How to run BaleBot, set up the sandbox, back it up, debug it and roll it back.

> **Honest status.** The sandbox pieces below were built and partly run on the real server (`rodi`) while writing this.
> Steps marked ✅ were observed working; steps marked ⏳ were not finished or not yet proven. The scripts in `deploy/`
> have **not** been run end-to-end from a clean machine in their final form: the server was fixed by hand in a few places
> and the scripts were updated to match. Do a clean run and `deploy/verify-isolation.sh` before trusting them.

## 1. Run the bot (no sandbox)
1. Create user `balebot`, directories `/opt/balebot` (code + `.venv`), `/var/lib/balebot` (data) and `/etc/balebot/env` (mode `0640 root:balebot`).
2. Put settings in `/etc/balebot/env` (see [configuration](configuration.md)): at least `BALEBOT_BALE_TOKEN`, `WEBHOOK_SECRET`,
   `SESSION_SECRET`, `BASE_MODEL_API_KEY`, `DATABASE_URL`, and `VAULT_KEY` if you want personal keys.
3. Install the units from `deploy/systemd/` and start `balebot`. nginx terminates HTTPS for `roditeam.ir` and forwards to `127.0.0.1:8000`.
4. `python -m balebot.cli create-admin`, then `python -m balebot.cli set-webhook`.
5. Run `python -m balebot.cli purge` daily (cron or a timer) for the 6-month message retention.

## 2. Sandbox setup on the Incus host
Run as root, read each script first. Do not run them through a helper that feeds a password on stdin (see [security](security.md)).

| Step | Script | Creates | Status on `rodi` |
|---|---|---|---|
| 1 | `deploy/incus-host-setup.sh` | btrfs pool `sandbox` (20 GiB), bridge `sbxbr0` (10.77.0.1/24), ACL `sbx-egress` (on the **network**), restricted project `balebot`, restricted bot certificate on `127.0.0.1:8443`, narrow ufw rules | ✅ done (some parts by hand, script updated to match) |
| 2 | `deploy/agent-base-setup.sh` | template container `agent-base` with Hermes pinned to commit `046094d…` | ⏳ cloning/installing Hermes when this was written |
| 3 | install the bot, set `SANDBOX_ENABLED`, `PROXY_SECRET`, start `balebot-proxy` | proxy bound to `10.77.0.1:8001` | ⏳ |
| 4 | `deploy/verify-isolation.sh` | throwaway sandbox, probes from inside | ⏳ **required before users get `/agent`** |

### What step 1 changes on the host
- one setting: `core.https_address` = `127.0.0.1:8443` (only if it was empty; it was)
- ufw (active on this server): **4 input rules** on `sbxbr0` (DHCP 67/udp; DNS 53 udp+tcp and proxy 8001/tcp to `10.77.0.1`) and
  **1 routed rule** `sbxbr0 → enp63s0` (the uplink; auto-detected). Each carries the comment `balebot ...`.
- it does **not** touch `incusbr0`, the `default` project or the `hermes` container.

### Capacity
Pool 20 GiB, project disk cap 19 GiB, each sandbox 3 GiB → **about 5-6 sandboxes** at once. Raise the pool
(`incus storage set sandbox size=...`, and the project's `limits.disk`) before real use. There is no project-wide RAM/CPU cap on purpose
(Incus sums configured limits of stopped instances too); `AGENT_MAX_CONCURRENT` limits real load.

## 3. Back up
| What | How | Why it matters |
|---|---|---|
| `BALEBOT_VAULT_KEY` | copy to a password manager, **not** only the server | without it every stored user key is lost |
| database | `pg_dump` | users, quotas, encrypted keys, messages |
| `/etc/balebot/env`, `/etc/balebot/incus` | encrypted copy | settings and the bot's Incus certificate |
Sandboxes and the template are rebuildable; they are not backed up.

## 4. Troubleshooting (things that actually happened)
| Symptom | Cause | Fix |
|---|---|---|
| `Failed loading ACL ... Network ACL not found` when starting a sandbox | ACL attached to the NIC; Incus looks it up in the instance's project | attach the ACL to the **network** (`incus network set sbxbr0 security.acls=...`) |
| container has no IPv4, `networkd` stuck "configuring", `tcpdump` shows DISCOVER but no reply | ufw drops DHCP in a different chain than the Incus rules | add the narrow ufw rules from step 1 |
| container has an address and DNS but no internet | ufw routed default is DROP | the one `ufw route allow in on sbxbr0 out on <uplink>` rule |
| `Instance ... has no "limits.memory" config` | project had a project-wide `limits.memory` | remove it (`incus project unset balebot limits.memory`) or set per-instance limits |
| `curl ... returned error: 403` from `hermes-agent.nousresearch.com` | the website blocks this server (`x-vercel-mitigated: deny`), same from every container | install from GitHub at a pinned commit (the template script does) |
| `incus storage create` fails with `cannot unmarshal !!str ...` | stdin is read as YAML and contained something else | start scripts with `exec </dev/null` (they do) |
| `tcpdump -w file` looks empty | output is buffered until exit | use `-l -U` or read after tcpdump ends |
| `in() { ... }` fails in bash | `in` is a reserved word | pick another function name |

## 5. Roll back the sandbox
```sh
systemctl disable --now balebot-proxy balebot-reap.timer 2>/dev/null
incus list --project balebot -f csv -c n | xargs -r -n1 incus delete --project balebot --force
incus project delete balebot
incus network unset sbxbr0 security.acls            # then:
incus network acl delete sbx-egress
incus network delete sbxbr0
incus storage delete sandbox
incus config trust list                              # remove the 'balebot' certificate:
incus config trust remove <fingerprint>
ufw status numbered                                  # delete the rules whose comment starts with "balebot", highest number first
# optional: incus config unset core.https_address
```
