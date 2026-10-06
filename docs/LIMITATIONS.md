# LIMITATIONS.md

Plain statements of what this prototype does not do. Every `SHORTCUT:` marker in code must have an entry here. Items marked *(planned)* are known limits of the design; they are listed now so none is forgotten, and each is confirmed or removed when the relevant code lands.

## Trust and economics
- **Single trusted verifier key.** The verifier can collude with a node, or wrongly slash or pass. Production would use a randomized committee plus requester challenges.
- **Optimistic payout.** If the verifier posts no verdict before the challenge window ends, `finalize_payout` pays the node. Liveness of the verifier is assumed.
- **No timeout after `Committed`.** If the verifier never opens a challenge, the request is stuck. Covered only by the trusted-verifier assumption.
- **Network failure can slash an honest node.** A sampled frame the node cannot serve (3 tries × 10 s) is a failed frame. The report labels it "reveal unavailable", but the verdict is still `Fail`.
- **Test USDC mint.** Mock mint, configurable via env. The real USDC mint is never used.
- **`init_config` can be front-run.** `Config` is a global singleton PDA created by whoever calls `init_config` first, and that caller becomes admin and chooses the verifier. `scripts/setup.sh` calls it immediately after deploying, so the window is a few seconds on a program nobody has been told about. A production version would require the program's upgrade authority as signer.
- **Classic SPL Token only.** The program supports the original SPL Token program; Token-2022 mints (transfer fees, interest-bearing extensions) are not supported and would break the exact-amount accounting.
- Any signer may claim a request, including its own requester.
- Devnet / localnet only. No mainnet.

## Detection
- **Sampling is probabilistic.** For N=20, k=7, a single bad frame is caught only ~28% of the time (table in PROTOCOL.md §5). Frames 0 and N-1 are always sampled.
- **Render quality is not verified.** Checks cover scene identity, annotation plausibility, image sanity and hash consistency, not that the render looks right.
- **`depth.png` is neither hashed nor checked.**
- Annotation plausibility uses tolerances calibrated on a small number of honest runs *(values TBD, PROTOCOL.md §7)*.
- Image hashes are of the node's own output. Cross-machine render determinism is not claimed or required; `rgb.png` is not even byte-reproducible on one machine (denoising), while `seg.png`/`depth.png` are.
- Per-object `visibility` is a lower bound (mask pixels over the box silhouette), loose for racks and forklifts.

## Infrastructure
- **No data availability layer.** The node serves its own frames from its own machine. If it disappears, nothing is retrievable (and it is slashed if sampled).
- **Polling, not subscriptions.** Nodes and the verifier poll accounts every few seconds.
- One scene type (`warehouse_v1`); no node bidding, reputation or committee.
- Sponsor integrations: none built. *(Update if any are added after the Day 5 gate.)*

## Shortcuts in code
- **SHORTCUT: the node's reveal URL is not on-chain.** `Request`/`Submission` carry no endpoint. The node writes its URL to `.localnet/reveal.json` (`node/client.py`, `start_server`) and the verifier is pointed at it through that file or `PORF_REVEAL_URL`. A real deployment would put an endpoint (or a registry entry) on-chain. Consequence: the verifier trusts whoever controls that file, and only one node per machine can be announced.
- Verifier-supplied blockhash fallback for sampling: **not used**; SlotHashes is the intended source. If that changes it must be recorded here with the grinding caveat (the verifier could grind blockhashes to bias the sample).

## Node
- **The reveal server is plain HTTP with no authentication** and serves any committed job to anyone who can reach it.
- **One job at a time**, and a crash during rendering discards the half-finished batch (no per-frame resume).
- Output directories are named by `request_id`, which is only unique per requester; the node refuses (before staking) a request whose id collides with an existing job directory rather than mixing the two.
- The node polls with `getProgramAccounts` via the chain CLI (about 0.7 s per call). Fine on localnet; a public devnet RPC may rate-limit it.
- A job marked `failed` is never retried automatically, and a failed job can leave the node's stake locked until the request is refunded or slashed.
- `--cheat` is accepted but does nothing yet; a node started with it behaves honestly (and logs that).
