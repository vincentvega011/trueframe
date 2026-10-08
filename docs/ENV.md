# ENV.md — pinned versions

Do not upgrade mid-project. Verified installed on 2026-10-05 (macOS arm64, Darwin 25.6).

| Component | Pinned version | Where / how |
|---|---|---|
| Rust (host) | **1.95.0** | rustup; `rust-toolchain.toml` |
| Rust MSRV (workspace) | 1.89.0 | `Cargo.toml` `rust-version`, Anchor 1.1.x minimum |
| Solana CLI (Agave) | **3.1.10** | `release.anza.xyz/v3.1.10/install` → `~/.local/share/solana/install/active_release` |
| Anchor CLI / AVM | **1.1.2** | `avm install 1.1.2` |
| `anchor-lang`, `anchor-spl` crates | **=1.1.2** (exact) | `programs/proof-of-render/Cargo.toml` |
| LiteSVM | 0.10.0 | dev-dependency (Anchor template default) |
| `solana-sha256-hasher` | **=3.1.0** (feature `sha2`) | direct dependency of the program: SHA-256 for the sample seed |
| Node | **22.22.3** | `fnm`; `.node-version` |
| Python | **3.11.17** | Homebrew `python@3.11`, venv at `py/.venv` |
| numpy | 1.26.4 | matches Blender's bundled numpy |
| Pillow | 12.3.0 | |
| pytest | 9.1.1 | |
| Blender | **4.5.14 LTS** | `/Applications/Blender.app` (arm64 dmg, sha256 verified) |
| Blender bundled Python | 3.11.15 (numpy 1.26.4) | |

The **verifier also needs Blender** (same version): check 7 re-renders the sampled frames' segmentation itself, and the verifier refuses to start without it. Its output is only bit-identical to the node's if both use the same Blender build (PROTOCOL.md §17.5).

## Why these versions

- **Anchor 1.1.2**: released 2026-06-26, ~3 months old when chosen (1.2.0 was ~1 month old). Its own CI builds against Solana 3.1.10 and Node 22.22.3, so those are used.
- **Rust 1.95.0**: the Anchor CLI's `Cargo.lock` needs rustc ≥ 1.91 (`cargo-platform 0.3.3`); 1.89 failed `cargo install`. 1.99.0 was the newest stable at the time, so 1.95.0 (2026-04-14) was chosen as a few-months-old release. `anchor build` compiles the BPF program with Solana's own bundled toolchain, independent of this.
- **Blender 4.5 LTS, not 5.2 LTS**: 4.5 bundles Python 3.11, matching the venv.
- **Python 3.11, not the system 3.14.5**: `solders` and similar native libraries lag new Python releases.

## Install provenance (read this)

- The Anchor repository has moved. `github.com/solana-foundation/anchor` and `coral-xyz/anchor` both redirect (HTTP "Moved Permanently") to **`github.com/otter-sec/anchor`**, and crates.io lists `anchor-cli` 1.2.0's repository as `otter-sec/anchor`. AVM was installed with `cargo install --git https://github.com/otter-sec/anchor --tag v1.1.2 avm --locked`. Verify this yourself if provenance matters to you.
- The Solana installer appended `export PATH=".../active_release/bin:$PATH"` to `~/.zprofile`. `rustup` added `. "$HOME/.cargo/env"` to `~/.zshenv` and `~/.profile`.
- `fnm` is not hooked into your shell. Use `eval "$(fnm env)"; fnm use 22.22.3` (or add the hook to `.zshrc`).

## Deviations from the original plan

- **Program tests are Rust LiteSVM, not TypeScript** (Anchor 1.1.2's default; `anchor test` runs `cargo test`). Reason: the protocol needs clock warping and SlotHashes injection, which LiteSVM provides.
- **Chain client is a TypeScript CLI on `@anchor-lang/core`, called from Python via subprocess; `anchorpy` is not attempted.** Reason: Anchor 1.x changed the IDL format and tooling, so `anchorpy` compatibility is near-certain to fail and would burn time. The UI uses the same package. `@anchor-lang/core` publishes 1.1.2 on npm; pin that exact version to match the CLI.
- Caret deps resolved `anchor-lang` to 1.2.0 on first build (`"1.1.2"` means `^1.1.2`). Pinned with `=1.1.2` and `cargo update`.

## Smoke tests run

- `solana-test-validator --reset` (3.1.10, in a scratch dir): booted, produced slots, answered RPC `cluster-version` = 3.1.10. Stopped afterwards.
- Template program: `anchor build` produced `target/deploy/proof_of_render.so` and `target/idl/proof_of_render.json`; `cargo test` passed.
- Blender: `Blender -b --version` = 4.5.14 LTS; bundled Python 3.11.15, numpy 1.26.4.

## Commands

```bash
. "$HOME/.cargo/env"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
eval "$(fnm env)"; fnm use 22.22.3
source py/.venv/bin/activate
/Applications/Blender.app/Contents/MacOS/Blender -b -P py/porf/render/blender_job.py -- <args>
```

## Local validator and chain client

**Validator: `solana-test-validator` 3.1.10, not Surfpool.** It is the tool already smoke-tested on this machine (above), needs nothing new installed, supports `--upgradeable-program` (the program is loaded exactly as `solana program deploy` would leave it, with the admin key as upgrade authority), and is a real validator, so the SlotHashes sysvar that `open_challenge` will read on Day 4 exists for real. Surfpool is not installed here and was not evaluated.

`scripts/localnet_up.sh` (about 8 s): creates the dev keys, starts a fresh validator (`.localnet/ledger`, log in `.localnet/validator.log`, RPC `http://127.0.0.1:8899`, override with `RPC_PORT`), airdrops 100 SOL to admin/requester/node/verifier, creates the mock USDC mint at the fixed address in `keys/mock_usdc_mint.json` (6 decimals, admin is mint authority), gives requester and node 1000 mock USDC each (`MINT_AMOUNT`), calls `init_config` (verifier key, `stake_bps` 2000), and writes `.localnet/env.json`. It refuses to start if something it did not start is already serving that port. `scripts/localnet_down.sh` stops it. `scripts/dev_keys.sh` creates `keys/{admin,requester,node,verifier,mock_usdc_mint}.json` if missing and never overwrites. `keys/` and `.localnet/` are gitignored.

**Chain client = TypeScript CLI + thin Python wrapper** (no anchorpy; see "Deviations"). `py/porf/chain/cli/` pins, exactly: `@anchor-lang/core` 1.1.2, `@solana/web3.js` 1.99.0, `@solana/spl-token` 0.4.15, `tsx` 4.23.15, `typescript` 5.9.3, `@types/node` 22.20.5. Setup: `cd py/porf/chain/cli && npm ci` (Node 22.22.3); type-check with `npm run check`. It needs `target/idl/proof_of_render.json`, so run `anchor build` first. Python code imports only `porf.chain.client`; it finds Node via `PORF_NODE`, else the fnm install named in `.node-version`, else `node` on PATH.

CLI contract: `node tsx/dist/cli.mjs src/cli.ts <command> '<json args>'`. One JSON line on stdout. Success `{"ok":true,...}`; failure `{"ok":false,"error":{"name","code"?,"message","logs"?,"kind"}}` with exit 1 (program, RPC, other) or 2 (bad input). Commands: `init-config`, `create-request`, `stake-and-claim`, `submit-commitment`, `open-challenge`, `submit-verdict` (`Pass`/`Fail`), `finalize-payout`, `slash-and-refund`, `get-config`, `get-request`, `get-submission`, `list-requests` (status filtered client-side), dev utilities `create-mint`, `create-token-account`, `mint-to`, `token-balance`, `airdrop`, and `info`. `timeout-refund` is added with its instruction on Day 5. Conventions: relative key paths resolve against the repo root; all 64-bit numbers cross the boundary as decimal strings; output keys are snake_case; `[u8; 32]` fields are hex; enum values are their variant names. Env: `PORF_RPC_URL`, `PORF_IDL`. A call takes about 0.7 s (Node start plus TypeScript transpile).

**Anchor/web3.js mismatch repaired (`src/compat.ts`).** `@anchor-lang/core` 1.1.2 builds `new SendTransactionError(message, logs)` (old positional form) for a transaction that passes the preflight simulation and then fails on chain; `@solana/web3.js` 1.99 turned that into the message "Unknown action 'undefined'" with no logs, losing the program error name (an intermittent failure near deadlines). The CLI patches the constructor to accept both forms; `test_a_transaction_that_fails_on_chain_keeps_its_program_error_name` covers it without a validator.

**Error names are never hardcoded.** A failure is named from Anchor's own log line (`Error Code: X. Error Number: N`), else from the loaded IDL's `errors` table, else from Anchor's built-in table; an unknown code is reported as `UnknownProgramError` with the number. `PORF_IDL` can point at a different IDL, and a test relies on that to prove the lookup happens at runtime.

**Chain tests:** `scripts/localnet_up.sh && py/.venv/bin/pytest -q py/tests/test_chain.py`. Tests marked `needs_validator` skip, with the reason shown, if no validator is reachable or the CLI dependencies are missing; the other chain tests need nothing running.

## Frontend (`app/`)
Exact pins (`app/package.json`, Node 22.22.3): `vite` 7.3.7, `@vitejs/plugin-react` 5.2.0, `react`/`react-dom` 18.3.1, `typescript` 5.9.3, `vitest` 3.2.7, `playwright` 1.55.1, `@anchor-lang/core` **1.1.2** (same as the CLI), `@solana/web3.js` 1.99.0, `@solana/spl-token` 0.4.15, `@solana/wallet-adapter-base` 0.9.28 / `-react` 0.15.40, `buffer` 6.0.3, and the fonts `@fontsource/ibm-plex-sans` / `-mono` 5.3.0 (bundled: nothing loads from the network at runtime). The IDL is copied from `target/idl/proof_of_render.json` by `npm run predev` / `prebuild` (`scripts/sync-idl.mjs`).
Env: `VITE_CLUSTER` (`localnet` default, or `devnet`), `VITE_RPC_URL` (default `http://127.0.0.1:8899`, or the public devnet RPC), `VITE_REPORT_URL` (default `http://127.0.0.1:8788`, the verifier's `--serve`). The request mint is read from the on-chain config.
Run: `cd app && npm ci && npm run dev` (http://127.0.0.1:5173); `npm test` (pure logic), `npm run check` (types), `scripts/check-devnet-bundle.sh`, `npm run shots` (Playwright, needs localnet, `npm run dev`, a verifier with `--serve`; writes `out/ui/shots/`). The app creates requests only; start a node and the verifier in terminals, e.g. `python -m porf.node.client --keypair keys/node.json` and `python -m porf.verifier.client --keypair keys/verifier.json --serve`.
Anchor's `.rpc()` wraps on-chain failures in the `SendTransactionError` form that web3.js 1.99 mishandles (see the compat note above), so the app signs, sends and confirms transactions itself and reads the program error from the transaction logs.

## Devnet
`scripts/setup.sh --cluster devnet` (admin key needs about 4.3 SOL at the peak of the first deploy, about 2.1 SOL of it stays locked as rent): `solana program write-buffer` into a buffer (keypair in `.devnet/`, resumable), then `solana program deploy --buffer ... --program-id <keypair> --max-len 411000` (the CLI default of twice the size would need about 5.7 SOL), then 0.1 SOL each to node, verifier and requester, the mock mint, token accounts with 1000 mock USDC, `init_config`. `--reclaim` closes leftover buffers and returns their SOL. Measured on 2026-10-08: peak need about 3.99 SOL (buffer 1.90, program data 2.09, program account 0.0008, fees 0.002), balance afterwards 2.61 SOL of the 5 SOL (deploy 2.09 plus 0.3 funding), a deploy of about 57 s on the public RPC.
RPC: `DEVNET_RPC_URL` (shell, read by the scripts and Python) and `VITE_RPC_URL` in `app/.env.local` (read by the UI); both fall back to `https://api.devnet.solana.com`. Neither is written to a committed file or to `.devnet/env.json`.
Scripts: `scripts/fund_wallet.sh <address> [--sol 0.2] [--usdc 1000] [--cluster devnet]`, `scripts/demo_honest.sh --cluster devnet`, `scripts/demo_cheat.sh --mode no_occlusion --cluster devnet`, `scripts/devnet_node.sh`, `scripts/devnet_verifier.sh`. UI: `cd app && npm run dev:devnet` (mode `devnet`, `app/.env.devnet`), `npm run build:devnet` (to `dist-devnet`). Poll intervals on devnet: node 8 s, verifier 4 s, console 5 s (`VITE_POLL_MS`), request list 20 s; transactions are fetched once each and cached.
