#!/usr/bin/env bash
# Pull the latest code, then rebuild and restart stx-monitor.
# Run it on stx-server from anywhere: ~/stx-monitor/scripts/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."
git pull --ff-only
GIT_SHA=$(git rev-parse --short HEAD)
export GIT_SHA
echo "Deploying stx-monitor ${GIT_SHA}"
docker compose up -d --build
docker compose ps

if ! cmp -s host/stx-helper.py /usr/local/lib/stx-monitor/stx-helper.py 2>/dev/null; then
  echo
  echo "stx-helper is missing or out of date. Install it with:"
  echo "  sudo $(pwd)/scripts/install-helper.sh"
fi
