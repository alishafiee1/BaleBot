#!/usr/bin/env bash
# Builds the template container "agent-base" (Debian + Hermes as the unprivileged user "agent").
# Every user sandbox is a clone of it. Run on the Incus host after incus-host-setup.sh, as any user who can administer Incus.
#
# The template must hold NO secrets: no .env, no auth files, no API keys. Hermes inside a sandbox
# gets its model access from the LLM proxy at run time (`--provider custom --base-url ... --api-key
# <short-lived token>`), so nothing has to be baked in. The script checks that at the end.
set -euo pipefail
exec </dev/null  # never let a wrapper's leftover stdin (e.g. a sudo password) reach incus

PROJECT=${PROJECT:-balebot}
NAME=${NAME:-agent-base}
IMAGE=${IMAGE:-images:debian/13}
HERMES_COMMIT=${HERMES_COMMIT:-046094df28b712f62a5e3c728a9e59da05dcbf1e}

sbx() { incus exec "$NAME" --project "$PROJECT" -- "$@"; }
# Only `incus` commands run here, so any user in the incus-admin group can run it (root is not needed).
command -v incus >/dev/null 2>&1 || { echo "incus not found" >&2; exit 1; }

if incus info "$NAME" --project "$PROJECT" >/dev/null 2>&1; then
  echo "$NAME already exists; delete it first to rebuild: incus delete $NAME --project $PROJECT --force" >&2
  exit 1
fi

# The template's own root quota only needs to be big enough to BUILD in: Debian + Hermes + its Python
# is already over 3 GiB. Each user sandbox later gets BALEBOT_SANDBOX_DISK = measured template size
# + the free space each user should have (clones can be given a smaller quota than the template).
TEMPLATE_DISK=${TEMPLATE_DISK:-8GiB}
incus launch "$IMAGE" "$NAME" --project "$PROJECT" -c security.privileged=false \
  -c limits.memory=1GiB -c limits.cpu=2 -d root,size="$TEMPLATE_DISK"
for _ in $(seq 1 30); do sbx true 2>/dev/null && break; sleep 1; done

sbx apt-get update
sbx env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl git python3 python3-venv ffmpeg poppler-utils unzip jq
sbx useradd --create-home --uid 1000 --shell /bin/bash agent
sbx install -d -o agent -g agent -m 0700 /home/agent/in /home/agent/outbox

echo "== installing Hermes $HERMES_COMMIT as 'agent' from GitHub"
# The website installer (hermes-agent.nousresearch.com) answers 403 to this server, so we use the
# repository's own installer, pinned to the commit that already runs on the hermes container.
# Fetch only that one commit (depth 1): a full clone of this repository is several hundred MB of history.
sbx su - agent -c "mkdir -p ~/.hermes/hermes-agent && cd ~/.hermes/hermes-agent && git init -q && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch -q --depth 1 origin $HERMES_COMMIT && git checkout -q FETCH_HEAD && bash scripts/install.sh --commit $HERMES_COMMIT --non-interactive --skip-browser --skip-computer-use"
sbx ln -sf /home/agent/.local/bin/hermes /usr/local/bin/hermes
sbx su - agent -c 'hermes --version'

echo "== secrets check (must print nothing)"
sbx sh -c 'ls -A /home/agent/.hermes 2>/dev/null | grep -E "^(\.env|auth\.json)$" || true'
sbx sh -c 'grep -rIl -E "sk-[A-Za-z0-9]{16,}|[0-9]{8,}:[A-Za-z0-9_-]{30,}" /home/agent 2>/dev/null | head || true'

echo "== template size in MiB (set BALEBOT_SANDBOX_DISK = this + the free space you want per user)"
incus exec "$NAME" --project "$PROJECT" -- df -B1M --output=used / | tail -1 || true
incus stop "$NAME" --project "$PROJECT"
echo "template ready: $NAME"
