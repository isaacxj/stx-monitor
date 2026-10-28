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
