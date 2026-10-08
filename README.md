# Proof-of-Render

Verifiable synthetic training data on Solana. A requester escrows (test) USDC for a seeded 3D dataset. A node stakes, renders it with headless Blender, and commits a Merkle root of the results. A verifier samples random frames, checks them against the deterministic seed, and the program either pays the node or slashes it and refunds the requester.

> Hackathon prototype. Status: scaffold only.

## Docs
- `docs/PROTOCOL.md` — state machine, hashing, sampling, detection table (source of truth)
- `docs/ENV.md` — pinned versions and how to activate them
- `docs/LIMITATIONS.md` — everything this does not do

## Console (`app/`)
A three-view React console (create request, status timeline, challenge result) over the same program. `cd app && npm ci && npm run dev`, with a local validator (`scripts/setup.sh`), a node (`python -m porf.node.client --keypair keys/node.json`) and the verifier with its report server (`python -m porf.verifier.client --keypair keys/verifier.json --serve`). On localnet the app signs with a dev wallet (see `docs/LIMITATIONS.md`); `VITE_CLUSTER=devnet` builds without it. Versions and env: `docs/ENV.md`.

## Prior work
None. (The Anchor project template from `anchor init` was used as the starting point for `programs/proof-of-render` and `Anchor.toml`.)

*(Architecture diagram, demo instructions, threat model, FAQ and sponsor-integration notes are written on Day 7.)*
