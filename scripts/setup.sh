#!/usr/bin/env bash
# One command to get a chain ready for the demos.
#   scripts/setup.sh [--cluster localnet|devnet]
# localnet: builds the program, starts a fresh validator with it deployed, creates and funds the dev keys, creates the mock USDC
#           mint, gives the requester and the node mock USDC, calls init_config. Writes .localnet/env.json.
# devnet:   same steps against devnet; the admin key must already hold SOL (fund it at https://faucet.solana.com, the address is printed).
#           Writes .devnet/env.json. NOT TESTED in this repository's development (see docs/LIMITATIONS.md).
# Both use the mock USDC mint (keys/mock_usdc_mint.json), never the real USDC.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
CLUSTER=localnet
while [ $# -gt 0 ]; do
  case "$1" in
    --cluster) CLUSTER="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (usage: scripts/setup.sh [--cluster localnet|devnet])" >&2; exit 2 ;;
  esac
done
case "$CLUSTER" in localnet|devnet) ;; *) echo "--cluster must be localnet or devnet" >&2; exit 2 ;; esac

export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi
PY="$ROOT/py/.venv/bin/python"
CLI="$ROOT/py/porf/chain/cli"
fail() { echo "SETUP FAILED: $*" >&2; exit 1; }
[ -x "$PY" ] || fail "python venv missing ($PY); see docs/ENV.md"
command -v anchor >/dev/null || fail "anchor not found; see docs/ENV.md"
[ -d "$CLI/node_modules" ] || (cd "$CLI" && npm ci --no-audit --no-fund >/dev/null)

echo "Proof-of-Render setup ($CLUSTER)"
echo
echo "[1/3] building the program"
anchor build >/dev/null 2>&1 || fail "anchor build failed; run it by hand to see why"
echo "      target/deploy/proof_of_render.so"

if [ "$CLUSTER" = localnet ]; then
  echo
  echo "[2/3] validator, keys, mock USDC, init_config"
  LOG="$(mktemp)"
  if ! "$ROOT/scripts/localnet_up.sh" > "$LOG" 2>&1; then cat "$LOG" >&2; fail "localnet_up.sh failed"; fi
  sed 's/^/      /' "$LOG"
  rm -f "$LOG"
  echo
  echo "[3/3] ready"
  echo "      next: scripts/demo_honest.sh   scripts/demo_cheat.sh --mode reuse_frames   scripts/demo_timeout.sh"
  exit 0
fi

# ------------------------------------------------------------------ devnet
RPC_URL="${DEVNET_RPC_URL:-https://api.devnet.solana.com}"
STATE="$ROOT/.devnet"; mkdir -p "$STATE"
export PORF_RPC_URL="$RPC_URL"
MINT_AMOUNT="${MINT_AMOUNT:-1000000000}"; STAKE_BPS="${STAKE_BPS:-2000}"
chain() { node "$CLI/node_modules/tsx/dist/cli.mjs" "$CLI/src/cli.ts" "$@"; }
call() { local out; out="$(chain "$@")" || fail "chain $1: $out"; echo "$out"; }
jget() { "$PY" -c "import sys,json; print(json.load(sys.stdin)$1)"; }
sol() { solana --keypair "$ROOT/keys/admin.json" --url "$RPC_URL" "$@"; }
keypub() { solana-keygen pubkey "$ROOT/keys/$1.json"; }

echo
echo "[2/3] keys, funding, program deploy"
"$ROOT/scripts/dev_keys.sh" | sed 's/^/      /'
ADMIN="$(keypub admin)"
BAL="$(sol balance "$ADMIN" | awk '{print $1}')" || fail "cannot reach devnet at $RPC_URL"
if "$PY" -c "import sys; sys.exit(0 if float('$BAL') >= 6 else 1)"; then :; else
  echo "      admin $ADMIN has $BAL SOL; it needs about 6 (program deploy plus funding the other keys)." >&2
  echo "      Fund it at https://faucet.solana.com (devnet), then re-run this script." >&2
  exit 1
fi
PROGRAM_ID="$("$PY" -c "import json;print(json.load(open('$ROOT/target/idl/proof_of_render.json'))['address'])")"
[ "$(solana-keygen pubkey "$ROOT/target/deploy/proof_of_render-keypair.json")" = "$PROGRAM_ID" ] || fail "target/deploy/proof_of_render-keypair.json does not match the program id $PROGRAM_ID"
sol program deploy "$ROOT/target/deploy/proof_of_render.so" --program-id "$ROOT/target/deploy/proof_of_render-keypair.json" >/dev/null || fail "program deploy failed"
echo "      program $PROGRAM_ID deployed"
for k in requester node verifier; do
  sol transfer "$(keypub $k)" 1 --allow-unfunded-recipient >/dev/null 2>&1 || true
  echo "      $k $(sol balance "$(keypub $k)")"
done

echo
echo "[3/3] mock USDC mint, token accounts, init_config"
MINT="$(solana-keygen pubkey "$ROOT/keys/mock_usdc_mint.json")"
if sol account "$MINT" >/dev/null 2>&1; then echo "      mint $MINT already exists"; else
  call create-mint '{"signer":"keys/admin.json","mint_keypair":"keys/mock_usdc_mint.json","decimals":6}' >/dev/null; echo "      mint $MINT created"; fi
REQ_TOKEN="$(call create-token-account "{\"signer\":\"keys/requester.json\",\"mint\":\"$MINT\"}" | jget "['token_account']")"
NODE_TOKEN="$(call create-token-account "{\"signer\":\"keys/node.json\",\"mint\":\"$MINT\"}" | jget "['token_account']")"
call mint-to "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"token_account\":\"$REQ_TOKEN\",\"amount\":\"$MINT_AMOUNT\"}" >/dev/null
call mint-to "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"token_account\":\"$NODE_TOKEN\",\"amount\":\"$MINT_AMOUNT\"}" >/dev/null
if chain get-config '{}' | grep -q '"ok":true'; then echo "      config already initialised"; else
  call init-config "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"verifier\":\"$(keypub verifier)\",\"stake_bps\":$STAKE_BPS}" >/dev/null; echo "      init_config done"; fi
"$PY" - <<PYEOF
import json
json.dump({
  "rpc_url": "$RPC_URL", "program_id": "$PROGRAM_ID", "mint": "$MINT", "decimals": 6, "stake_bps": $STAKE_BPS, "mint_amount": "$MINT_AMOUNT",
  "keys": {k: "keys/%s.json" % k for k in ("admin", "requester", "node", "verifier")},
  "pubkeys": {k: v for k, v in (("admin", "$(keypub admin)"), ("requester", "$(keypub requester)"), ("node", "$(keypub node)"), ("verifier", "$(keypub verifier)"))},
  "token_accounts": {"requester": "$REQ_TOKEN", "node": "$NODE_TOKEN"},
}, open("$STATE/env.json", "w"), indent=2)
PYEOF
echo "      wrote $STATE/env.json"
echo; echo "ready. next: scripts/demo_honest.sh --cluster devnet"
