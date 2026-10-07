#!/usr/bin/env bash
# timeout demo, one command. Needs scripts/setup.sh first.   scripts/demo_timeout.sh [--cluster localnet|devnet]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
exec "$ROOT/py/.venv/bin/python" -m porf.demo timeout "$@"
