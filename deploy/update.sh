#!/usr/bin/env bash
# Pull the latest Finance Free and restart it. Keeps .env and state/.
#   cd /opt/finance-free && bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose -f docker/compose.yaml up -d --build
docker image prune -f >/dev/null
docker compose -f docker/compose.yaml ps
