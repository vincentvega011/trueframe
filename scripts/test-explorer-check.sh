#!/usr/bin/env bash
# Proves scripts/check-explorer-bundle.sh catches what it claims to: plants each kind of secret into a copy of the built
# explorer and requires the check to fail, and requires the untouched copy to pass.   scripts/test-explorer-check.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SRC="$ROOT/app/dist-explorer"
[ -d "$SRC" ] || { echo "build first: scripts/build_explorer.sh" >&2; exit 1; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
CHECK="$ROOT/scripts/check-explorer-bundle.sh"
fails=0
fresh() { rm -rf "$WORK/d"; cp -R "$SRC" "$WORK/d"; }
expect_fail() {  # expect_fail <name> <text to plant>
  fresh; printf '%s\n' "$2" >> "$WORK/d/index.html"
  if "$CHECK" "$WORK/d" >/dev/null 2>&1; then echo "NOT CAUGHT: $1"; fails=$((fails+1)); else echo "caught:     $1"; fi
}
fresh; "$CHECK" "$WORK/d" >/dev/null 2>&1 && echo "clean copy passes" || { echo "clean copy FAILS"; fails=$((fails+1)); }

SECRET58="$(python3 -c "import random,string;r=random.Random(7);a='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';print(''.join(r.choice(a) for _ in range(88)))")"
ARRAY64="[$(python3 -c "print(','.join(str((i*37)%256) for i in range(64)))")]"
expect_fail "dev wallet route"            'fetch("/__dev/requester-key")'
expect_fail "dev wallet label"            'name: "Dev wallet"'
expect_fail "keys/ file path"             'keys/admin.json'
expect_fail "keypair file name"           'proof_of_render-keypair.json'
expect_fail "keypair array (64 numbers)"  "$ARRAY64"
expect_fail "base58 secret key"           "const k = \"$SECRET58\";"
expect_fail "private key header"          '-----BEGIN PRIVATE KEY-----'
expect_fail "provider URL with api-key"   'https://devnet.example.org/?api-key=0123456789abcdef'
expect_fail "helius URL"                  'https://devnet.helius-rpc.com/?x=1'
expect_fail "alchemy URL"                 'https://solana-devnet.g.alchemy.com/v2/abc'
expect_fail "local path"                  '/Users/someone/project/keys'
expect_fail "user name"                   'built by yajan'
# the literal RPC URL, as it would arrive from the shell or app/.env.local
fresh; echo 'https://my-private-rpc.example.net/abc123secret' >> "$WORK/d/index.html"
if DEVNET_RPC_URL='https://my-private-rpc.example.net/abc123secret' "$CHECK" "$WORK/d" >/dev/null 2>&1; then echo "NOT CAUGHT: DEVNET_RPC_URL value"; fails=$((fails+1)); else echo "caught:     DEVNET_RPC_URL value"; fi
fresh; echo 'https://my-private-rpc.example.net/xyz789' >> "$WORK/d/index.html"
if VITE_RPC_URL='https://my-private-rpc.example.net/xyz789' "$CHECK" "$WORK/d" >/dev/null 2>&1; then echo "NOT CAUGHT: VITE_RPC_URL value"; fails=$((fails+1)); else echo "caught:     VITE_RPC_URL value"; fi
# missing pieces
fresh; rm "$WORK/d/frames/3.json"; "$CHECK" "$WORK/d" >/dev/null 2>&1 && { echo "NOT CAUGHT: a missing frame"; fails=$((fails+1)); } || echo "caught:     a missing frame"
fresh; rm "$WORK/d/reports/"*/snapshot.json; "$CHECK" "$WORK/d" >/dev/null 2>&1 && { echo "NOT CAUGHT: missing snapshots"; fails=$((fails+1)); } || echo "caught:     missing snapshots"
# a real transaction signature must NOT trip the base58 rule
fresh; sig="$(python3 -c "import json;print(json.load(open('$SRC/reports/EDN2pWCD2DJ8Yo9YxP5P2SrasK11oF7UxtbhF9tc3C4y/snapshot.json'))['txs'][0]['signature'])")"
printf '"%s"\n' "$sig" >> "$WORK/d/index.html"; "$CHECK" "$WORK/d" >/dev/null 2>&1 && echo "a bundled transaction signature is allowed" || { echo "FALSE POSITIVE on a bundled signature"; fails=$((fails+1)); }
echo; [ "$fails" = 0 ] && echo "all planted-secret tests passed" || echo "$fails test(s) failed"
exit "$fails"
