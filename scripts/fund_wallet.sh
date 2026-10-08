#!/usr/bin/env bash
# Sends a little SOL and mints mock USDC to any wallet address (for a browser wallet such as Phantom), signing with the admin key.
#   scripts/fund_wallet.sh <address> [--sol 0.2] [--usdc 1000] [--cluster devnet|localnet]
# The admin key is read from keys/admin.json on this machine only. devnet RPC: $DEVNET_RPC_URL if set (never printed), else the public endpoint.
# Idempotent for the token account; every run sends the SOL and mints the USDC again.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
ADDRESS=""; SOL=0.2; USDC=1000; CLUSTER=devnet
usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --sol) SOL="${2:-}"; shift 2 ;;
    --usdc) USDC="${2:-}"; shift 2 ;;
    --cluster) CLUSTER="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) [ -z "$ADDRESS" ] && ADDRESS="$1" || { echo "only one address, please" >&2; exit 2; }; shift ;;
  esac
done
fail() { echo "FUND FAILED: $*" >&2; exit 1; }
[ -n "$ADDRESS" ] || { usage >&2; exit 2; }
case "$CLUSTER" in localnet|devnet) ;; *) fail "--cluster must be localnet or devnet" ;; esac
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
PY="$ROOT/py/.venv/bin/python"; CLI="$ROOT/py/porf/chain/cli"
export PYTHONPATH="$ROOT/py"
"$PY" - "$ADDRESS" "$SOL" "$USDC" <<'PYEOF' || exit 2
import re, sys
addr, sol, usdc = sys.argv[1:]
if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr):
    sys.exit(f"FUND FAILED: '{addr}' is not a Solana address (base58, 32 to 44 characters)")
for name, v in (("--sol", sol), ("--usdc", usdc)):
    if not re.fullmatch(r"\d+(\.\d{1,9})?", v):
        sys.exit(f"FUND FAILED: {name} must be a plain number, got '{v}'")
if float(sol) > 5:
    sys.exit("FUND FAILED: --sol above 5 is almost certainly a mistake")
PYEOF
if [ "$CLUSTER" = devnet ]; then ENVF="$ROOT/.devnet/env.json"; RPC_URL="${DEVNET_RPC_URL:-https://api.devnet.solana.com}"; else ENVF="$ROOT/.localnet/env.json"; RPC_URL="http://127.0.0.1:${RPC_PORT:-8899}"; fi
[ -f "$ENVF" ] || fail "no $CLUSTER environment ($ENVF): run scripts/setup.sh --cluster $CLUSTER first"
export PORF_RPC_URL="$RPC_URL"
mask() { "$PY" -c "import sys; from porf.chain.client import redact; sys.stdout.write(redact(sys.stdin.read()))"; }
sol() { solana --keypair "$ROOT/keys/admin.json" --url "$RPC_URL" "$@" 2> >(mask >&2) | mask; return "${PIPESTATUS[0]}"; }
chain() { node "$CLI/node_modules/tsx/dist/cli.mjs" "$CLI/src/cli.ts" "$@" 2> >(mask >&2); }
jget() { "$PY" -c "import sys,json; print(json.load(sys.stdin)$1)"; }
MINT="$("$PY" -c "import json;print(json.load(open('$ENVF'))['mint'])")"
BASE="$("$PY" -c "from decimal import Decimal; print(int(Decimal('$USDC') * 10**6))")"

echo "Funding $ADDRESS on $CLUSTER"
echo "  [1/3] $SOL SOL"
sol transfer "$ADDRESS" "$SOL" --allow-unfunded-recipient >/dev/null || fail "SOL transfer failed (does the admin key have enough SOL? solana balance keys/admin.json)"
echo "  [2/3] token account for mock USDC"
TOK="$(chain create-token-account "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"owner\":\"$ADDRESS\"}" | jget "['token_account']")" || fail "could not create the token account"
echo "  [3/3] $USDC mock USDC"
chain mint-to "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"token_account\":\"$TOK\",\"amount\":\"$BASE\"}" >/dev/null || fail "could not mint"
echo
echo "  SOL         $(sol balance "$ADDRESS")"
BAL="$(chain token-balance "{\"token_account\":\"$TOK\"}" | jget "['amount']")"
echo "  mock USDC   $("$PY" -c "print(f'{int(\"$BAL\")/10**6:,.2f}')")   (mint $MINT)"
