#!/usr/bin/env bash
# A devnet build must not contain the dev wallet or its key route.
set -euo pipefail
cd "$(dirname "$0")/.."
out="$(mktemp -d)"
VITE_CLUSTER=devnet npx vite build --outDir "$out" >/dev/null
if grep -rlE "__dev/requester-key|requester\.json|Dev wallet" "$out"; then echo "FAIL: dev wallet code is in the devnet bundle" >&2; exit 1; fi
echo "ok: the devnet bundle has no dev wallet code"
