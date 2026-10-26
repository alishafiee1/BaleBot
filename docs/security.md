# Security

This page says what is protected, how, and what is **not yet proven**. Read the last section before enabling `/agent`.

## What an attacker might want
| Goal | Who | Main defence |
|---|---|---|
| Read another user's API key | another user, a compromised sandbox, a curious admin | keys are AES-256-GCM encrypted and bound to owner + key id; the model never sees keys; the panel never shows them |
| Steal the free base model's key | a sandbox, a user | the key lives only on the server; sandboxes use short-lived tokens through the LLM proxy |
| Send a user's key to a malicious host | a poisoned docs page, a hostile user | the API host must be the same site the user named, the user confirms it, and the request goes only to public https |
| Reach internal services (SSRF) | any user-chosen URL | public https only, port 443, no redirects, DNS pinned to the checked address |
| Harm the server through `/agent` | a user running tools | one unprivileged container per user, restricted Incus project, resource limits, egress rules |
| Change the bot's code | a sandbox | code and secrets are never mounted into a sandbox; services run with `ProtectSystem=strict` |
| Take over the panel | internet | forced password change, argon2 hashes, CSRF tokens, login rate limit, `SameSite=Strict` + `Secure` cookies |

## Controls in the code (all covered by tests)
- **Vault** (`vault.py`): AES-256-GCM, random nonce, AAD = `userkey:<user>:<key id>`. A copied row never decrypts for another user.
- **Key hygiene**: the message holding a key is deleted first; keys are never logged; errors keep only the exception type;
  the pending-conversation table never stores a key; HTTP client logs are silenced (the Bale token is part of its URLs).
- **Provider configs** (`services/providers.py`): the AI returns data validated by a schema (https, safe paths, known auth styles).
  Its host must share a registrable domain with the site the user gave, and the user must confirm.
- **SafeHttp** (`net/safe_http.py`): scheme/port/credentials checks, every resolved address must be public (IPv4-mapped IPv6 included),
  TCP is pinned to the checked address while TLS still verifies the hostname, redirects off, environment proxies ignored, response size capped.
  A live check against two real hosts passed; unit tests cover the blocklist.
- **LLM proxy** (`sandbox/proxy.py`): HMAC token with expiry and a call cap; only whitelisted OpenAI fields are forwarded;
  the model is always the user's selected one; `max_tokens` is capped; banned users are refused.
- **Sandbox manager** (`sandbox/manager.py`): argument lists only (no shell strings); the task is pushed as a file; the agent runs as uid 1000;
  files named by the sandbox are validated; a sandbox is **not run** if its disk quota cannot be applied; the run token is scrubbed from output.
- **Quota**: atomic `UPDATE ... WHERE count <= limit - cost`; refunds on infrastructure failure.

## Controls on the server (see [operations](operations.md))
Restricted Incus project (unprivileged only, no host paths, only the sandbox bridge) · btrfs pool with disk quotas ·
network ACL on the sandbox bridge · NIC port isolation · narrow ufw rules (DHCP, DNS, proxy port, one routed rule to the uplink) ·
the bot talks to Incus with a **restricted TLS certificate on 127.0.0.1:8443**, not the root-equivalent unix socket.

## Not yet proven / known gaps
1. **Sandbox isolation has not been verified on the real server.** `deploy/verify-isolation.sh` must print `ALL CHECKS PASSED`
   (host ports, LAN, other sandboxes, the hermes container, metadata address blocked; proxy and internet reachable) before `/agent` is opened to users.
   The ufw routed rule lets sandboxes out through the same network card the LAN uses, so **blocking the LAN depends on the Incus ACL** until that script says otherwise.
2. **Hermes approvals in `--oneshot` mode** are untested: a risky command may be denied or may hang until the timeout.
3. The run token is passed as a command-line argument, so the agent can read **its own** token in `ps`. It is run-scoped, expires, and is call-capped.
4. Login rate limiting is in memory (one process, resets on restart).
5. No database migrations yet (`create_all` only).
6. Requests on a user's own key have no per-user rate limit.
7. The `.env` file and the vault key are plain files protected by Unix permissions. Back up the vault key separately; losing it loses all stored keys.
8. Capacity: with a 20 GiB pool and 3 GiB sandboxes only about 5-6 sandboxes can exist at once. Grow the pool before real use.

## Lessons recorded from setup
- **A sudo helper that passes the password on stdin leaked it to `incus`**, which reads stdin as YAML and echoed it in an error. All deploy scripts now start with `exec </dev/null`. Never let a wrapper's stdin reach a child command.
- ufw drops traffic in a separate chain from the Incus rules, so a new bridge needs its own ufw rules.

## Reporting a problem
Open an issue without secrets in it. If a key or token was exposed, rotate it first.
