#!/usr/bin/env bash
# The node against devnet, for a request created from the browser.   scripts/devnet_node.sh [--only-requester ADDR] [--cheat MODE --fraction F] [other node flags]
# Polls every 8 s (the public devnet RPC is rate-limited). RPC: $DEVNET_RPC_URL if set, else the public endpoint. Output in out/devnet/node.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"; cd "$ROOT"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
export PORF_RPC_URL="${DEVNET_RPC_URL:-https://api.devnet.solana.com}"
exec "$ROOT/py/.venv/bin/python" -m porf.node.client --keypair keys/node.json --out out/devnet/node --poll 8 "$@"
