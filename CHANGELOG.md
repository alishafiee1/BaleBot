# Changelog

Format: newest first. Dates are approximate (month) except where noted.

## Unreleased

### Added
- Graphify code search (`.graphifyignore`, [docs/code-search.md](docs/code-search.md)).
- Documentation set: architecture, configuration, bot commands, security, operations, contributing.

### Changed
- Sandbox network ACL is attached to the **network**, not the NIC; project no longer has RAM/CPU totals; sandbox disk is 3 GiB.
- `deploy/agent-base-setup.sh` installs Hermes from GitHub at a pinned commit (the website installer answers 403 here).
- `deploy/incus-host-setup.sh` adds narrow ufw rules (DHCP, DNS, proxy port, one routed rule to the uplink).
- All deploy scripts start with `exec </dev/null` after a sudo password reached `incus` through stdin.
- The original design proposal moved to `docs/history/proposal-fa.md`.

## 2026-10 - Phase 4: agent sandbox (built, tested with fakes)
- `/agent <task>` runs Hermes one-shot in a per-user unprivileged Incus container.
- LLM proxy with short-lived, call-capped tokens; the sandbox holds no keys.
- Sandbox manager (create/limits/run/collect files/reap), `sandbox-reap` command, systemd units.
- Quota can take more than one unit per action (`AGENT_COST`), with refunds.
- **Not yet verified on a real server**: see [docs/security.md](docs/security.md).

## 2026-10 - Phase 3: personal keys
- `/addkey`, `/keys`, `/delkey`, `/addmodel`, `/models`, `/model`, `/cancel`; model menu with buttons.
- AES-256-GCM vault bound to owner and key id; message deletion; AI-built provider configs validated against the named site.
- `SafeHttp`: public https only, DNS pinned, TLS verified against the hostname. Fixed a bug found only on a real host.
- Fix: `utcnow()` is now naive UTC (PostgreSQL/asyncpg rejects aware values for timezone-less columns).

## 2026-10 - Phase 2: admin panel
- `/panel/`: login (forced password change), dashboard, base model settings, users (ban, per-user quota).
- CSRF tokens, login rate limit, `SameSite=Strict` cookies. Fix: an empty CSRF token no longer matches an empty session token.

## 2026-10 - Phases 0-1
- Multi-user bot, 100 free requests a day with refunds, base model and quota editable at runtime, 6-month retention, webhook and polling.
