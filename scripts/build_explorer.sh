#!/usr/bin/env bash
# Builds the read-only explorer for judges into app/dist-explorer. Needs the two devnet runs in out/devnet/ (see docs/ENV.md).
# Nothing is deployed; upload the folder yourself (docs/ENV.md, "Explorer build").
#   scripts/build_explorer.sh
# RPC for the snapshot and the frame check: $DEVNET_RPC_URL if set (never written to the output), else the public endpoint.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
DIST="$ROOT/app/dist-explorer"
echo "[1/4] building the app (mode explorer)"
(cd app && npm run build:explorer >/dev/null 2>&1) || { echo "build failed; run: cd app && npm run build:explorer" >&2; exit 1; }
echo "[2/4] reports, frame images and the honest dataset (verified against devnet)"
PYTHONPATH="$ROOT/py" "$ROOT/py/.venv/bin/python" scripts/explorer_data.py "$DIST"
echo "[3/4] snapshots of the request and submission accounts"
node "$ROOT/py/porf/chain/cli/node_modules/tsx/dist/cli.mjs" app/scripts/snapshot-explorer.ts "$DIST" 2>&1 | sed -E 's#https?://[^ ]+#<url>#g'
echo "[4/4] security check"
"$ROOT/scripts/check-explorer-bundle.sh" "$DIST"
echo
echo "output: $DIST   $(du -sh "$DIST" | cut -f1), $(find "$DIST" -type f | wc -l | tr -d ' ') files"
