#!/usr/bin/env bash
# Proves the sandbox boundary from the inside. Run as root on the host AFTER the setup scripts and
# with the LLM proxy running. It creates a throwaway sandbox, probes it as the agent user, deletes it.
# Anything printed as FAIL means: do NOT enable sandboxes for users.
#
#   PROBE_BLOCKED="10.179.96.162:22 192.168.10.196:22"  extra host:port pairs that must be unreachable
set -uo pipefail
exec </dev/null  # never let a wrapper's leftover stdin (e.g. a sudo password) reach incus

PROJECT=${PROJECT:-balebot}
IMAGE=${IMAGE:-agent-base}
PROBE=sbx-probe
GATEWAY=${GATEWAY:-10.77.0.1}
PROXY_PORT=${PROXY_PORT:-8001}
PROBE_BLOCKED=${PROBE_BLOCKED:-"10.179.96.162:22 192.168.10.196:22"}
fails=0

cleanup() { incus delete "$PROBE" --project "$PROJECT" --force >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
incus copy "$IMAGE" "$PROBE" --project "$PROJECT" -c limits.memory=256MiB -c limits.cpu=1 || exit 1
incus start "$PROBE" --project "$PROJECT" || exit 1
for _ in $(seq 1 30); do incus exec "$PROBE" --project "$PROJECT" -- ip -4 route show default 2>/dev/null | grep -q default && break; sleep 1; done

agent() { incus exec "$PROBE" --project "$PROJECT" --user 1000 --group 1000 --cwd /home/agent -- "$@"; }
tcp() { agent timeout 4 bash -c 'exec 3<>/dev/tcp/$0/$1' "$1" "$2" 2>/dev/null; }
expect() { # expect <pass|fail> <description> <command...>
  want=$1; desc=$2; shift 2
  if "$@"; then got=pass; else got=fail; fi
  if [ "$got" = "$want" ]; then echo "PASS  $desc"; else echo "FAIL  $desc (expected $want)"; fails=$((fails + 1)); fi
}

expect pass "runs as uid 1000, not root"        test "$(agent id -u)" = 1000
expect fail "cannot read host secrets path"      agent test -e /root/.hermes/.env
expect fail "template holds no .env"             agent test -e /home/agent/.hermes/.env
expect fail "cannot mount filesystems"           agent mount -t tmpfs none /mnt
expect fail "cannot write /proc/sys"             agent sh -c 'echo 1 > /proc/sys/kernel/panic'
expect pass "reaches the LLM proxy port"         tcp "$GATEWAY" "$PROXY_PORT"
expect fail "host ssh (22) is blocked"           tcp "$GATEWAY" 22
expect fail "host port 8000 (bot app) is blocked" tcp "$GATEWAY" 8000
expect fail "host incus API (8443) is blocked"   tcp "$GATEWAY" 8443
expect pass "public internet works (1.1.1.1:443)" tcp 1.1.1.1 443
for hp in $PROBE_BLOCKED; do
  expect fail "blocked target ${hp}" tcp "${hp%%:*}" "${hp##*:}"
done
expect fail "metadata address blocked"          tcp 169.254.169.254 80

echo
if [ "$fails" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$fails CHECK(S) FAILED"; exit 1; fi
