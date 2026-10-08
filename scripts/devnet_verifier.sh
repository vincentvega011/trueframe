#!/usr/bin/env bash
# The verifier against devnet, serving its reports and frame images to the UI on :8788.   scripts/devnet_verifier.sh [other verifier flags]
# Polls every 4 s. RPC: $DEVNET_RPC_URL if set, else the public endpoint. Output in out/devnet/verifier.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"; cd "$ROOT"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
export PORF_RPC_URL="${DEVNET_RPC_URL:-https://api.devnet.solana.com}"
exec "$ROOT/py/.venv/bin/python" -m porf.verifier.client --keypair keys/verifier.json --out out/devnet/verifier --poll 4 --serve "$@"
