#!/usr/bin/env bash
# One-time Garage setup: give the single node its storage, import the app's key, create the bucket.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash on Windows would rewrite /garage into a Windows path
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
g() { docker compose exec -T garage /garage "$@"; }
node=$(g node id -q | cut -d@ -f1)
g layout assign -z local -c 2G "$node" || true
g layout apply --version 1 || true
g key import --yes -n bikerental "$S3_ACCESS_KEY" "$S3_SECRET_KEY" || true
g bucket create bikerental || true
g bucket allow --read --write --owner bikerental --key bikerental
echo "Garage ready: bucket bikerental"
