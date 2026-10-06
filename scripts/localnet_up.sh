#!/usr/bin/env bash
# Starts a fresh local validator and prepares everything the chain tests and demo need:
#   1. dev keys exist           2. solana-test-validator starts with the program loaded as an
#   upgradeable program (authority = admin; equivalent to `solana program deploy`)
#   3. dev keys funded          4. mock USDC mint created (fixed address, keys/mock_usdc_mint.json)
#   5. requester + node get token accounts and mock USDC     6. init_config
# Writes .localnet/env.json (read by py/tests/test_chain.py). Stop with scripts/localnet_down.sh.
# Env: RPC_PORT (8899), MINT_AMOUNT in base units (1000000000 = 1000 USDC), STAKE_BPS (2000), SOL_AIRDROP (100)
set -euo pipefail
trap 'echo "setup failed; the validator may still be running (scripts/localnet_down.sh stops it)" >&2' ERR
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use "$(cat .node-version)" >/dev/null 2>&1 || true; fi

RPC_PORT="${RPC_PORT:-8899}"
RPC_URL="http://127.0.0.1:${RPC_PORT}"
MINT_AMOUNT="${MINT_AMOUNT:-1000000000}"
STAKE_BPS="${STAKE_BPS:-2000}"
SOL_AIRDROP="${SOL_AIRDROP:-100}"
STATE="$ROOT/.localnet"
CLI="$ROOT/py/porf/chain/cli"
SO="$ROOT/target/deploy/proof_of_render.so"
PY="$ROOT/py/.venv/bin/python"
export PORF_RPC_URL="$RPC_URL"

# the solana CLI wants a default signer even for read-only commands
sol() { solana --keypair "$ROOT/keys/admin.json" --url "$RPC_URL" "$@"; }
chain() { node "$CLI/node_modules/tsx/dist/cli.mjs" "$CLI/src/cli.ts" "$@"; }
# call <command> <json>: runs a chain command, aborts with the error JSON if it fails, prints the output
call() { local out; out="$(chain "$@")" || { echo "chain $1 failed: $out" >&2; exit 1; }; echo "$out"; }
jget() { "$PY" -c "import sys,json; print(json.load(sys.stdin)$1)"; }
healthy() { curl -s -m 2 -X POST -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}' "$RPC_URL" 2>/dev/null | grep -q '"ok"'; }

[ -x "$PY" ] || { echo "python venv missing: $PY (see docs/ENV.md)" >&2; exit 1; }
command -v solana-test-validator >/dev/null || { echo "solana-test-validator not found (see docs/ENV.md)" >&2; exit 1; }
[ -d "$CLI/node_modules" ] || (cd "$CLI" && npm ci --no-audit --no-fund)
[ -f "$SO" ] || { echo "building program..."; anchor build; }

echo "== keys"; "$ROOT/scripts/dev_keys.sh"
keypub() { solana-keygen pubkey "$ROOT/keys/$1.json"; }

echo "== validator"
if [ -f "$STATE/validator.pid" ] && kill -0 "$(cat "$STATE/validator.pid")" 2>/dev/null; then
  "$ROOT/scripts/localnet_down.sh"
elif healthy; then
  echo "something else is already serving RPC on port $RPC_PORT; stop it first (not started by localnet_up.sh)" >&2
  exit 1
fi
mkdir -p "$STATE"
PROGRAM_ID="$("$PY" -c "import json;print(json.load(open('$ROOT/target/idl/proof_of_render.json'))['address'])")"
nohup solana-test-validator --reset --quiet --ledger "$STATE/ledger" --rpc-port "$RPC_PORT" \
  --upgradeable-program "$PROGRAM_ID" "$SO" "$(keypub admin)" > "$STATE/validator.log" 2>&1 &
echo $! > "$STATE/validator.pid"
for i in $(seq 1 120); do healthy && break; sleep 1; [ "$i" = 120 ] && { echo "validator did not become healthy; see $STATE/validator.log" >&2; exit 1; }; done
echo "validator up (pid $(cat "$STATE/validator.pid"), $RPC_URL)"
sol program show "$PROGRAM_ID" | sed 's/^/  /'

echo "== fund dev keys (${SOL_AIRDROP} SOL each)"
for k in admin requester node verifier; do
  sol airdrop "$SOL_AIRDROP" "$(keypub $k)" >/dev/null
  echo "  $k $(sol balance "$(keypub $k)")"
done

echo "== mock USDC mint"
MINT="$(call create-mint '{"signer":"keys/admin.json","mint_keypair":"keys/mock_usdc_mint.json","decimals":6}' | jget "['mint']")"
echo "  mint $MINT"
REQ_TOKEN="$(call create-token-account "{\"signer\":\"keys/requester.json\",\"mint\":\"$MINT\"}" | jget "['token_account']")"
NODE_TOKEN="$(call create-token-account "{\"signer\":\"keys/node.json\",\"mint\":\"$MINT\"}" | jget "['token_account']")"
call mint-to "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"token_account\":\"$REQ_TOKEN\",\"amount\":\"$MINT_AMOUNT\"}" >/dev/null
call mint-to "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"token_account\":\"$NODE_TOKEN\",\"amount\":\"$MINT_AMOUNT\"}" >/dev/null
echo "  requester $REQ_TOKEN"; echo "  node      $NODE_TOKEN   (each holds $MINT_AMOUNT base units)"

echo "== init_config"
call init-config "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"verifier\":\"$(keypub verifier)\",\"stake_bps\":$STAKE_BPS}" >/dev/null
call get-config '{}' | "$PY" -c "import sys,json; a=json.load(sys.stdin)['account']; print('  admin',a['admin'],'\n  verifier',a['verifier'],'\n  mint',a['mint'],'\n  stake_bps',a['stake_bps'])"

"$PY" - <<PYEOF
import json
json.dump({
  "rpc_url": "$RPC_URL", "program_id": "$PROGRAM_ID", "mint": "$MINT", "decimals": 6, "stake_bps": $STAKE_BPS,
  "mint_amount": "$MINT_AMOUNT",
  "keys": {k: "keys/%s.json" % k for k in ("admin", "requester", "node", "verifier")},
  "pubkeys": {k: v for k, v in (("admin", "$(keypub admin)"), ("requester", "$(keypub requester)"), ("node", "$(keypub node)"), ("verifier", "$(keypub verifier)"))},
  "token_accounts": {"requester": "$REQ_TOKEN", "node": "$NODE_TOKEN"},
}, open("$STATE/env.json", "w"), indent=2)
PYEOF
echo "== ready: $STATE/env.json   (stop with scripts/localnet_down.sh)"
