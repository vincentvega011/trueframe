# LIMITATIONS.md

Plain statements of what this prototype does not do. Every `SHORTCUT:` marker in code must have an entry here. Items marked *(planned)* are known limits of the design; they are listed now so none is forgotten, and each is confirmed or removed when the relevant code lands.

## Trust and economics
- **Single trusted verifier key.** The verifier can collude with a node, or wrongly slash or pass. Production would use a randomized committee plus requester challenges.
- **Optimistic payout.** If the verifier posts no verdict before the challenge window ends, `finalize_payout` pays the node. Liveness of the verifier is assumed.
- **No timeout after `Committed`.** If the verifier never opens a challenge, the request is stuck. Covered only by the trusted-verifier assumption.
- **Network failure can slash an honest node.** A sampled frame the node cannot serve (3 tries × 10 s) is a failed frame. The report labels it "reveal unavailable", but the verdict is still `Fail`. The verifier stops fetching at the first unavailable reveal (the rest are recorded as `not_checked`), so one outage is judged once.
- **The verifier must finish inside the challenge window.** Verdicts are rejected once `challenge_window_end` passes, and a request without a verdict is then paid optimistically. A slow or stuck verifier therefore pays a cheating node. The demo default window is 60 s; verification of a 20-frame request took 4.7 s idle and up to 16.9 s under heavy CPU load on the calibration machine (PROTOCOL.md §17.6). Real deployments need a window longer than the worst-case verification time on the verifier's hardware.
- **Test USDC mint.** Mock mint, configurable via env. The real USDC mint is never used.
- **`init_config` can be front-run.** `Config` is a global singleton PDA created by whoever calls `init_config` first, and that caller becomes admin and chooses the verifier. `scripts/setup.sh` calls it immediately after deploying, so the window is a few seconds on a program nobody has been told about. A production version would require the program's upgrade authority as signer.
- **Classic SPL Token only.** The program supports the original SPL Token program; Token-2022 mints (transfer fees, interest-bearing extensions) are not supported and would break the exact-amount accounting.
- Any signer may claim a request, including its own requester.
- **The verifier chooses when to call `open_challenge`,** so a dishonest verifier could wait for a favorable slot hash and thereby bias which frames are sampled. Acceptable because the verifier is already fully trusted (it also posts the verdict); a randomized committee would fix both. The seed itself is auditable: `Submission.sample_slot` names the SlotHashes entry used, which can be re-read while it is within the sysvar's 512-slot window (about 3.5 minutes).
- **Accounts are never closed.** `Request`, `Submission` and the (emptied) vaults stay on-chain after `Paid` or `Slashed`, so a few thousand lamports of rent per request are never returned. Deliberate: closing the `Request` would erase the terminal status the UI and verifier report read (PROTOCOL.md section 15.1).
- **A payout needs the node's token account to exist** (and a refund the requester's). Anyone can create an associated token account for any owner, so this is recoverable, but the payout fails until it exists.
- A `Fail` verdict cannot be appealed or overturned, and a verdict arriving after the window is rejected; a node whose reveal server was merely offline is slashed.
- Tokens sent to a vault by a third party are not swept by a payout or slash (only the recorded amounts move) and stay in the vault forever.
- Devnet / localnet only. No mainnet.

## Detection
- **Sampling is probabilistic.** For N=20, k=7, a single bad frame is caught only ~28% of the time (table in PROTOCOL.md §5). Frames 0 and N-1 are always sampled.
- **Render quality is not verified.** Checks cover scene identity, annotation plausibility, image sanity and hash consistency, not that the render looks right.
- **`depth.png` is neither hashed nor checked.**
- **Thresholds were calibrated on one machine only** (Apple silicon, macOS, Blender 4.5.14 CPU Cycles; 200 honest frames, 5 seeds, all 4 occlusion levels; PROTOCOL.md §17.5). On that machine honest `seg.png` matches the verifier's re-render bit for bit, so the approved check 7 rule (agreement 0.999, object IoU 0.90, 50 px floor) has a large margin. **Agreement on another CPU, OS or Blender build was not tested.** A Metal GPU render matched the CPU on the same machine (12 frames), which is not evidence about other machines. If an honest node on different hardware differs from the verifier by more than the thresholds allow, it is slashed. Recalibrate with `scripts/calibrate.py` on the real verifier host.
- **Check 7 evasion depends on that bit-exact agreement.** A node that does cheap depth sorting instead of real rendering (the "painter" in the calibration) agrees with the true render 94 to 99.8% of pixels. Under the old placeholder rule (agreement 0.99, object IoU 0.5, 50 px) it **evaded detection in 7, 2, 1 and 9 of 50 frames** (caught 43, 48, 49, 41) at occlusion levels 0 to 3; under the approved rule it was caught in 50 of 50 at every level. That result holds only because honest agreement is exact here; the thresholds must not be loosened without re-measuring. A smarter occlusion model than the painter (one that reproduces the renderer's pixel-level result) is not bounded by these measurements.
- **No verdict when the verifier's own re-render fails.** If the seg-only Blender re-render fails twice (a fresh process each time), the verifier posts neither `Pass` nor `Fail` and retries while the window is open; if the window closes, the node is paid optimistically without having been checked. **The verifier host must have Blender installed** (it refuses to start otherwise).
- **The IoU term is weak by construction:** annotations are tied to `seg.png` (check 6) and boxes to the projected box (check 4), so a node with a self-consistent `seg.png` cannot fail the IoU requirement (PROTOCOL.md §17.2).
- **`rgb.png` content is checked only for validity, size, not-flat and uniqueness.** Nothing relates the picture to `seg.png` or the scene, so a node that commits a geometrically consistent `seg.png` and annotations next to an unrelated (but non-flat, non-duplicate) `rgb.png` passes. This is the "render quality is not verified" limit, stated precisely.
- **Duplicate-image detection is per verifier run:** the set of seen image hashes lives in memory, so images reused across a verifier restart are not recognised.
- Image hashes are of the node's own output. Cross-machine render determinism is not claimed or required; `rgb.png` is not even byte-reproducible on one machine (denoising), while `seg.png`/`depth.png` are.
- Per-object `visibility` is a lower bound (mask pixels over the box silhouette), loose for racks and forklifts.

## Infrastructure
- **No data availability layer.** The node serves its own frames from its own machine. If it disappears, nothing is retrievable (and it is slashed if sampled).
- **Polling, not subscriptions.** Nodes and the verifier poll accounts every few seconds.
- One scene type (`warehouse_v1`); no node bidding, reputation or committee.
- Sponsor integrations: none built. *(Update if any are added after the Day 5 gate.)*

## Shortcuts in code
- **SHORTCUT: the node's reveal URL is not on-chain.** `Request`/`Submission` carry no endpoint. The node writes its URL to `.localnet/reveal.json` (`node/client.py`, `start_server`) and the verifier reads it from that file, from `--reveal-url`, or from `PORF_REVEAL_URL` (`verifier/reveal.py`, `discover_reveal_url`). A real deployment would put an endpoint (or a registry entry) on-chain. Consequence: the verifier trusts whoever controls that file, and only one node per machine can be announced.
- Verifier-supplied blockhash fallback for sampling: **not used**. The SlotHashes sysvar is read on-chain, so the fallback (and its blockhash-grinding caveat) does not apply.

## Node
- **The reveal server is plain HTTP with no authentication** and serves any committed job to anyone who can reach it.
- **One job at a time**, and a crash during rendering discards the half-finished batch (no per-frame resume).
- Output directories are named by `request_id`, which is only unique per requester; the node refuses (before staking) a request whose id collides with an existing job directory rather than mixing the two.
- The node polls with `getProgramAccounts` via the chain CLI (about 0.7 s per call). Fine on localnet; a public devnet RPC may rate-limit it.
- A job marked `failed` is never retried automatically, and a failed job can leave the node's stake locked until the request is refunded or slashed.
- `--cheat` is accepted but does nothing yet; a node started with it behaves honestly (and logs that).
