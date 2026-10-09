#!/usr/bin/env bash
# Fails if the explorer output holds anything that must not be published: keys, an RPC provider key, the dev wallet,
# local paths. Also fails if the expected pieces are missing.
#   scripts/check-explorer-bundle.sh [dir]        dir defaults to app/dist-explorer
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DIR="${1:-$ROOT/app/dist-explorer}"
[ -d "$DIR" ] || { echo "FAIL: $DIR does not exist (run scripts/build_explorer.sh)" >&2; exit 1; }
fail=0
bad() { echo "FAIL: $1" >&2; fail=1; }
scan() {  # scan <description> <extended regex>
  local hits; hits="$(grep -rIlE -e "$2" "$DIR" 2>/dev/null | head -3)"
  [ -z "$hits" ] || bad "$1 found in: $(echo "$hits" | sed "s|$DIR/||" | tr '\n' ' ')"
}
# the dev wallet and anything from keys/
scan "dev wallet code"        '__dev/requester-key|Dev wallet|loadBurner'
scan "key file names"         'keys/(admin|node|verifier|requester|mock_usdc_mint)|requester\.json|admin\.json|verifier\.json|node\.json|-keypair\.json'
# RPC provider keys: patterns, plus the literal values this shell or app/.env.local would inject
scan "RPC provider or API key" 'api[-_]?key=|[?&]token=|helius|alchemy|quiknode|quicknode|ankr\.com|getblock|triton|rpcpool'
scan "a non-public RPC URL"   'https://[a-z0-9.-]*(helius|alchemy|quiknode|quicknode|ankr|getblock|rpcpool)[a-z0-9./?=_&-]*'
for v in "${DEVNET_RPC_URL:-}" "${VITE_RPC_URL:-}"; do
  [ -n "$v" ] && grep -rIqF -e "$v" "$DIR" && bad "the configured RPC URL (value hidden) is in the output"
done
if [ -f "$ROOT/app/.env.local" ]; then
  while IFS='=' read -r k v; do
    v="${v%\"}"; v="${v#\"}"
    case "$k" in VITE_*URL) [ -n "$v" ] && grep -rIqF -e "$v" "$DIR" && bad "$k from app/.env.local (value hidden) is in the output" ;; esac
  done < "$ROOT/app/.env.local"
fi
# key material: a 64-number JSON array (a keypair file), a base58 secret key (87-88 chars), a seed phrase header
scan "a keypair-shaped array"  '\[ *([0-9]{1,3} *, *){63}[0-9]{1,3} *\]'
# A base58 secret key and a transaction signature are both 64 bytes (87-88 characters), so the signatures the data files
# legitimately carry (snapshot txs, report transactions) are allowed and any other quoted string of that shape fails.
python3 - "$DIR" <<'PYEOF' || bad "a quoted 87-88 character base58 string that is not a transaction signature of the bundled runs (a secret key?)"
import json, os, re, sys
d = sys.argv[1]
allowed = set()
for dp, _, fs in os.walk(d):
    for f in fs:
        p = os.path.join(dp, f)
        try:
            if f == "snapshot.json":
                allowed |= {t["signature"] for t in json.load(open(p))["txs"]}
            elif f == "report.json":
                allowed |= set(json.load(open(p)).get("transactions", {}).values())
        except Exception:
            pass
pat = re.compile(rb"[\"'`]([1-9A-HJ-NP-Za-km-z]{87,88})[\"'`]")
bad = []
for dp, _, fs in os.walk(d):
    for f in fs:
        p = os.path.join(dp, f)
        try:
            data = open(p, "rb").read()
        except OSError:
            continue
        for m in pat.finditer(data):
            if m.group(1).decode() not in allowed:
                bad.append(os.path.relpath(p, d))
if bad:
    print("  in: " + " ".join(sorted(set(bad))[:3]), file=sys.stderr)
    sys.exit(1)
PYEOF
scan "a private-key header"    'BEGIN [A-Z ]*PRIVATE KEY'
# local paths and the machine's user name
scan "a local path"            '/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+/'
scan "the local user name"     'yajan'
# the pieces that must be there
for a in EDN2pWCD2DJ8Yo9YxP5P2SrasK11oF7UxtbhF9tc3C4y FUbhyAU19SKaFB4tNfKU4URWwksVAL9jJorfeTVo5d2j; do
  [ -f "$DIR/reports/$a/report.json" ] || bad "reports/$a/report.json is missing"
  [ -f "$DIR/reports/$a/snapshot.json" ] || bad "reports/$a/snapshot.json is missing"
done
[ "$(ls "$DIR/frames" 2>/dev/null | grep -c '\.json$')" = 20 ] || bad "frames/ does not hold the 20 honest frames"
[ -f "$DIR/index.html" ] || bad "index.html is missing"
[ "$fail" = 0 ] && echo "ok: the explorer output has no keys, dev wallet, provider URL or local paths ($(find "$DIR" -type f | wc -l | tr -d ' ') files)"
exit "$fail"
