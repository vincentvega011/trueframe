# PROTOCOL.md — source of truth

Everything here is a **decision**, not a suggestion. Code must match this file; if code and this file disagree, fix one of them in the same change.

Status legend: **[DECIDED]** settled by the human; **[ASSUMED]** my interpretation, awaiting confirmation; **[TBD]** to be filled when the number exists (e.g. calibrated).

---

## 1. State machine

```
Open ──stake_and_claim──▶ Claimed ──submit_commitment──▶ Committed ──open_challenge──▶ Challenged
                                                                                         │
            submit_verdict(Pass), now < window_end ─▶ Verified ──finalize_payout (now ≥ window_end)──▶ Paid
            submit_verdict(Fail), now < window_end ─▶ (stays Challenged, verdict=Fail) ──slash_and_refund (any time)──▶ Slashed
            verdict still Pending, now ≥ window_end ──finalize_payout──▶ Paid   (optimistic, §6)

Open    ──timeout_refund (now > claim_deadline)──▶ Expired
Claimed ──timeout_refund (now > commit_deadline)─▶ Expired
```

`Verified` = verdict `Pass` recorded, payout not yet executed. A `Fail` verdict does not change `Request.status`; `Submission.verdict == Fail` gates `slash_and_refund`.

**Known gap [ASSUMED, flagged]:** `Committed` with no `open_challenge` ever sent has no timeout in the spec. This is covered by the trusted-verifier assumption (LIMITATIONS.md). A `challenge_start_deadline` could be added if wanted; it is not built.

## 2. Config, request parameters, deadlines

**Config PDA** `["config"]`, created by `init_config` **[DECIDED]**: `admin`, `verifier`, `mint`, `stake_bps` (default `2000` = 20%).
Stake: `stake_amount = escrow_amount * stake_bps / 10_000` (checked arithmetic, rounded down).

**Per-request durations, stored on `Request` [DECIDED]:** `claim_duration`, `commit_duration`, `challenge_window_secs` (all `i64` seconds).

| Value | Set when | Rule | Demo default |
|---|---|---|---|
| `claim_deadline` | `create_request` | `created_at + claim_duration` | claim\_duration = 120 s |
| `commit_deadline` | **`stake_and_claim`** | `claimed_at + commit_duration` (so render time does not eat the claim window) | commit\_duration = 600 s |
| `challenge_window_end` | `open_challenge` | `now + challenge_window_secs` | challenge\_window\_secs = 30 s |

Tests warp the clock (LiteSVM `set_sysvar::<Clock>`); no `sleep`.

## 3. Hashing and canonical encoding

- Canonical JSON: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`, UTF-8. No NaN/Infinity. Floats quantized to 4 decimals before entering a scene graph.
- All hashes SHA-256:
  - `scene_hash = sha256(canonical(scene_graph))`
  - `annotation_hash = sha256(canonical(annotations))`
  - `image_hash = sha256(rgb_png_bytes)`
  - `seg_hash = sha256(seg_png_bytes)` **[DECIDED]**
- `depth.png` is rendered and served but **not hashed and not checked** **[DECIDED]**. Stated in LIMITATIONS.md.

**Per-frame RNG:** `random.Random(int.from_bytes(sha256(seed_u64_le || frame_index_u32_le)[:8], "little"))`.

**Depth encoding [DECIDED]:** 16-bit PNG, fixed `near`/`far` stored in the scene graph's camera block (never adaptive per frame). `depth_u16 = round(65535 * clamp((z - near) / (far - near), 0, 1))`, `z` = distance along the camera axis in metres. `near`/`far` values **[TBD]** — fixed in `scene/camera.py` and copied here.

## 4. Merkle commitment

- Leaf **[DECIDED]**: `sha256(0x00 || frame_index_u32_le || scene_hash || annotation_hash || image_hash || seg_hash)`  (1 + 4 + 4×32 = 133 bytes preimage)
- Node: `sha256(0x01 || left || right)`
- Odd node at a level: duplicate it.
- Single leaf: root = leaf (tree height 0).
- `frame_index` is **0-based**.
- **Proof format [DECIDED]:** ordered list (leaf level upward) of `(sibling_hash, sibling_is_left)`.

**Verification must do all of the following [DECIDED]:**
1. Recompute the leaf from the revealed fields; fold the proof to a root; compare to the on-chain root.
2. `frame_index < frame_count`.
3. Proof length == `ceil(log2(frame_count))` (0 when `frame_count == 1`).
4. The position implied by the proof equals `frame_index`: bit *i* of the position is `1` iff `sibling_is_left` at level *i*.

Rationale: with odd-node duplication, a proof for the last leaf also "verifies" at a phantom index `N`. Because the leaf commits to `frame_index` and checks 2–4 pin the position, a duplicated-last-leaf forgery fails. `py/tests/test_merkle.py` must contain that forgery test (must fail verification), plus vectors for first, middle, last, and odd-count trees.

## 5. Sampling

On-chain derivation **[DECIDED]**, inside `open_challenge`:
- Read the SlotHashes sysvar (`UncheckedAccount` with `address = sysvar::slot_hashes::ID`), reading the **most recent entry directly from the account data** without deserializing the whole sysvar. Layout: `u64 len` then entries of `(u64 slot, [u8;32] hash)`, newest first, so the entry is at offset 8.
- Require `entry.slot > submission.commit_slot`.
- `sample_seed = sha256(merkle_root || entry.hash)`; stored in `Submission.sample_seed`.
- **Time box: 1 hour.** If this cannot be made to work, fall back to a verifier-supplied blockhash + slot (program requires `slot > commit_slot`), mark `// SHORTCUT:`, and add the grinding caveat to LIMITATIONS.md. **Status: [TBD] — SlotHashes path in use unless recorded otherwise here.**

Indices are computed **off-chain by one shared pure function** (`py/porf/verifier/sampler.py`) **[DECIDED]**:
- `rng = random.Random(int.from_bytes(sample_seed, "little"))`
- If `N >= 7`: `indices = {0, N-1}` ∪ 5 unique draws from `1..N-2` → **7 total**.
- If `N < 7`: `min(5, N)` unique draws from `0..N-1`.
- Unique draws: repeat `rng.randrange(lo, hi + 1)`, rejecting duplicates, until the count is reached. Return sorted.
- `Submission.sample_count` = the total (7 for N=20) **[DECIDED]**.

### Detection probability

For `N = 20`, `k = 7` (frames 0 and 19 always sampled + 5 of the 18 middle frames). Worst case for the node: the `b` bad frames are all hidden in the middle (bad frames at index 0 or N-1 are caught with probability 1).
Exact: `P(detect) = 1 − C(18−b, 5) / C(18, 5)`. Rough: `1 − (1−f)^7`.

| bad frames `b` | fraction `f` | exact | `1−(1−f)^7` |
|---|---|---|---|
| 1 | 5% | 0.278 | 0.302 |
| 2 | 10% | 0.490 | 0.522 |
| 3 | 15% | 0.650 | 0.679 |
| 4 | 20% | 0.766 | 0.790 |
| 6 | 30% | 0.908 | 0.918 |
| 9 | 45% | 0.985 | 0.985 |
| 12 | 60% | 0.999 | 0.998 |
| 18 | 90% | 1.000 | 1.000 |

Sparse cheating (one bad frame in 20) is caught only ~28% of the time. This is stated openly in the README. The demo cheat modes affect many frames, so they are caught with probability ≈ 1.

## 6. Verdicts, payout, slash

All **[DECIDED]**.

- `submit_verdict` — verifier key only. Accepted **only while `now < challenge_window_end`**, `status == Challenged`, and `verdict == Pending`. A verdict after the window ends is rejected (typed error).
- `finalize_payout` — anyone. Allowed when `now >= challenge_window_end` and `verdict ∈ {Pass, Pending}`: escrow → node, stake → node, status `Paid`. A `Pending` verdict at window end pays the node so funds are never silently locked. This relies on the trusted verifier being live (LIMITATIONS.md). Payout on `Fail` is rejected.
- `slash_and_refund` — anyone, any time after a `Fail` verdict is recorded (no need to wait for the window to end). Requires `verdict == Fail`: escrow → requester, **stake → requester**, status `Slashed`. No treasury. Slash on `Pass` is rejected.
- `timeout_refund` — anyone. `Open` past `claim_deadline`: escrow → requester. `Claimed` past `commit_deadline`: escrow → requester **and stake → requester**. Status `Expired`. (The node is penalised for not committing.)
- Because verdicts close at `challenge_window_end` and `Pending` then pays, the window length is the verifier's deadline. Verifier tooling must finish within it, including the retry budget in §7.

## 7. Verifier checks

For each sampled index, fetch the reveal (`GET /frame/<i>`): `scene.json`, `annotations.json`, `rgb.png`, `seg.png`, leaf fields, Merkle proof.

1. Merkle proof valid against the on-chain root, including the §4 position/range checks.
2. Scene match: regenerate from `(seed, frame_index)`; `sha256(canonical(...))` equals the revealed `scene_hash`.
3. Hash consistency: revealed `scene.json`, `annotations.json`, `rgb.png`, **`seg.png`** hash to the committed fields.
4. Annotation plausibility (pinhole projection, `scene/projection.py`): each node box lies within the projected full-object box (pixel tolerance) and IoU ≥ a threshold scaled by visibility. **Tolerance values: [TBD — calibrate on honest runs, Day 4, record here.]**
5. Image sanity: correct resolution, valid PNG, not a duplicate of another sampled frame or previously seen image hash.
6. Segmentation consistency: instance ids in `seg.png` match object ids in the scene graph.

**Unavailable reveal [DECIDED]:** 3 attempts, 10 s timeout each. Still failing ⇒ that frame fails with reason **"reveal unavailable"** (reported distinctly from any check failure) and the verdict is `Fail`. A network failure can therefore slash an honest node (LIMITATIONS.md).

Verdict is `Pass` only if every sampled frame passes every check. The report (JSON + short text) lists frames, checks, and failure reasons.

## 8. Cheat modes → checks that catch them

| Mode | Primary catching check |
|---|---|
| `reuse_frames` | 5 (duplicate image hash); also 2/3 (scene/image mismatch for the copied frame) |
| `fake_boxes` | 4 (annotation plausibility); 3 if the committed hash is of the real boxes |
| `wrong_seed` | 2 (scene match) |
| `partial` | 5 (duplicates); the forced frame N-1 guarantees a tail check |

## 9. Cluster and keys [DECIDED / ASSUMED]

- Develop and test on localnet / LiteSVM. Record the demo on devnet. `demo_honest.sh` and `demo_cheat.sh` take `--cluster {localnet,devnet}` so recording can fall back to localnet.
- `scripts/setup.sh` **[ASSUMED]**: the human funds one payer keypair once from faucet.solana.com; the script transfers SOL from it to the node, verifier and requester keypairs, creates the mock USDC mint (configurable via env, never the real mint), and mints mock USDC to the requester.

## 10. On-chain account layout additions

`Request` gains `claim_duration`, `commit_duration`, `challenge_window_secs`, `created_at`, `claimed_at` (all `i64`). `Submission` unchanged. `Config` added. `stake_amount` derives from `Config.stake_bps`. New instruction `init_config`.

## 11. Testing approach

Program tests are **Rust LiteSVM** (`programs/proof-of-render/tests/`, run by `anchor test` / `cargo test`), not TypeScript on a localnet validator — see ENV.md. LiteSVM provides the clock warping and sysvar injection (SlotHashes) these protocol rules need.

Required tests: one per transition, plus wrong signer, wrong status, wrong mint, deadline not reached, double claim, double payout, payout before window ends, slash on `Pass` (must fail), payout on `Fail` (must fail), **verdict after window end (must fail)**, **payout on `Pending` after window end (must succeed)**, **timeout refund after claim-without-commit pays the stake to the requester**.

## 12. Notable design choices

1. Leaf includes `seg_hash`.
2. Sampling rule is exact (§5); `open_challenge` derives the seed on-chain.
3. `commit_deadline` is set at claim time, not creation.
4. `Config` PDA + `init_config`, `stake_bps` in Config.
5. `finalize_payout` also pays on a `Pending` verdict after the window; verdicts close when the window ends.
6. A failed commit after claiming forfeits the stake to the requester.
7. Program tests are Rust LiteSVM.
8. Chain client is a TypeScript CLI on `@anchor-lang/core` called from Python (ENV.md).
9. Node cheat modes corrupt ≥ 50% of frames by default and take `--fraction`, so the demo is reliable; the single-bad-frame row of the detection table is reproduced by passing a small `--fraction`.
