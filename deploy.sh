#!/usr/bin/env bash
# Deploy script for amazon-crawler.
#
# Run on the target host, in the deploy dir (/opt/amazon-crawler/).
# Pulls latest from git, refreshes the venv if requirements changed,
# and bounces the systemd unit.
#
# First-time setup (run by hand):
#     git clone https://github.com/Tsangares/amazon-crawler /opt/amazon-crawler
#     cd /opt/amazon-crawler
#     python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#     cp amazon-crawler.service /etc/systemd/system/
#     systemctl daemon-reload && systemctl enable --now amazon-crawler
#
# After that, ship updates with `./deploy.sh` (or `ssh root@host /opt/amazon-crawler/deploy.sh`).

set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/amazon-crawler}"
SERVICE="${SERVICE:-amazon-crawler}"

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
