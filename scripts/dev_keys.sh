#!/usr/bin/env bash
# Creates the dev keypairs in keys/ (gitignored) if missing. Safe to re-run; never overwrites.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
command -v solana-keygen >/dev/null || { echo "solana-keygen not found (see docs/ENV.md)" >&2; exit 1; }

mkdir -p "$ROOT/keys"
chmod 700 "$ROOT/keys"
for name in admin requester node verifier mock_usdc_mint; do
  file="$ROOT/keys/$name.json"
  if [ -f "$file" ]; then
    state="exists "
  else
    solana-keygen new --no-bip39-passphrase --silent --outfile "$file"
    chmod 600 "$file"
    state="created"
  fi
  printf '%s  %-14s %s\n' "$state" "$name" "$(solana-keygen pubkey "$file")"
done
