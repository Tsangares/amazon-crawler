#!/usr/bin/env bash
# Deploy script for applesauce-crawlers.
#
# Run on the target host, in the deploy dir (/opt/applesauce-crawlers/).
# Pulls latest from git, refreshes the venv if requirements changed,
# and bounces the systemd unit.
#
# First-time setup (run by hand):
#     git clone https://github.com/Tsangares/applesauce-crawlers /opt/applesauce-crawlers
#     cd /opt/applesauce-crawlers
#     python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#     cp applesauce-crawlers.service /etc/systemd/system/
#     systemctl daemon-reload && systemctl enable --now applesauce-crawlers
#
# After that, ship updates with `./deploy.sh` (or `ssh root@host /opt/applesauce-crawlers/deploy.sh`).

set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/applesauce-crawlers}"
SERVICE="${SERVICE:-applesauce-crawlers}"

cd "$DEPLOY_DIR"

# Snapshot requirements before pull so we know whether to reinstall.
REQS_HASH_BEFORE="$(sha256sum requirements.txt 2>/dev/null | awk '{print $1}' || echo none)"

git pull --ff-only

REQS_HASH_AFTER="$(sha256sum requirements.txt | awk '{print $1}')"

if [ "$REQS_HASH_BEFORE" != "$REQS_HASH_AFTER" ]; then
    echo "requirements.txt changed — refreshing venv"
    .venv/bin/pip install -r requirements.txt
fi

# Quick syntax check before bouncing the live service.
.venv/bin/python -c "import main" >/dev/null

systemctl restart "$SERVICE"
sleep 2
systemctl is-active "$SERVICE" && echo "deploy OK"
