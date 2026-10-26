#!/usr/bin/env bash
# One-time setup of the sandbox side of BaleBot on the Incus host. Run as root. READ IT FIRST.
#
# What it creates (all new, nothing existing is modified except one server setting, see 5):
#   1. storage pool  "sandbox"    btrfs loop file: gives disk quotas and cheap clones (`dir` has neither)
#   2. network       "sbxbr0"     isolated bridge 10.77.0.1/24 with NAT, no IPv6
#   3. network ACL   "sbx-egress" sandboxes may reach: the LLM proxy port, DNS, the public internet.
#                                 They may NOT reach: the host's other ports, other sandboxes,
#                                 the hermes container, your LAN, Docker, link-local/metadata.
#   4. project       "balebot"    restricted: unprivileged only, no host paths, only sbxbr0, size caps
#   5. a restricted client certificate for the bot, reachable on 127.0.0.1:8443 ONLY.
#      (core.https_address is set to 127.0.0.1:8443 if it is empty; the bot never gets the root-
#      equivalent unix socket, and its certificate can only touch project "balebot".)
#
# Roll back: see deploy/README.md.
set -euo pipefail
# Some incus subcommands (storage create, ...) read stdin as YAML when it is not a terminal. If a
# wrapper such as a sudo helper feeds a password on stdin, it would leak into incus and its error
# messages. Always start from an empty stdin.
exec </dev/null

POOL=${POOL:-sandbox}
POOL_SIZE=${POOL_SIZE:-20GiB}
NET=${NET:-sbxbr0}
GATEWAY=${GATEWAY:-10.77.0.1}
PROJECT=${PROJECT:-balebot}
ACL=${ACL:-sbx-egress}
PROXY_PORT=${PROXY_PORT:-8001}
CLIENT_DIR=${CLIENT_DIR:-/etc/balebot/incus}

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }; }
exists() { "$@" >/dev/null 2>&1; }
need incus
need openssl
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

echo "== 1. storage pool $POOL ($POOL_SIZE, btrfs)"
exists incus storage show "$POOL" || incus storage create "$POOL" btrfs size="$POOL_SIZE"

echo "== 2. network $NET"
exists incus network show "$NET" || incus network create "$NET" \
  ipv4.address="$GATEWAY/24" ipv4.nat=true ipv6.address=none dns.mode=managed

echo "== 3. network ACL $ACL"
exists incus network acl show "$ACL" || incus network acl create "$ACL"
# Incus evaluates drop first, then reject, then allow, whatever the list order. So a drop rule
# must never overlap something that has to stay reachable: the gateway is blocked by port ranges
# that leave out the proxy port and DNS, and 10.0.0.0/8 is blocked as two ranges that skip the gateway.
incus network acl edit "$ACL" <<YAML
description: BaleBot sandbox egress
egress:
  - {action: allow, protocol: tcp, destination: "$GATEWAY/32", destination_port: "$PROXY_PORT", state: enabled, description: "LLM proxy"}
  - {action: allow, protocol: udp, destination: "$GATEWAY/32", destination_port: "53", state: enabled, description: "DNS"}
  - {action: allow, protocol: tcp, destination: "$GATEWAY/32", destination_port: "53", state: enabled, description: "DNS"}
  - {action: drop, protocol: tcp, destination: "$GATEWAY/32", destination_port: "1-52,54-$((PROXY_PORT - 1)),$((PROXY_PORT + 1))-65535", state: enabled, description: "rest of the host"}
  - {action: drop, protocol: udp, destination: "$GATEWAY/32", destination_port: "1-52,54-65535", state: enabled, description: "rest of the host"}
  - {action: drop, destination: "10.0.0.0-10.77.0.0", state: enabled, description: "private 10/8 below the bridge"}
  - {action: drop, destination: "10.77.0.2-10.255.255.255", state: enabled, description: "private 10/8 above the gateway (other sandboxes, hermes)"}
  - {action: drop, destination: "172.16.0.0/12", state: enabled, description: "private + docker"}
  - {action: drop, destination: "192.168.0.0/16", state: enabled, description: "LAN"}
  - {action: drop, destination: "169.254.0.0/16", state: enabled, description: "link-local and cloud metadata"}
  - {action: drop, destination: "100.64.0.0/10", state: enabled, description: "CGNAT"}
  - {action: drop, destination: "127.0.0.0/8", state: enabled, description: "loopback"}
ingress: []
YAML

echo "== 4. project $PROJECT"
exists incus project show "$PROJECT" || incus project create "$PROJECT" \
  -c features.images=false -c features.profiles=true -c features.storage.volumes=true \
  -c restricted=true \
  -c restricted.containers.privilege=unprivileged -c restricted.containers.nesting=block \
  -c restricted.devices.nic=managed -c restricted.devices.disk=managed \
  -c restricted.networks.access="$NET" \
  -c limits.instances=100
# No project-wide limits.memory / limits.cpu on purpose: Incus sums the *configured* limits of every
# instance, stopped ones included, so they would cap how many sandboxes can exist (4 GiB = ~7) without
# protecting the host. Real protection is the per-sandbox limits set at creation and
# BALEBOT_AGENT_MAX_CONCURRENT. No project-wide limits.disk either: it sums each sandbox's QUOTA
# (template size + free space), but sandboxes are copy-on-write clones that share the template, so
# real use is far smaller. The fixed-size pool file is the real cap: monitor it.
incus profile device remove default root --project "$PROJECT" 2>/dev/null || true
incus profile device remove default eth0 --project "$PROJECT" 2>/dev/null || true
incus profile device add default root disk path=/ pool="$POOL" --project "$PROJECT"
# The ACL is attached to the NETWORK, not to the instance NIC. Incus looks a NIC-level ACL up in the
# instance's project, but the bridge and its ACL live in the default project, so a NIC-level ACL
# fails with "Network ACL not found" for instances in project "$PROJECT".
incus network set "$NET" security.acls="$ACL" \
  security.acls.default.egress.action=allow security.acls.default.ingress.action=drop
incus profile device add default eth0 nic network="$NET" name=eth0 \
  security.port_isolation=true --project "$PROJECT"

echo "== 5. restricted client certificate for the bot"
current=$(incus config get core.https_address || true)
if [ -z "$current" ]; then
  incus config set core.https_address 127.0.0.1:8443
elif [ "$current" != "127.0.0.1:8443" ]; then
  echo "core.https_address is already '$current'; leaving it. The bot needs it reachable on 127.0.0.1:8443." >&2
fi
install -d -m 0750 "$CLIENT_DIR" "$CLIENT_DIR/servercerts"
if [ ! -f "$CLIENT_DIR/client.key" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -keyout "$CLIENT_DIR/client.key" -out "$CLIENT_DIR/client.crt" -subj "/CN=balebot"
  chmod 0640 "$CLIENT_DIR/client.key"
  incus config trust add-certificate "$CLIENT_DIR/client.crt" --name balebot --restricted --projects "$PROJECT"
fi
cp /var/lib/incus/server.crt "$CLIENT_DIR/servercerts/restricted.crt"
cat >"$CLIENT_DIR/config.yml" <<'YAML'
default-remote: restricted
remotes:
  restricted:
    addr: https://127.0.0.1:8443
    protocol: incus
    auth_type: tls
    public: false
aliases: {}
YAML
if id balebot >/dev/null 2>&1; then chown -R root:balebot "$CLIENT_DIR"; fi

echo "== 6. host firewall (ufw)"
# ufw drops inbound traffic by default, and it does so in a separate netfilter chain from the rules
# Incus adds for its bridge, so DHCP/DNS from a new bridge are dropped unless ufw allows them too.
# These are deliberately narrow: only what a sandbox needs from the host, not "allow all on $NET".
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow in on "$NET" to any port 67 proto udp comment "balebot sandbox DHCP"
  ufw allow in on "$NET" to "$GATEWAY" port 53 proto udp comment "balebot sandbox DNS"
  ufw allow in on "$NET" to "$GATEWAY" port 53 proto tcp comment "balebot sandbox DNS"
  ufw allow in on "$NET" to "$GATEWAY" port "$PROXY_PORT" proto tcp comment "balebot LLM proxy"
  # Forwarding: ufw's routed default is DROP. Allow sandboxes out through the uplink ONLY (not to
  # docker0, incusbr0/hermes or other bridges). Replies come back through ufw's established/related rule.
  UPLINK=${UPLINK:-$(ip -4 route show default | awk '{print $5; exit}')}
  [ -n "$UPLINK" ] || { echo "cannot detect the uplink interface; set UPLINK=" >&2; exit 1; }
  ufw route allow in on "$NET" out on "$UPLINK" comment "balebot sandbox internet"
else
  echo "ufw not active: nothing to do"
fi

echo "== checks"
INCUS_CONF="$CLIENT_DIR" incus --project "$PROJECT" list >/dev/null && echo "ok: bot cert can use project $PROJECT"
if INCUS_CONF="$CLIENT_DIR" incus --project default list >/dev/null 2>&1; then
  echo "FAIL: bot cert can see the default project (must not)" >&2
  exit 1
fi
echo "ok: bot cert cannot see the default project"
echo "done. Next: deploy/agent-base-setup.sh, then deploy/verify-isolation.sh"
