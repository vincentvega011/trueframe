#!/usr/bin/env bash
# Cheating-node demo, one command. Needs scripts/setup.sh first.
#   scripts/demo_cheat.sh --mode reuse_frames|fake_boxes|wrong_seed|partial|no_occlusion [--fraction 0.8] [--cluster localnet|devnet]
# The demo corrupts 80% of the frames by default so the cheat is effectively always sampled (the protocol default for the node's
# --cheat is 0.5; see docs/PROTOCOL.md section 8.1 for the detection probabilities).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
HAVE_FRACTION=0; for a in "$@"; do [ "$a" = "--fraction" ] && HAVE_FRACTION=1; done
EXTRA=(); [ "$HAVE_FRACTION" = 0 ] && EXTRA=(--fraction 0.8)
exec "$ROOT/py/.venv/bin/python" -m porf.demo cheat "$@" ${EXTRA[@]+"${EXTRA[@]}"}
