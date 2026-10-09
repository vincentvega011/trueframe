# TrueFrame

Verifiable synthetic training data on Solana.

## What it is

A requester escrows (test) USDC for a seeded 3D dataset. A node stakes, renders the dataset with headless Blender and commits a Merkle root of the results. A verifier samples frames using an on-chain slot hash, checks them against the seed and re-renders their segmentation, and the program either pays the node or slashes it and refunds the requester. "Proof-of-render" is the name of this mechanism: the node commits to every frame up front, and a random sample decides whether the whole set is paid for.

## How it works

1. The requester creates a request (frame count, occlusion level, price, seed). The escrow is locked on chain.
2. A node stakes a fraction of the escrow and claims the request.
3. The node renders every frame from the seed and commits the Merkle root of all frame records.
4. The verifier opens a challenge. The program derives the sample from a slot hash, so neither side picks the frames. The verifier fetches the sampled frames from the node, runs 7 checks, and posts a verdict.
5. Pass: after the challenge window the node is paid and its stake returned. Fail: the node is slashed and the requester gets the escrow and the stake. A request nobody claims or commits can be refunded after its deadline.

## Status

Hackathon prototype. Not audited, test USDC only, one trusted verifier key.

Built and working today:
- Anchor program with 9 instructions: init_config, create_request, stake_and_claim, submit_commitment, open_challenge, submit_verdict, finalize_payout, slash_and_refund, timeout_refund (Rust LiteSVM tests, including negative cases).
- Python node: polls for requests, claims, renders with Blender, commits, and serves a reveal endpoint (frames plus Merkle proofs).
- Python verifier: samples on chain, runs 7 checks (Merkle proof, scene match, hash consistency, box plausibility, image sanity, segmentation ids, segmentation re-render), posts the verdict, writes a report, and can serve reports and frame images.
- Cheat modes for testing the verifier: reuse_frames, fake_boxes, wrong_seed, partial, no_occlusion.
- React console: create request, status timeline, challenge result, and a refund button for timed-out requests.
- Verified dataset download: the browser fetches every frame, checks it against the Merkle root on chain, and zips the frames that pass.
- Devnet deployment: program `7jhnpAu8H9rtDhEyXRmFCicjGLUepXoafE4SiE7EvDLw`, with a funding script and devnet node and verifier scripts.
- Read-only explorer build: a static site with one honest (paid) and one cheating (slashed) devnet run.

## Quickstart

Prerequisites and pinned versions (Anchor, Solana CLI, Rust, Python, Blender, Node) are in `docs/ENV.md`. Set up the Python environment and the Node version as described there first.

Localnet demo (starts a fresh local validator, deploys, creates the mock USDC mint and keys):

```
scripts/setup.sh
scripts/demo_honest.sh
scripts/demo_cheat.sh --mode no_occlusion
scripts/demo_timeout.sh
```

Console on localnet (three terminals, after `scripts/setup.sh`):

```
cd app && npm ci && npm run dev
py/.venv/bin/python -m porf.verifier.client --keypair keys/verifier.json --serve
py/.venv/bin/python -m porf.node.client --keypair keys/node.json --port 8787
```

Open http://127.0.0.1:5173. On localnet the console signs with a dev wallet (see `docs/LIMITATIONS.md`). Port 8787 is where the console's dataset download looks for the node.

Devnet (the admin key needs devnet SOL for the first deploy, see `docs/ENV.md`):

```
scripts/setup.sh --cluster devnet
scripts/fund_wallet.sh <your wallet address> --cluster devnet
scripts/devnet_verifier.sh
scripts/devnet_node.sh --only-requester <your wallet address>
cd app && npm run dev:devnet
```

Connect a Solana wallet extension (Phantom in Testnet Mode, set to Devnet) and create a request in the console. Add `--cheat no_occlusion --fraction 0.8` to `scripts/devnet_node.sh` to run a cheating node. Set `DEVNET_RPC_URL` (shell) and `VITE_RPC_URL` (`app/.env.local`) to use your own RPC provider instead of the rate-limited public endpoint.

Read-only explorer (needs the two devnet runs under `out/devnet/`; nothing is deployed):

```
scripts/build_explorer.sh
python3 -m http.server 5180 -d app/dist-explorer
```

Tests:

```
anchor test
py/.venv/bin/pytest -q py/tests
cd app && npm test && npm run check
```

## Docs

- `docs/PROTOCOL.md`: state machine, hashing, Merkle format, sampling, checks, detection table (source of truth).
- `docs/LIMITATIONS.md`: everything this prototype does not do, stated plainly.
- `docs/ENV.md`: pinned versions, setup, devnet procedure, explorer deploy steps.

The on-chain program and internal packages keep the working names proof_of_render and porf.

## Prior work

None. (The Anchor project template from `anchor init` was used as the starting point for `programs/proof-of-render` and `Anchor.toml`.)
