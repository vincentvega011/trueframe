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
| Node | **22.22.3** | `fnm`; `.node-version` |
| Python | **3.11.17** | Homebrew `python@3.11`, venv at `py/.venv` |
| numpy | 1.26.4 | matches Blender's bundled numpy |
| Pillow | 12.3.0 | |
| pytest | 9.1.1 | |
| Blender | **4.5.14 LTS** | `/Applications/Blender.app` (arm64 dmg, sha256 verified) |
| Blender bundled Python | 3.11.15 (numpy 1.26.4) | |

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
