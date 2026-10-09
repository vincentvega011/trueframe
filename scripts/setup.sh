#!/usr/bin/env bash
# One command to get a chain ready for the demos.
#   scripts/setup.sh [--cluster localnet|devnet]
# localnet: builds the program, starts a fresh validator with it deployed, creates and funds the dev keys, creates the mock USDC
#           mint, gives the requester and the node mock USDC, calls init_config. Writes .localnet/env.json.
# devnet:   same steps against devnet; the admin key must already hold SOL (fund it at https://faucet.solana.com, the address is printed).
#           Needs about 4.3 SOL at the peak of the first deploy (about 2.1 SOL is kept). The program is written to a buffer account first
#           (resumable, buffer keypair in .devnet/) and deployed with --max-len 411000 (the CLI default of 2x the size would need about
#           5.7 SOL). Funds node, verifier and requester with 0.1 SOL each, creates the mock USDC mint, calls init_config.
#           RPC: $DEVNET_RPC_URL if set (a provider URL; never printed or stored), else the public devnet endpoint.
#           --reclaim closes leftover buffer accounts and returns their SOL to the admin key.  Writes .devnet/env.json.
# Both use the mock USDC mint (keys/mock_usdc_mint.json), never the real USDC.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
CLUSTER=localnet
RECLAIM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --cluster) CLUSTER="${2:-}"; shift 2 ;;
    --reclaim) RECLAIM=1; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (usage: scripts/setup.sh [--cluster localnet|devnet] [--reclaim])" >&2; exit 2 ;;
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

echo "TrueFrame setup ($CLUSTER)"
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
export PORF_RPC_URL="$RPC_URL" PYTHONPATH="$ROOT/py"
RPC_HOST="$("$PY" -c "from porf.chain.client import rpc_host; import sys; print(rpc_host(sys.argv[1]))" "$RPC_URL")"
[ -n "${DEVNET_RPC_URL:-}" ] && RPC_NOTE="$RPC_HOST (from DEVNET_RPC_URL)" || RPC_NOTE="$RPC_HOST (public, rate-limited; set DEVNET_RPC_URL to use a provider)"
MINT_AMOUNT="${MINT_AMOUNT:-1000000000}"; STAKE_BPS="${STAKE_BPS:-2000}"
MAX_LEN=411000
# nothing the solana CLI or the chain CLI prints may carry the RPC key: every output goes through this filter
mask() { "$PY" -c "import sys; from porf.chain.client import redact; sys.stdout.write(redact(sys.stdin.read()))"; }
chain() { node "$CLI/node_modules/tsx/dist/cli.mjs" "$CLI/src/cli.ts" "$@" 2> >(mask >&2); }
call() { local out; out="$(chain "$@")" || fail "chain $1: $(printf %s "$out" | mask)"; echo "$out"; }
jget() { "$PY" -c "import sys,json; print(json.load(sys.stdin)$1)"; }
sol() { solana --keypair "$ROOT/keys/admin.json" --url "$RPC_URL" "$@" 2> >(mask >&2) | mask; return "${PIPESTATUS[0]}"; }
keypub() { solana-keygen pubkey "$ROOT/keys/$1.json"; }
lt() { "$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)" "$1" "$2"; }

echo
echo "[2/3] keys, funding, program deploy   (rpc: $RPC_NOTE)"
"$ROOT/scripts/dev_keys.sh" | sed 's/^/      /'
ADMIN="$(keypub admin)"
BAL="$(sol balance "$ADMIN" | awk '{print $1}')" || fail "cannot reach devnet at $RPC_HOST"
PROGRAM_ID="$("$PY" -c "import json;print(json.load(open('$ROOT/target/idl/proof_of_render.json'))['address'])")"
SO="$ROOT/target/deploy/proof_of_render.so"
PROG_KEY="$ROOT/target/deploy/proof_of_render-keypair.json"
BUFKEY="$STATE/buffer-keypair.json"

if [ "$RECLAIM" = 1 ]; then
  echo "      leftover buffers:"; sol program show --buffers | sed 's/^/        /'
  sol program close --buffers --bypass-warning | sed 's/^/        /' || true
  echo "      admin now has $(sol balance "$ADMIN")"; exit 0
fi

[ "$(solana-keygen pubkey "$PROG_KEY")" = "$PROGRAM_ID" ] || fail "target/deploy/proof_of_render-keypair.json does not match the program id $PROGRAM_ID"
SIZE="$(wc -c < "$SO" | tr -d ' ')"
[ "$SIZE" -le "$MAX_LEN" ] || fail "the program is $SIZE bytes, over the $MAX_LEN-byte limit this script deploys with; raise MAX_LEN (and the SOL budget) first"
if sol program show "$PROGRAM_ID" >/dev/null 2>&1; then
  echo "      program $PROGRAM_ID is already deployed"
else
  if lt "$BAL" 4.3; then
    echo "      admin $ADMIN has $BAL SOL; the first deploy peaks at about 4.0 SOL (buffer 1.90 + program data 2.09 + fees)." >&2
    echo "      Fund it at https://faucet.solana.com (devnet), then re-run this script." >&2
    exit 1
  fi
  [ -f "$BUFKEY" ] || solana-keygen new --no-bip39-passphrase --silent --outfile "$BUFKEY" >/dev/null
  echo "      writing the program to a buffer account (resumable: re-running continues where it stopped)"
  sol program write-buffer "$SO" --buffer "$BUFKEY" --max-sign-attempts 30 \
    || fail "write-buffer did not finish. Re-run to resume, or run scripts/setup.sh --cluster devnet --reclaim to get the SOL back"
  echo "      deploying from the buffer (max length $MAX_LEN bytes)"
  sol program deploy --buffer "$BUFKEY" --program-id "$PROG_KEY" --max-len "$MAX_LEN" \
    || fail "deploy failed. The buffer still holds the program: re-run to retry, or --reclaim to close it and recover its SOL"
  rm -f "$BUFKEY"
  echo "      program $PROGRAM_ID deployed ($SIZE bytes, max length $MAX_LEN)"
fi
for k in node verifier requester; do
  B="$(sol balance "$(keypub $k)" | awk '{print $1}')" || B=0
  if lt "$B" 0.05; then sol transfer "$(keypub $k)" 0.1 --allow-unfunded-recipient >/dev/null || fail "could not fund $k"; fi
  echo "      $k $(sol balance "$(keypub $k)")"
done

echo
echo "[3/3] mock USDC mint, token accounts, init_config"
MINT="$(solana-keygen pubkey "$ROOT/keys/mock_usdc_mint.json")"
if sol account "$MINT" >/dev/null 2>&1; then echo "      mint $MINT already exists"; else
  call create-mint '{"signer":"keys/admin.json","mint_keypair":"keys/mock_usdc_mint.json","decimals":6}' >/dev/null; echo "      mint $MINT created"; fi
REQ_TOKEN="$(call create-token-account "{\"signer\":\"keys/requester.json\",\"mint\":\"$MINT\"}" | jget "['token_account']")"
NODE_TOKEN="$(call create-token-account "{\"signer\":\"keys/node.json\",\"mint\":\"$MINT\"}" | jget "['token_account']")"
tokbal() { chain token-balance "{\"token_account\":\"$1\"}" | jget "['amount']" 2>/dev/null || echo 0; }
for pair in "requester:$REQ_TOKEN" "node:$NODE_TOKEN"; do
  who="${pair%%:*}"; tok="${pair#*:}"
  if [ "$(tokbal "$tok")" = 0 ]; then call mint-to "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"token_account\":\"$tok\",\"amount\":\"$MINT_AMOUNT\"}" >/dev/null; echo "      minted mock USDC to $who"; else echo "      $who already holds mock USDC"; fi
done
if chain get-config '{}' | grep -q '"ok":true'; then echo "      config already initialised"; else
  call init-config "{\"signer\":\"keys/admin.json\",\"mint\":\"$MINT\",\"verifier\":\"$(keypub verifier)\",\"stake_bps\":$STAKE_BPS}" >/dev/null; echo "      init_config done"; fi
# env.json never holds the RPC URL: consumers read $DEVNET_RPC_URL (or fall back to the public endpoint) themselves
"$PY" - <<PYEOF
import json
json.dump({
  "program_id": "$PROGRAM_ID", "mint": "$MINT", "decimals": 6, "stake_bps": $STAKE_BPS, "mint_amount": "$MINT_AMOUNT",
  "keys": {k: "keys/%s.json" % k for k in ("admin", "requester", "node", "verifier")},
  "pubkeys": {k: v for k, v in (("admin", "$(keypub admin)"), ("requester", "$(keypub requester)"), ("node", "$(keypub node)"), ("verifier", "$(keypub verifier)"))},
  "token_accounts": {"requester": "$REQ_TOKEN", "node": "$NODE_TOKEN"},
}, open("$STATE/env.json", "w"), indent=2)
PYEOF
echo "      wrote $STATE/env.json"
echo "      admin keeps $(sol balance "$ADMIN")"
echo; echo "ready. next: scripts/demo_honest.sh --cluster devnet"
