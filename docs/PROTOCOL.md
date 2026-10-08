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
| `challenge_window_end` | `open_challenge` | `now + challenge_window_secs` | challenge\_window\_secs = **60 s** (`DEFAULT_CHALLENGE_WINDOW_SECS`; derivation in section 17.6) |

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

**Depth encoding [DECIDED]:** 16-bit PNG, fixed `near`/`far` stored in the scene graph's camera block (never adaptive per frame). `depth_u16 = round(65535 * clamp((z - near) / (far - near), 0, 1))`, `z` = distance along the camera axis in metres. `near = 0.1`, `far = 40.0` (metres, constants in `scene/camera.py`, written into every scene graph's camera block). Depth is the distance along the camera's viewing axis, not the Euclidean distance to the camera.

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

### 4.1 Exact byte layout

Leaf preimage, 133 bytes, no length prefixes or separators:

| Offset | Length | Field |
|---|---|---|
| 0 | 1 | domain tag `0x00` |
| 1 | 4 | `frame_index`, unsigned 32-bit, **little-endian** |
| 5 | 32 | `scene_hash` (raw digest bytes, not hex) |
| 37 | 32 | `annotation_hash` |
| 69 | 32 | `image_hash` |
| 101 | 32 | `seg_hash` |

Node preimage, 65 bytes: `0x01` (1) ‖ `left` (32) ‖ `right` (32). All hashes are raw 32-byte SHA-256 digests; hex appears only in JSON transport and logs.

Proof position: leaf-level sibling first; at level *i*, `sibling_is_left = true` means the running node is the right child, contributing bit *i* of the index. Expected proof length is `ceil(log2(frame_count))`, i.e. `(frame_count - 1).bit_length()`.

Canonical JSON note: Python's float repr is part of the format (`1.0` stays `1.0`, `2.5` stays `2.5`), which is another reason floats are quantized to 4 decimals before entering a scene graph. Non-ASCII is escaped as `\uXXXX`, with astral characters as UTF-16 surrogate pairs.

### 4.2 Worked example

Inputs: `frame_index = 5`; `scene_hash` = 32 × `0x11`; `annotation_hash` = 32 × `0x22`; `image_hash` = 32 × `0x33`; `seg_hash` = 32 × `0x44`.

Preimage (hex, 133 bytes):

```
00                                  tag
05000000                            frame_index = 5, u32 little-endian
1111111111111111111111111111111111111111111111111111111111111111   scene_hash
2222222222222222222222222222222222222222222222222222222222222222   annotation_hash
3333333333333333333333333333333333333333333333333333333333333333   image_hash
4444444444444444444444444444444444444444444444444444444444444444   seg_hash
```

`leaf = sha256(preimage) = 5b4faa09ef2bcb0f4a444889da86952db0534a0f1e6cf209b81e411e5de53fd4`

Reference vectors in `py/tests/test_merkle.py` were generated by a separate hashlib-only script, not by `porf.merkle`. With per-frame fields `sha256("scene{i}")`, `sha256("ann{i}")`, `sha256("img{i}")`, `sha256("seg{i}")`: leaf 0 = `696717fc…b1cf`, and the 3-leaf root (leaf 2 paired with itself) = `831dc8a7…2f08`.

Canonical JSON example: `{"b":1,"a":[1,2.5,"é"],"c":{"z":null,"y":true}}` encodes to `{"a":[1,2.5,"\u00e9"],"b":1,"c":{"y":true,"z":null}}`, whose sha256 is `eb9504ed6d071f0e465f9ab5f5e7fb193db5b7eae14953711412e8a31f7c65d5`.

### 4.3 Forgery that the position checks prevent

In a 3-leaf tree, leaf 2 is paired with a copy of itself. Flipping that sibling to `sibling_is_left = true` still folds to the real root, but it implies index 3 (= `frame_count`). Both the range check (`frame_index < frame_count`) and the position check (`implied position == frame_index`) reject it; `test_duplicated_last_leaf_forgery_*` cover this, and removing either check makes tests fail.

## 5. Sampling

On-chain derivation **[DECIDED]**, inside `open_challenge`:
- Read the SlotHashes sysvar (`UncheckedAccount` with `address = sysvar::slot_hashes::ID`), reading the **most recent entry directly from the account data** without deserializing the whole sysvar. Layout: `u64 len` then entries of `(u64 slot, [u8;32] hash)`, newest first, so the entry is at offset 8.
- Require `entry.slot > submission.commit_slot`.
- `sample_seed = sha256(merkle_root || entry.hash)`; stored in `Submission.sample_seed`. The slot of that entry is stored in **`Submission.sample_slot`** so anyone can audit the seed: read the SlotHashes entry for `sample_slot` (while it is still in the sysvar's 512-slot window, about 3.5 minutes) and recompute the hash. `py/tests/test_challenge_flow.py` does exactly this against a live chain.
- The newest SlotHashes entry is the **previous** slot (the current slot's hash does not exist yet), so `open_challenge` in the same slot as the commit fails with `SlotHashNotNewer`; the verifier client retries (bounded: `ChainClient.open_challenge(max_attempts=20, delay=0.4)`, a clear error when exhausted). Empty or malformed sysvar data is `SlotHashesUnavailable`. The parser trusts the length prefix and validates it against the account size; it never reads past the data.
- **Time box: 1 hour.** If this cannot be made to work, fall back to a verifier-supplied blockhash + slot (program requires `slot > commit_slot`), mark `// SHORTCUT:`, and add the grinding caveat to LIMITATIONS.md. **Status: DONE: the SlotHashes path works, in LiteSVM (injected) and on a real validator; the verifier-supplied fallback was not needed and is not in the code.**

Indices are computed **off-chain by one shared pure function** (`py/porf/verifier/sampler.py`) **[DECIDED]**:
- `rng = random.Random(int.from_bytes(sample_seed, "big"))`: the RNG is seeded **explicitly from an integer, big-endian**. Never pass the bytes to `Random` (it would hash them with a different, version-dependent scheme), and never use little-endian here; `test_the_rng_is_seeded_big_endian_from_an_integer` pins the difference. (The per-frame scene RNG of section 3 is a different RNG and stays little-endian.)
- If `N >= 7`: `indices = {0, N-1}` ∪ 5 unique draws from `1..N-2` → **7 total**.
- If `N < 7`: `min(5, N)` unique draws from `0..N-1`.
- Unique draws: repeat `rng.randrange(lo, hi + 1)`, rejecting duplicates, until the count is reached. Return sorted.
- `Submission.sample_count` = the total, computed **on-chain** in `open_challenge` by `state::sample_count_for` **[DECIDED]**:

| N (`frame_count`) | `sample_count` | Composition |
|---|---|---|
| 1 | 1 | the only frame |
| 2 | 2 | both frames |
| 5 | 5 | all five |
| 6 | 5 | 5 unique draws from `0..5` |
| 7 | 7 | frames 0 and 6, plus all 5 of `1..5` |
| 8 | 7 | frames 0 and 7, plus 5 unique draws from `1..6` |
| 20 | 7 | frames 0 and 19, plus 5 unique draws from `1..18` |
| 1024 | 7 | frames 0 and 1023, plus 5 unique draws from `1..1022` |

**Required cross-check test for the Python sampler (Day 4b):** for each N in this table, `sampler(sample_seed, N)` must return **exactly** `sample_count` unique, sorted, in-range indices (and, for `N >= 7`, must contain 0 and N-1); and the on-chain `sample_count` read from a challenged request must equal `len(sampler(...))`. The Rust unit test `state::tests::sample_count_table` and the LiteSVM test `sample_count_follows_the_protocol_table` pin the on-chain side of the same table.

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

**Status transitions (implemented):**

| From | Instruction | To |
|---|---|---|
| `Committed` | `open_challenge` (verifier) | `Challenged` (window starts) |
| `Challenged`, verdict `Pending`, `now < window_end` | `submit_verdict(Pass)` | **`Verified`**, verdict `Pass` |
| same | `submit_verdict(Fail)` | **stays `Challenged`**, verdict `Fail` (only the verdict field changes) |
| `Verified`, or `Challenged` with verdict `Pending`; `now >= window_end` | `finalize_payout` | `Paid` |
| `Challenged` with verdict `Fail`, any time | `slash_and_refund` | `Slashed` |

**Check order**, so each failure has one predictable error: `submit_verdict`: verifier (`UnauthorizedVerifier`), status `Challenged` (`InvalidStatus`), argument is not `Pending` (`InvalidVerdict`), verdict not yet set (`VerdictAlreadySet`), `now < window_end` (`ChallengeWindowClosed`). `finalize_payout`: status `Challenged` or `Verified` (`InvalidStatus`), verdict not `Fail` (`VerdictIsFail`), `now >= window_end` (`ChallengeWindowOpen`). `slash_and_refund`: verdict is `Fail` (`VerdictNotFail`), status `Challenged` (`InvalidStatus`). Consequences: a second verdict after `Pass` is `InvalidStatus` (status moved to `Verified`) but after `Fail` it is `VerdictAlreadySet`; a slash on `Pass` or `Pending` is `VerdictNotFail`; a second slash or a payout after a slash is `InvalidStatus`.

**Money:** payouts move the **recorded** `escrow_amount` and `stake_amount` (not the vault balances), as the `Request` PDA, with `transfer_checked`. Tokens sent to a vault by someone else stay in the vault. `finalize_payout` requires the destination token account to be owned by `request.node`; `slash_and_refund` requires it to be owned by `request.requester`. Passing a vault as its own destination, or the two vaults swapped, is refused by Anchor 1.x's duplicate-mutable-account check (`ConstraintDuplicateMutableAccount`) before any other check.

## 7. Verifier checks

For each sampled index, fetch the reveal (`GET /frame/<i>`): `scene.json`, `annotations.json`, `rgb.png`, `seg.png`, leaf fields, Merkle proof.

1. Merkle proof valid against the on-chain root, including the §4 position/range checks.
2. Scene match: regenerate from `(seed, frame_index)`; `sha256(canonical(...))` equals the revealed `scene_hash`.
3. Hash consistency: revealed `scene.json`, `annotations.json`, `rgb.png`, **`seg.png`** hash to the committed fields.
4. Annotation plausibility (pinhole projection, `scene/projection.py`): each node box lies within the projected full-object box (pixel tolerance) and IoU ≥ a threshold scaled by visibility. The projection uses the scene **regenerated from the chain**, and the visibility is **derived from `seg.png`**, never taken from the node's annotation. **Calibrated thresholds (section 17.5): pixel tolerance 1.0 px and IoU slack 0.30, identical for all four classes.**
5. Image sanity: correct resolution, valid PNG, not a duplicate of another sampled frame or previously seen image hash.
6. Segmentation consistency: every id in `seg.png` is 0 or an object id of the scene graph. **Also** (a deliberate strengthening, since both files are committed): each annotation's `bbox`, `mask_pixels` and `visibility` must equal what `seg.png` yields, using the same code as the renderer (`porf/seg.py`, `scene/projection.visibility_fraction`). This makes a shifted or invented box detectable without any tolerance.

**Unavailable reveal [DECIDED]:** 3 attempts, 10 s timeout each. Still failing ⇒ that frame fails with reason **"reveal unavailable"** (reported distinctly from any check failure) and the verdict is `Fail`. A network failure can therefore slash an honest node (LIMITATIONS.md).

7. **Seg re-render** (`seg_rerender`): the verifier renders the id pass itself, for all sampled frames in one Blender process, from the scene regenerated from the chain's parameters at the request's resolution, and compares it with the revealed `seg.png`: global pixel agreement at least **0.999** and, for every object with at least **50** pixels in either image, a mask IoU of at least **0.90** (section 17.5). This is the only check that sees occlusion.

Verdict is `Pass` only if every sampled frame passes every check. The report (JSON + short text) lists frames, checks, and failure reasons. **Implementation of the checks, the verifier loop and the report: section 17.**

## 8. Cheat modes → checks that catch them

Every mode commits a **consistent** Merkle tree over its fake data (the leaves are built after the cheat rewrites the files), so the proof and hash checks (1, 3) never fire; each frame's `scene.json` is the honest scene of that frame except in `wrong_seed`. The frames a mode corrupts are `cheats.bad_frames(mode, seed, n, fraction)`, section 16.6. Observed = the live 20-frame, occlusion-3, default-fraction run of each mode (section 8.2).

| Mode | What the node does to a bad frame | Checks expected to fire on a sampled bad frame | Observed |
|---|---|---|---|
| `reuse_frames` | rgb, depth, seg and annotations are copied from a good frame; own honest `scene.json` | **4, 6, 7** (the copied boxes and seg describe another scene); **5** on both frames when a copy and its source (or two copies) are in the same sample | 4, 6, 7 on every bad frame; 5 on the two honest source frames whose copies were sampled |
| `fake_boxes` | boxes shifted 12 to 40 px, rescaled 0.7x to 1.4x, invented for hidden objects; honest seg | **6** always (boxes must equal what `seg.png` yields), **4** usually (box outside the projected box); not 7 | 4 and 6 on every bad frame |
| `wrong_seed` | frame rendered from a different seed | **2** always; also 4, 6, 7 (the wrong scene's boxes and ids do not fit the requested scene) | 2, 4, 6, 7 |
| `partial` | only the first (1-f)*n frames are rendered; each tail frame is a copy of a rendered frame | as `reuse_frames`: **4, 6, 7**, plus **5** when copy and source are both sampled. Frame n-1 is always a bad frame and always sampled | 4, 6, 7 on every bad frame; 5 on two of them |
| `no_occlusion` | full silhouettes painted in id order into `seg.png`, boxes derived from it | **7 only**; checks 1 to 6 pass | 7 only |

### 8.1 Detection probability for N = 20, k = 7

The sample is frames 0 and 19 plus 5 of the 18 middle frames (section 5). The selector hides bad frames in the middle first, which is the node's best strategy, so these are worst-case for the verifier. A sampled bad frame is caught with certainty by every mode above (the checks in the table fire on every one of them in the live runs); the probability is therefore that of sampling at least one bad frame: `1 - C(18-b, 5) / C(18, 5)`.

| Case | bad frames `b` | P(detect), hidden in the middle | `partial` (always includes frame 19) |
|---|---|---|---|
| fraction 0.5 (the default) | 10 | **0.9935** (a node that gets away: 56 of 8568 samples) | 1.000 |
| a single bad frame (`--fraction 0.05`) | 1 | **0.278** | 1.000 |

So one run of a default-fraction cheat can, with probability 0.65%, evade the verifier for the four middle-hiding modes; the demo requests are chosen so that this is checked beforehand (the selection is deterministic per seed and the sample is public once drawn). `1 - (1-f)^7` gives 0.992 and 0.302.

### 8.2 Live end-to-end results (localnet, 20 frames, occlusion 3, default fraction 0.5, production render settings, 60 s window)

| Mode | Caught | Bad frames sampled / 7 | Checks that fired | Stake slashed | Refunded to requester |
|---|---|---|---|---|---|
| `reuse_frames` | yes | 3 | 4, 5, 6, 7 | 4.0 USDC | 24.0 USDC (20.0 escrow + 4.0 stake) |
| `fake_boxes` | yes | 5 | 4, 6 | 4.0 USDC | 24.0 USDC |
| `wrong_seed` | yes | 1 | 2, 4, 6, 7 | 4.0 USDC | 24.0 USDC |
| `partial` | yes | 3 | 4, 5, 6, 7 | 4.0 USDC | 24.0 USDC |
| `no_occlusion` | yes | 1 | 7 | 4.0 USDC | 24.0 USDC |

The reproduction is `py/.venv/bin/pytest -q py/tests/test_verifier_flow.py -k every_cheat -s` on a fresh localnet (prints one `CHEAT_RESULT` JSON line per mode). `wrong_seed` and `no_occlusion` were caught on a single sampled bad frame each, as 0.9935 predicts for most draws but not all.

## 9. Cluster and keys [DECIDED / ASSUMED]

- Develop and test on localnet / LiteSVM. Record the demo on devnet. `demo_honest.sh` and `demo_cheat.sh` take `--cluster {localnet,devnet}` so recording can fall back to localnet.
- `scripts/setup.sh` **[ASSUMED]**: the human funds one payer keypair once from faucet.solana.com; the script transfers SOL from it to the node, verifier and requester keypairs, creates the mock USDC mint (configurable via env, never the real mint), and mints mock USDC to the requester.

## 10. On-chain account layout additions

`Request` gains `claim_duration`, `commit_duration`, `challenge_window_secs`, `created_at`, `claimed_at` (all `i64`). `Submission` gains `sample_slot: u64` (set by `open_challenge`). `Config` added. `stake_amount` derives from `Config.stake_bps`. New instruction `init_config`.

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

## 13. Scene generator and camera conventions (`warehouse_v1`)

Code: `py/porf/scene/{generator,camera,projection}.py`. The Blender job must reproduce these conventions exactly; the verifier's projection check depends on them.

### 13.1 Coordinate conventions

- **World:** right-handed, **+Z up**, metres. The floor spans x ∈ [−10, 10], y ∈ [−8, 8], top surface at z = 0. Objects stay at least 0.5 m inside the walls.
- **Object pose:** `location` is the centre of the object's box; `dimensions = [sx, sy, sz]` are full extents along the object's local x, y, z; `yaw_deg` is a rotation about +Z, counter-clockwise seen from above (local +x → world +y at 90°). No pitch or roll. Blender: unit cube scaled by `dimensions / 2` (Blender's default cube is 2 m wide), `rotation_euler = (0, 0, radians(yaw_deg))`.
- **Camera pose:** `location`, `yaw_deg`, `pitch_deg`, no roll. Yaw 0 looks along **+Y**; positive yaw turns counter-clockwise seen from above (yaw 90 looks along −X, 180 along −Y, 270 along +X). Positive pitch looks **down**. World-space unit vectors: forward = (−cos p · sin y, cos p · cos y, −sin p); right = (cos y, sin y, 0); up = (−sin p · sin y, sin p · cos y, cos p).
- **Blender camera:** cameras look down their local **−Z** with **+Y up** and +X right. This pose equals `rotation_euler = (radians(90 − pitch_deg), 0, radians(yaw_deg))`, mode `XYZ` (R = Rz·Ry·Rx), at `location`.
- **FOV:** `fov_deg` is the **horizontal** field of view (`fov_axis = "horizontal"`). In Blender set `sensor_fit = 'HORIZONTAL'`, `lens_unit = 'FOV'`, `angle = radians(fov_deg)`, `shift_x = shift_y = 0`, `clip_start = near`, `clip_end = far`, render resolution = `resolution` at 100%, pixel aspect 1:1. Never rely on `sensor_fit = 'AUTO'`. Pixels are square: focal length in pixels `f = (W/2) / tan(fov/2)`; vertical FOV is derived (`2·atan(tan(fov/2)·H/W)`).
- **Image/pixel space:** origin at the **top-left corner** of the top-left pixel, **u to the right, v downward**; pixel (i, j) covers [i, i+1) × [j, j+1) and its centre is (i + 0.5, j + 0.5). The principal point is the image centre (W/2, H/2). Saved PNGs are top-down; whatever Blender's bottom-up buffers do must be corrected so PNGs match this.
- **Projection:** with camera-space coordinates (xc, yc, zc) (x right, y up, looking down −z) and depth d = −zc: `u = W/2 + f·xc/d`, `v = H/2 − f·yc/d`.

### 13.2 Full-object 2D box

`projection.project_box` takes the 8 corners of the object's yawed box, clips its 12 edges to the near plane (depth ≥ `near`), projects them, takes the bounding rectangle and clips it to [0, W] × [0, H]. Result: `status` ∈ {`ok`, `behind_camera` (entire box nearer than the near plane), `outside_image` (in front but fully off-screen)}, a `box` (None unless `ok`), and `truncated` (cut by the near plane or the image border). This is the *full* extent ignoring occlusion, so a node's visible-mask box must lie inside it. **Any geometry the renderer builds for an object (rack shelves, forklift parts) must stay inside that object's box**, otherwise this guarantee breaks. Racks, pallets, boxes and the forklift are each one box in the scene graph.

### 13.3 Scene graph

```
{ "scene_type": "warehouse_v1", "seed": u64, "frame_index": u32, "occlusion_level": 0..3,
  "camera":   { "location": [x,y,z], "yaw_deg", "pitch_deg", "fov_deg", "fov_axis": "horizontal",
                "resolution": [W,H], "near": 0.1, "far": 40.0 },
  "lighting": { "sun": { "yaw_deg", "elevation_deg", "energy" }, "ambient": 0.3 },
  "floor":    { "size": [20.0, 16.0] },
  "objects":  [ { "id", "class", "dimensions": [..], "location": [..], "yaw_deg",
                  "occlusion": { "placed_behind": id | null, "kind": "none" | "rack" | "pallet" } }, ... ] }
```

- **Ids** are integers assigned in generation order starting at 1, and `objects` is built in that order (racks, then pallets, then boxes on pallets, then loose boxes, then the optional forklift). Nothing is iterated from a `set` or `dict`. Segmentation instance id = object id (0 = background); class ids: `rack` 1, `pallet` 2, `box` 3, `forklift` 4.
- **`occlusion.placed_behind`** records the generator's *placement intent*: this object was positioned directly behind the object with that id, as seen along the camera's aiming direction (boxes inherit it from their pallet). It does not guarantee the object is invisible; actual visibility comes from the segmentation mask.
- The scene hash covers the whole graph including `seed`, `frame_index` and `occlusion_level`.

### 13.4 Generation rules and occlusion levels

Per frame, one RNG (`frame_rng(seed, frame_index)`, §3) drives everything, so frames are independent scenes. Order: racks → aim direction, focus rack and camera pose (with the guard in 13.4.1) → pallets → boxes → forklift → lighting. Racks (3–5, 2.5/3.0/3.5 m long, 1 m deep, 3/3.5/4 m tall, yaw 0 or 90) are placed by rejection sampling with 1.8 m aisles. The camera aims along one of the four axis directions at a randomly chosen **focus rack**. Six pallets (1.2 × 0.8 × 0.15 m, yaw 0 or 90) are placed. "Open" pallets go in the corridor between camera and focus rack; "hidden" pallets go directly behind the focus rack or behind another pallet. 0–3 boxes sit on each pallet; 4–6 loose boxes and a forklift (50%) go on the floor, preferably in the camera's view (see 13.4.1).

| `occlusion_level` | hidden pallets (of 6) | camera height z (m) | pitch (° down) | distance to focus rack (m) | yaw jitter (±°) |
|---|---|---|---|---|---|
| 0 | 0 | 3.5 – 5.0 | 25 – 35 | 7.0 – 10.0 | 12 |
| 1 | 1 | 2.5 – 3.5 | 18 – 28 | 6.0 – 8.5 | 10 |
| 2 | 3 | 1.6 – 2.5 | 8 – 18 | 4.5 – 7.0 | 7 |
| 3 | 5 | 0.9 – 1.6 | 0 – 8 | 3.5 – 5.5 | 5 |

Level 3 therefore puts a low, nearly horizontal camera close to a tall rack with most pallets hidden behind it or behind other pallets; level 0 looks down from above at pallets standing in the open. A hidden pallet whose placement attempts all fail (30 tries) is skipped, so the realised count can be below the target (seed 42, frames 0–59, level 3: 5 hidden pallets in 56 frames, 4 in the other 4; levels 1 and 2 always place all of theirs). Other draws: horizontal FOV 62–78°, sun yaw 0–360°, elevation 35–75°, energy 2–4.

#### 13.4.1 Camera pose guard and view-cone placement

Without a guard, level 3 produced near-degenerate frames: a camera pushed against a wall 2–3 m from a rack that fills the whole view, or aimed at a rack standing against the far wall so nothing could be placed behind it. The camera pose (aim direction, focus rack, distance, lateral offset, height) is therefore validated **after the wall clamp**, on the final position:

| Rule | Value |
|---|---|
| Plan-view distance from the camera to the nearest rack surface (any rack) | ≥ `MIN_RACK_DIST` = 3.5 m |
| Camera height | ≥ `MIN_CAM_HEIGHT` = (3.5, 2.5, 1.6, **1.0**) m for levels 0–3 (equal to the draw range's floor except at level 3, where the range starts at 0.9) |
| Free floor behind the focus rack along the aim, when the level hides pallets | ≥ `MIN_ROOM_BEHIND` = 3.0 m |

If a pose fails, the pose is **redrawn from the same per-frame RNG stream** (aim direction, focus rack, distance, lateral offset, height — five draws per attempt), at most `MAX_POSE_TRIES` = 12 times. If all fail, a **deterministic fallback that consumes no randomness** is used: the first (rack in generation order, direction in table order) combination whose pose at the *far end* of the level's distance range and height range, with zero lateral offset, passes the guard; if even that fails (not observed), the last drawn pose is kept. Pitch, yaw jitter and FOV are drawn once, after the pose is accepted. `test_deterministic_fallback_pose` forces this path by setting `MAX_POSE_TRIES = 0`.

Because the guard is evaluated on the same values that enter the graph, it uses only comparisons, `+ − × ÷` and `sqrt` (§13.5).

**View-cone placement.** Open pallets go in the *corridor* between camera and focus rack (§13.4). Loose boxes and the forklift use three stages: the corridor, then the whole *view cone* (distance 1.8–9 m ahead of the camera, lateral offset within ±0.55 × distance, excluding the focus rack's perspective shadow), then anywhere on the floor. This keeps "visible context" in frame without any extra randomness source.

**Measured effect** (seed 42, real renders, 10 frames, 64 samples): level 3 shows **min 8, mean 12.6 visible objects per frame** (per frame 12, 19, 11, 9, 18, 14, 12, 8, 15, 8), and **48%** of its objects are fully hidden; level 0 shows min 15, mean 19.5, and **17%** hidden. Over 60 frames at level 3 (low-quality renders), 3 frames showed only 4 visible objects, each a tall rack filling the view from 3.5–4.3 m; an extra rack-width guard was tried and rejected because it did not change the two worst frames. The spread within level 3 is real: frame 1 sees its focus rack side-on, so the pallets behind it peek out at the sides and are only partially occluded (visibility 0.2–0.6).

### 13.5 Determinism rules for the generator

No trigonometry and no `numpy` in the generator. Yaws that other placements depend on are exactly 0 or 90, so footprints and stacking offsets are exact swaps/negations; everything else comes from the RNG and `+ − × ÷ √`, which are bit-identical under IEEE-754. Trig appears only in `camera.py`/`projection.py`, whose outputs are never hashed. Every float passes through `canon.quantize` (round to 4 decimals, −0.0 → 0.0, reject NaN/Infinity). Regression vectors (`py/tests/test_scene.py`): `sha256(canonical(generate_scene(42, 0, 0)))` = `77f7f25c1e75ee2ea0a5f66ebb2c9936162a841dd3949ca9db56a61a5396ac4c` and `generate_scene(42, 0, 3)` = `1704f1b7be84042dbb9c53669afeda33938abcd47b81e66e56ea5b738ed153f5`. The level-0 value was **deliberately changed** when §13.4.1 was added (previous value `8b6bfed0…27c7`): loose boxes went from 2–4 to 4–6 and moved into the view cone. Any later generator change must update these vectors on purpose.

## 14. Renderer (`py/porf/render/`)

### 14.1 Architecture

- `runner.py` (system Python, 3.11 venv) generates the scene graphs, writes `render_spec.json`, launches **one Blender process per batch** (`blender -b --factory-startup -noaudio --python-exit-code 1 -P blender_job.py -- --spec render_spec.json`), then builds `annotations.json` from the rendered `seg.png`.
- `blender_job.py` (inside Blender, `bpy` + stdlib only) reads `scene.json` and the spec, builds geometry, renders. It makes **no random choices**.
- **Camera conventions are not re-derived in Blender.** The runner computes the exact Blender camera parameters with `camera.blender_camera_spec` (location, `rotation_euler = (radians(90 − pitch), 0, radians(yaw))`, `sensor_fit = HORIZONTAL`, `angle = radians(fov)`, clip planes, resolution) and writes them into the spec; the job applies them verbatim. Reason: `camera.py` uses package-relative imports and cannot be imported by path from inside Blender, and a second copy of the angle maths could drift. `scene.json` stays the only hashed scene input; `render_spec.json` is derived data and is not hashed. A unit test checks the spec against `Camera.basis()`.
- Object geometry (`blender_job.parts_for`): rack = 4 corner posts + 4 shelf boards + a solid back panel on local +y; pallet = deck + 3 skids; box = one cube; forklift = chassis, counterweight, mast, 4 guard posts, roof, 2 forks (forks toward local +y). All unit-cube parts, parented to an empty at the object's `location` / `yaw_deg`. Every part's `pass_index` is the object id.
- **Seg-only mode (the verifier's re-render):** `spec["mode"] == "seg_only"` runs the same `Stage.build` and the same 1-sample id pass but writes only `seg.png` (no RGB, no depth). `runner.render_seg_only(seed, frames, out_dir, occlusion_level, width, height)` regenerates the scenes and renders them all in ONE Blender process (about 1.5 s of startup plus about 0.1 s per frame). Its output is byte-identical to the `seg.png` of the full render (`test_the_seg_only_rerender_is_byte_identical_to_the_full_renderers_seg_png`, and 200 of 200 calibration frames).
- **Containment assertion:** after building each frame, every part's world-space bound-box corners are transformed into its object's local frame and must satisfy `|axis| ≤ dimension/2 + 1e-4`; otherwise the job raises and exits non-zero (the runner reports the tail of Blender's output). **Test-only hook:** the spec key `test_inflate` (`{object_id, part, extra}`, set via `render_frames(..., test_inflate=...)`) grows one part past its box so a slow test can prove that Blender exits with code 1 and a "leaves its box" message before rendering anything. It is never set in production runs.

### 14.2 Per-frame outputs (`<out>/frame_<i>/`)

| File | Content |
|---|---|
| `scene.json` | canonical JSON of the scene graph; `sha256(file) = scene_hash` |
| `rgb.png` | 8-bit RGB, top-down, Standard view transform; `sha256(file) = image_hash` |
| `depth.png` | 16-bit grayscale (not hashed, not checked), see below |
| `seg.png` | 16-bit grayscale, pixel = object id, 0 = background; `sha256(file) = seg_hash` |
| `annotations.json` | canonical JSON; `sha256(file) = annotation_hash` |

**Depth encoding:** `v = floor((z − near) · 65535 / (far − near) + 0.5)` clamped to [0, 65535], with `z` the distance along the camera axis (Blender's Z pass), `near = 0.1`, `far = 40.0`. Decode: `z = near + v/65535 · (far − near)`; resolution ≈ 0.61 mm. Rays that hit nothing (sky) saturate at 65535 (= `far`).

**Segmentation exactness:** `rgb` and the id/depth data come from two Cycles renders. The auxiliary render uses 1 sample and `filter_width = 0.01`, so each pixel is the single sample at its centre (Cycles importance-samples the pixel filter) and the object-index pass holds one exact id per pixel, never a blend. It is read from float EXR files written by the compositor. The job asserts that **every pixel value is an exact integer that is 0 or a real object id** before writing, and the runner re-asserts it from the PNG.

### 14.3 `annotations.json`

Per object, in id order: `instance_id`, `class`, `class_id` (rack 1, pallet 2, box 3, forklift 4), `bbox` = `[x0, y0, x1, y1]` from the mask's pixel extents (origin top-left, `x1`/`y1` exclusive, same pixel space as §13.1) or `null` if fully hidden, `mask_pixels`, `visibility`. Fully hidden objects stay in the list with `bbox: null`, `visibility: 0.0`.

**Visibility method (cheap, approximate):** `visibility = min(1, mask_pixels / silhouette_area)`, where `silhouette_area` is the pixel area, inside the image, of the convex hull of the object's projected, near-clipped 3D box (`projection.silhouette_area`). It is "how much of the most the object could cover is visible". It under-reports objects that are not solid (pallets seen edge-on, racks seen from the open side) and over-reports nothing: the hull contains every real part, so values are lower bounds on true visibility. Do not read it as an exact fraction. All floats go through `canon.quantize`.

> **Flag for Day 4 calibration (do not tune yet):** visibility is a lower bound, and a loose one for the **non-solid classes, racks and forklifts** (their parts leave gaps inside the box silhouette), and for pallets seen edge-on. The visibility-scaled IoU threshold in check 4 (§7) will therefore need **per-class handling or per-class thresholds**, otherwise honest racks/forklifts will look "less visible than they are" and the IoU floor derived from visibility will be set too low for solid classes or too strict for non-solid ones. Calibrate on honest runs per class.
>
> Measured so far (seed 42, frames 0–4, occlusion 0 and 3, 131 visible objects): the mask box never exceeds the projected full-object box by more than **0.48 px** (mean 0.14–0.19 px), so a pixel tolerance of 2 px has plenty of headroom for the containment half of check 4. The IoU half is the one that needs calibration.

### 14.4 Render settings

Cycles, CPU by default (`--gpu` selects Metal on all non-CPU devices), 640×480, **64 samples** for `rgb.png` with the **OpenImageDenoise** denoiser (default pixel filter 1.5 px, adaptive sampling off, 4 bounces, `seed = 0`, not animated; denoising applies to the RGB render only), Standard view transform, sun + constant world light (`ambient`). The sun is rotated like a camera: `rotation_euler = (radians(90 − elevation), 0, radians(yaw))`, so light travels along that heading. Auxiliary render: 1 sample, 0 bounces, `filter_width = 0.01`, denoising off.

Measured on an Apple-silicon laptop (CPU): about **2.8 s of render work per frame** (RGB 64 samples + OIDN ≈ 2.7 s, aux ≈ 0.05 s, encode ≈ 0.07 s), 3.5 s for a one-frame process including Blender startup (~0.8 s), ≈ 3.2 s/frame for a 5-frame batch.

**Reproducibility:** with identical inputs, `depth.png` and `seg.png` were byte-identical across runs, but `rgb.png` was **not** (denoiser / thread scheduling). This does not affect the protocol, because the node commits to the hash of its own `rgb.png` and the verifier never re-renders (LIMITATIONS.md).

## 15. On-chain program details (`init_config`, `create_request`, `stake_and_claim`, `submit_commitment`)

Code: `programs/proof-of-render/src/`. Anchor 1.1.2, classic SPL Token program only (Token-2022 / `token_interface` is deferred to the Reflect check, if that integration is ever attempted).

### 15.1 Accounts, PDAs and vaults

| Account | Seeds | Notes |
|---|---|---|
| `Config` | `["config"]` | singleton: `admin`, `verifier`, `mint`, `stake_bps`, `bump` |
| `Request` | `["request", requester, request_id_le]` | all fields in §10, plus `challenge_window_end` (0 until `open_challenge`) and `commit_deadline` (0 until claimed) |
| escrow vault | `["escrow", request]` | SPL token account, mint = `config.mint`, **authority = the `Request` PDA** |
| stake vault | `["stake", request]` | same; empty until `stake_and_claim` |
| `Submission` | `["submission", request]` | created by `submit_commitment` (Day 3) |

Both vaults are created by `create_request` (the requester pays their rent). Payouts and refunds sign as the `Request` PDA, using `["request", requester, request_id_le, bump]`. Every token transfer is `transfer_checked`, and every token account is constrained on `mint` and `authority`.

**Closing order, and the decision not to close:** if accounts were closed at a terminal status, the vaults would have to be **emptied and closed first (rent returned to the requester), and only then the `Request` account**, because the `Request` PDA is the vault authority. **Decision (Day 4): nothing is closed.** The terminal `Request` and `Submission` records (`Paid`, `Slashed`, verdict, sample fields) must stay readable for the UI timeline, the verifier report and tests, and closing the `Request` would destroy exactly that. The cost is a little rent per request (LIMITATIONS.md); funds are never at risk. Anyone adding closing later must follow the order above.

### 15.2 Parameter rules (`create_request`)

| Parameter | Rule | Error |
|---|---|---|
| `num_frames` | 1 ..= 1024 | `ZeroFrames`, `TooManyFrames` |
| `price_per_frame` | > 0 | `ZeroPrice` |
| `scene_type` | 0 | `InvalidSceneType` |
| `occlusion_level` | 0 ..= 3 | `InvalidOcclusionLevel` |
| `width`, `height` | 1 ..= 4096 each | `InvalidResolution`, `ResolutionTooLarge` |
| `claim_duration`, `commit_duration`, `challenge_window_secs` | > 0, and `now + claim + commit + window` must not overflow `i64` | `InvalidDuration`, `MathOverflow` |
| `escrow_amount` | `price_per_frame * num_frames`, `checked_mul` | `MathOverflow` |
| `stake_amount` | `escrow_amount * stake_bps / 10_000`, checked, **rounded down**; must be > 0 | `MathOverflow`, `ZeroStake` |
| `Config.stake_bps` | 1 ..= 10_000 (checked in `init_config`) | `InvalidStakeBps` |

The overflow check on the summed durations is made up front so that a later `claimed_at + commit_duration` or `now + challenge_window_secs` can never fail and strand a request.

### 15.3 Deadline boundary semantics

`stake_and_claim` and `submit_commitment` are allowed while `now <= deadline`; `timeout_refund` only when `now > deadline`. The two can therefore never both succeed in the same second. `stake_and_claim` checks the status first (`InvalidStatus`), then the deadline (`ClaimDeadlinePassed`). Any signer may claim, including the requester.

### 15.4 Errors (`errors.rs`, codes start at 6000)

`InvalidStakeBps, ZeroFrames, TooManyFrames, ZeroPrice, InvalidDuration, InvalidOcclusionLevel, InvalidSceneType, InvalidResolution, ResolutionTooLarge, ZeroStake, MathOverflow, WrongMint, InvalidStatus, UnauthorizedVerifier, UnauthorizedNode, ClaimDeadlinePassed, CommitDeadlinePassed, DeadlineNotReached, ChallengeWindowOpen, ChallengeWindowClosed, FrameCountMismatch, ZeroMerkleRoot, SlotHashesUnavailable, SlotHashNotNewer, VerdictAlreadySet, InvalidVerdict, VerdictNotFail, VerdictIsFail, NotImplemented`. Token-account mismatches surface as Anchor's own `ConstraintTokenMint` / `ConstraintTokenOwner` / `ConstraintSeeds`; a `mint` account that is not `config.mint` is `WrongMint`.

### 15.5 Test status

Tests are in `programs/proof-of-render/tests/` (`config.rs`, `create_request.rs`, `stake_and_claim.rs`, `timeout_refund.rs`, shared harness in `common/`). Written for Day 2: every transition of the three implemented instructions with exact balances; init twice; wrong mint (mint account, requester token account, node token account); wrong token owner; double claim; claim after the deadline and exactly at it; claim on each of the seven non-`Open` statuses; each parameter bound, both sides; overflow; zero stake; insufficient balance; wrong stake vault; missing signer; (the stub that returned `NotImplemented` is gone; every instruction is implemented).

**Pending (§11 list):** nothing: the whole list is written; see 15.7 and 15.8.

### 15.6 `submit_commitment(merkle_root: [u8; 32], frame_count: u32)`

Accounts: `node` (signer, mut, pays rent), `request` (mut, PDA-checked), `submission` (`init`, PDA `["submission", request]`), `system_program`. No token accounts: no tokens move on commit.

Checks, in this order: (1) `request.node == node` (`UnauthorizedNode`, evaluated as an account constraint, so it comes first; on an unclaimed request, whose `node` is the default key, no signer can pass); (2) status is `Claimed` (`InvalidStatus`); (3) `now <= commit_deadline` (`CommitDeadlinePassed`; `commit_deadline` is `claimed_at + commit_duration`, §2); (4) `frame_count == request.num_frames` (`FrameCountMismatch`); (5) `merkle_root` is not all zero bytes (`ZeroMerkleRoot`).

Effects: `Submission` records `request`, `node`, `merkle_root`, `frame_count`, `commit_slot = Clock.slot`, `sample_seed = [0; 32]`, `sample_count = 0`, `verdict = Pending`, `bump`; `Request.status = Committed`. `commit_slot` is the slot `open_challenge` later compares against (§5).

**Double commit** fails with the system program's "already in use" error, not `InvalidStatus`: `submission` is created with `init`, and Anchor creates it before the handler runs. The first commitment is therefore immutable, and a node cannot swap its root. The program cannot check that the root is a *correct* Merkle root; that is the verifier's job (§7).

Tests (`tests/submit_commitment.rs`, 15): happy path with every `Submission` field exact (including `commit_slot` and rent paid by the node, not the requester); single-nonzero-byte root accepted; wrong node, requester/verifier/admin, unclaimed request and missing signature rejected; every non-`Claimed` status rejected; commit exactly at the deadline succeeds and one second later fails; deadline counted from the claim, not from creation; frame counts 0, 1, 9, 11, 1024 and `u32::MAX` rejected; zero root rejected; double commit (same and different root) fails and keeps the first root; another request's submission address and a fake system program rejected.

## 16. The node (`py/porf/node/`)

Run: `python -m porf.node.client --keypair keys/node.json [--out out] [--poll 5] [--port 8787] [--samples 64] [--only-request ADDR] [--only-requester PUBKEY] [--cheat MODE]`. The node reaches the chain only through `porf.chain.client`. Frame hashing is defined once, in `porf/records.py` (`hash_frame`, `FrameHashes`), and will be reused by the verifier.

### 16.1 Poll loop

Every `--poll` seconds (default 5) one `list-requests` call, then, in order: (1) advance every local job that is not finished; (2) **adopt** any request that is `Claimed` on chain by this node but has no local job (a crash between the claim transaction and the first write of `job.json`); (3) only if nothing else was done, claim the oldest `Open` request that passes the filters. One job is processed at a time. Filters: `--only-request`, `--only-requester`, `scene_type == 0`, `num_frames <= --max-frames` (1024), and the node's token balance must cover `stake_amount`. A lost race (`InvalidStatus`, `ClaimDeadlinePassed`) is logged and ignored. Before staking the node checks that `<out>/<request_id>/` does not already belong to a different request (`request_id` is only unique per requester); on a collision it refuses, before any stake is spent.

### 16.2 Job directory and state machine

`<out>/<request_id>/` holds `job.json`, `leaves.json`, `render_spec.json`, `blender.log` and the renderer's `frame_<i>/` directories (§14.2). `job.json` is written atomically (temp file + rename). Fields: `request` (address), `requester`, `request_id`, `seed`, `num_frames`, `width`, `height`, `occlusion_level` (all copied from the chain), `state`, `claim_signature`, `merkle_root`, `commit_signature`, `commit_slot`, `committed_at`, `attempts`, `error`, timestamps.

| State | Meaning | Next action on (re)start |
|---|---|---|
| `claimed` | stake paid, nothing rendered | render the whole batch again (a half-finished batch is discarded; there is no per-frame resume), write `leaves.json`, go to `rendered` |
| `rendered` | frames and `leaves.json` on disk, not committed | submit the commitment; **no rendering** |
| `committed` | on-chain `Submission` matches `leaves.json` | nothing; the reveal server serves it |
| `failed` | gave up (`error` says why) | none; never retried automatically |

Recovery cases, each covered by a test: crash after rendering (restart commits without rendering); crash after the commit (restart does nothing and serves); crash after the commit transaction but before `job.json` was updated (the node sees the request already `Committed`, or the second `submit_commitment` fails with "already in use", and reconciles by comparing the on-chain root with `leaves.json`); claim landed but no `job.json` (adopted). A job is marked `failed` when the request is no longer `Claimed` for it, another node holds the claim, the on-chain commitment differs from `leaves.json`, a non-transient chain error occurs (e.g. `CommitDeadlinePassed`), or a render fails three times. RPC errors and timeouts are retried on the next poll. A stale request snapshot never fails a job: the claim is re-read from the chain first.

### 16.3 `leaves.json` (the commitment)

Written once, after rendering and after the cheat hook (§16.6), before the commit. Canonical JSON: `{"frame_count": N, "merkle_root": "<hex>", "frames": [{"frame_index", "scene_hash", "annotation_hash", "image_hash", "seg_hash", "leaf_hash"}, ...]}` (all hashes lowercase hex). The root is computed from the files on disk at that moment; the node does not re-read all frames when serving.

### 16.4 Reveal endpoint

Plain HTTP (default `127.0.0.1:8787`, `--port 0` picks a free port). Only jobs in state `committed` are served.

| Request | Response |
|---|---|
| `GET /frame/<i>` | frame `i` of the most recently committed job |
| `GET /frame/<i>?request=<address>` | frame `i` of that request's job |
| `GET /health` | `{"ok": true, "committed": [<request addresses>]}` |
| anything else; `<i>` not a non-negative integer; `<i> >= frame_count`; unknown or uncommitted `request`; no committed job | **404** `{"error": "..."}` |
| unexpected server failure | 500 `{"error": "..."}` |

A 200 body for `/frame/<i>`:

```json
{
  "request": "<request address>",
  "frame_index": 3,
  "frame_count": 10,
  "merkle_root": "<64 hex>",
  "leaf": {"frame_index": 3, "scene_hash": "<64 hex>", "annotation_hash": "<64 hex>",
           "image_hash": "<64 hex>", "seg_hash": "<64 hex>", "leaf_hash": "<64 hex>"},
  "proof": [{"sibling_hash": "<64 hex>", "sibling_is_left": true}, ...],
  "scene": { ...scene.json as a JSON object... },
  "annotations": { ...annotations.json as a JSON object... },
  "rgb_png": "<base64 of rgb.png>",
  "seg_png": "<base64 of seg.png>",
  "depth_png": "<base64 of depth.png, or null>"
}
```

**Proof encoding:** `proof` is the Merkle proof of §4, leaf level first, one object per level: `sibling_hash` is 64 lowercase hex characters, `sibling_is_left` a JSON boolean. `porf.merkle.proof_to_json` / `proof_from_json` implement it; the decoder rejects anything malformed (wrong length, upper case, non-hex, non-boolean side, extra keys). `scene` and `annotations` are the parsed files; they are canonical JSON on disk, so `hash_json(object)` equals the hash of the file bytes. Everything else is hashed from the decoded base64 bytes.

**What is committed versus what is served:** `leaf`, `proof` and `merkle_root` come from `leaves.json` (what was committed); `scene`, `annotations` and the PNGs are read from disk at request time. If a file was changed after the commit, the node serves it anyway (and logs a warning) and the verifier's hash comparison (§7 check 3) rejects it; the node neither repairs nor hides the difference. To verify a frame: recompute the four hashes from the served content, rebuild the leaf with `frame_index`, and run `merkle.verify_frame` against the **on-chain** root and `frame_count` (never the root in the response).

### 16.5 Finding the node (SHORTCUT)

The reveal URL is not on-chain. The node writes `{"url", "node", "started_at"}` to `.localnet/reveal.json` (`--announce-file`, empty string disables) when its server starts; the verifier will read that file or the `PORF_REVEAL_URL` environment variable (implemented with the verifier, Day 4). Marked `# SHORTCUT:` in `node/client.py`; see LIMITATIONS.md.

### 16.6 Cheat hooks

`node/cheats.py` is test tooling and runs only when the node is started with `--cheat <mode>` (`reuse_frames`, `fake_boxes`, `wrong_seed`, `partial`, `no_occlusion`) and optionally `--fraction F` (default 0.5, valid range (0, 1]; `--fraction` without `--cheat` is rejected). Two hooks: `render_plan(mode, seed, n, fraction) -> [(seed, frames)]` decides what is rendered from which seed (`wrong_seed` renders its bad frames from `wrong_seed_for(seed)`, `partial` renders only the front), and `after_render(mode, job_dir, n, seed, fraction, occlusion, width, height)` rewrites files before the leaves are built, so the committed root covers the lie.

**Frame selection** is `bad_frames(mode, seed, n, fraction)`: a pure function of the mode, the request's seed, the frame count and the fraction (SHA-256 seeded RNG), so a run is reproducible. Count = `ceil(fraction * n)` (so the default corrupts 10 of 20, always at least half), capped at `n-1` for the copying modes (they need a real frame to copy). Bad frames are drawn from the middle frames `1..n-2` first, and from frames 0 and n-1 only when the fraction exceeds the middle; `partial` takes the last `count` frames.

Per mode: `no_occlusion` paints each bad frame's full silhouettes in ascending object-id order (no depth sorting) and rebuilds the annotations from that `seg.png`. `reuse_frames` copies `rgb.png`, `depth.png`, `seg.png`, `annotations.json` of a good frame (cycling through the good frames) over each bad frame and keeps the bad frame's own honest `scene.json`. `fake_boxes` moves, rescales or invents every box of a bad frame (seg stays honest). `wrong_seed` and `partial` are described in the table of section 8. Except for `partial`, which really skips the renders, the cheats render everything honestly and then rewrite, so they save no compute.

### 16.7 Tests (`py/tests/test_node.py`)

No validator needed: frame hashing against the spec and an independent recomputation, leaf cache, reveal payloads verifying for 1, 2, 3, 7 and 8 frames, out-of-range indexes, a file changed after the commit (not hidden; leaf block still shows the committed values), server routes and every 404 case, request selection and latest-commit default, job state machine and collision guard, unreadable job files, cheat hooks do nothing without a mode (the cheat modes themselves: `test_cheats.py`, 50 tests on frame selection, fractions, determinism and what each mode writes). Against a live localnet with real Blender renders: **the gate** (a 10-frame request is picked up, rendered and committed; the on-chain root equals the root recomputed independently from the files on disk; every `/frame/<i>` proof verifies against the on-chain root; the served scene equals `generate_scene` for the on-chain parameters; out-of-range is 404; a second poll does nothing), the four recovery cases above, a claim by another node left alone, and the real `python -m porf.node.client` process end to end.

### 15.7 `open_challenge`, `submit_verdict`, `finalize_payout`, `slash_and_refund`

Accounts. `open_challenge`: `verifier` (signer), `config`, `request` (mut), `submission` (mut), `slot_hashes` (`UncheckedAccount`, `address = SlotHashes::id()`). `submit_verdict(verdict)`: `verifier`, `config`, `request` (mut), `submission` (mut). `finalize_payout`: `caller` (any signer), `request` (mut), `mint` (must equal `request.mint`, else `WrongMint`), `submission`, `escrow`, `stake_vault`, `node_token` (`token::authority = request.node`), `token_program`. `slash_and_refund`: the same, with `requester_token` (`token::authority = request.requester`). The verifier is checked against `Config.verifier` (`UnauthorizedVerifier`). Vaults are PDA-checked and constrained to `mint` with the `Request` PDA as authority.

Effects and check order are in section 5 (`open_challenge`) and section 6 (the other three). `open_challenge` has no deadline of its own: a request that is never challenged stays `Committed` (LIMITATIONS.md).

**Implementation notes.** (1) The token accounts of `finalize_payout` and `slash_and_refund` are `Box`ed: unboxed, the generated account-deserialization function overflowed the 4 KB SBF stack frame ("access violation in stack frame") on a real run. (2) The SlotHashes parser (`slot_hashes.rs`) is a pure function with native unit tests for empty, truncated and lying (huge count) data; LiteSVM cannot inject malformed sysvar data, so those cases are covered there, not in the integration tests. (3) SHA-256 comes from `solana-sha256-hasher =3.1.0` (the only direct hashing dependency; `solana_program` no longer re-exports one). (4) `submit_verdict` rejects `Pending` as an argument (`InvalidVerdict`).

**Tests** (LiteSVM, SlotHashes injected by writing the sysvar account; 96 program tests in total):
- `open_challenge.rs` (11): exact fields including the seed against a hashlib-computed vector; only the newest entry matters; `sample_count` for N = 1, 2, 5, 6, 7, 8, 20, 1024; stale slot hashes (below, equal, one newer) and recovery; empty sysvar; wrong sysvar account; wrong signer and missing signature; every non-`Committed` status; never-committed request; second call changes nothing; another request's submission.
- `submit_verdict.rs` (9): `Pass` gives `Verified`, `Fail` leaves `Challenged`; unauthorized signers; every wrong status; `Pending` argument; second verdict after each outcome; the window boundary (`window_end - 1` succeeds, `window_end` and later fail); documented check order.
- `finalize_payout.rs` (12): `Pass` then payout, and `Pending` then payout (at `window_end`, +1, +1,000,000 s) with exact balances; one second early fails for `Verified` and `Pending`; payout on `Fail`; double payout; payout after a slash; every wrong status; wrong destinations (the requester's, a stranger's, a vault, the wrong mint); wrong vaults and wrong mint account; never-committed; donations left alone; missing signature.
- `slash_and_refund.rs` (12): slash inside, at and long after the window with exact balances; slash on `Pass` / `Pending` / never-challenged; double slash; slash after each kind of payout; wrong status; wrong destinations, vaults and mint; donations left alone; missing signature; both terminal outcomes side by side with token conservation.

**Terminal balances** (requester / node, each starting from `S`; escrow `E`, stake `K`): `Paid`: `S - E` / `S + E`; `Slashed`: `S + K` / `S - K`; both vaults 0 in both cases.

**Live tests** (`py/tests/test_challenge_flow.py`, real validator and CLI, 3-second windows): the honest flow (open with bounded retry, independent audit of the seed from the real SlotHashes account, `Pass`, early payout refused, payout after the window, exact balances, double payout, slash on `Pass`); the tampered flow (`Fail`, unauthorized verdicts, overturning refused, payout refused, immediate slash, exact balances, double slash); optimistic payout on `Pending`, with the verifier too late and no verdict invented; a second `open_challenge` and a misrouted payout rejected. Error codes are compared with the IDL's. Chain-client unit tests cover the retry: it retries only `SlotHashNotNewer`, stops after the bound, and raises a clear message.

### 15.8 `timeout_refund`

Accounts: `caller` (signer, anyone), `request`, `mint`, `escrow`, `stake_vault`, `requester_token` (constrained to the requester's own token account of the request's mint), `token_program`. Logic, in this order: the status must be `Open` or `Claimed` (anything else, including `Expired`, is `InvalidStatus`, so a second refund fails), then `now > claim_deadline` (Open) or `now > commit_deadline` (Claimed), otherwise `DeadlineNotReached`. `Open`: only the escrow moves, because no stake was ever deposited (the recorded `stake_amount` is not transferred). `Claimed`: the escrow and the node's stake both go to the requester. The status becomes `Expired`. The comparison is strict, mirroring `stake_and_claim` and `submit_commitment`, which are allowed while `now <= deadline`. Donations to a vault are not swept. A request that is `Committed` or later is never refundable (the trusted-verifier assumption covers a verifier that never opens a challenge; LIMITATIONS.md).

Tests: `programs/proof-of-render/tests/timeout_refund.rs` (14, mutation-checked: `>=` at either deadline, escrow-only on a claimed request, a status guard removed, a stake transfer on an open request) and live `py/tests/test_timeout_flow.py` (3, real CLI, short real-time deadlines, exact balances). CLI command `timeout-refund` (`{"signer", "request" | "requester"+"request_id", "requester_token"?, "mint"?}`), wrapper `ChainClient.timeout_refund`.

## 17. The verifier (`py/porf/verifier/`)

Run: `python -m porf.verifier.client --keypair keys/verifier.json [--out out/verifier] [--poll 2] [--only-request ADDR] [--reveal-url URL] [--once]`. Chain access only through `porf.chain.client`. Hashing and proofs come from `porf.records` and `porf.merkle`, the same code the node uses. **The verifier host needs Blender** (check 7); the constructor refuses to start without it, before any challenge is opened.

### 17.1 Loop

Each poll, for every request (optionally `--only-request`):

| Request status | Action |
|---|---|
| `Committed` | `open_challenge` (retrying only `SlotHashNotNewer`: 20 attempts, 0.4 s apart, then a clear error), then verify immediately |
| `Challenged`, verdict `Pending` | if the window is still open: derive the sample from the on-chain seed, fetch the frames, **re-render the reference seg for the fetched frames (one Blender process)**, run the checks, **write the report, then post the verdict**; if the window already closed, leave it to the optimistic payout |
| `Challenged`, verdict `Fail` | `slash_and_refund` at once |
| `Verified` (verdict `Pass`) | wait for `challenge_window_end` (plus 0.5 s), then `finalize_payout`; a `ChallengeWindowOpen` answer (the chain's clock trails ours) is "waiting", not an error |

The sample is `sampler.sample_indices(bytes.fromhex(sample_seed), frame_count)` (section 5); its length must equal the on-chain `sample_count`, otherwise the verifier stops with an error instead of posting a verdict. The node's reveal URL comes from `PORF_REVEAL_URL`, `--reveal-url`, or `.localnet/reveal.json` (`# SHORTCUT:`, LIMITATIONS.md). Frames are fetched in ascending order. **The first unavailable reveal ends the fetching:** the verdict is already `Fail`, and three 10 s timeouts per remaining frame could outlast the challenge window; the remaining frames are recorded as `not_checked`. If no reveal arrived at all (an offline node) nothing is re-rendered. **The verifier must finish before `challenge_window_end`**: a verdict that arrives late is rejected by the program and the node is then paid optimistically (LIMITATIONS.md). The run keeps one in-memory set of image hashes across all requests it verifies. An unreachable chain is logged and retried on the next poll; it never stops the verifier.

**If the seg re-render fails** (Blender crash, missing output), it is retried once in a fresh Blender process. If that fails too, the verifier posts **no verdict** (neither `Pass`, which would hide fraud, nor `Fail`, which would slash a node for the verifier's own fault), records `verification_error` in the report, and tries again on the next poll while the window is open. If the window closes first, the node is paid optimistically (LIMITATIONS.md).

### 17.2 The seven checks (`checks.py`)

Each returns `{name, passed, detail}` (plus `metrics` for calibration), runs on every sampled frame independently, and never raises: a malformed reveal fails checks, and a check that crashes counts as failed. Names: `merkle_proof`, `scene_match`, `hash_consistency`, `annotation_plausibility`, `image_sanity`, `segmentation_consistency`, `seg_rerender`.

1. **`merkle_proof`**: the reveal's `frame_index` and the leaf's `frame_index` equal the requested index; `leaf_hash` equals the hash of the leaf fields; a root stated in the reveal equals the chain's; and `merkle.verify_frame` accepts the proof against the **on-chain** root and **on-chain** `frame_count` (never the node's), which also enforces the position and length rules of section 4.
2. **`scene_match`**: `generate_scene(seed, index, occlusion_level, width, height)` from the **on-chain request** is hashed; the committed `scene_hash` and the hash of the revealed `scene.json` must both equal it. On a mismatch the detail names the first differing JSON path.
3. **`hash_consistency`**: `porf.records.hash_frame` of the four revealed files must equal the committed leaf fields; the detail names exactly the files that differ.
4. **`annotation_plausibility`**: uses the regenerated scene and the pixel counts from the committed `seg.png`. Every scene object must be annotated exactly once with its class. For each box: it must be non-empty and inside the image; the object's projection must be `ok` (a visible box for an off-screen or behind-camera object fails); the mask box must lie inside the projected full-object box within the class's pixel tolerance; and `IoU(mask box, projected box grown by the tolerance) >= min_iou * mask_pixels / area(grown box)`. That last form is derived, not guessed: the visible part of a mostly hidden object can be a one-pixel sliver that sticks out of the exact projected box by under a pixel, so its IoU with the exact box is tiny; against the grown box, honest data satisfies the inequality by construction for any `min_iou <= 1`. There is no visibility floor. Thresholds live in `Thresholds` (per class, plus `seg_rerender`, with a `calibrated` flag that is `True` for `DEFAULT_THRESHOLDS`): **1.0 px tolerance and `min_iou` 0.30 for all four classes** (section 17.5).
5. **`image_sanity`**: `rgb.png` and `seg.png` are valid PNGs of the requested resolution (`seg.png` 16-bit); `rgb.png` is not a single flat colour; and `rgb.png` is not byte-identical to an image seen earlier in this run in a *different* frame or request (re-verifying the same frame is not a duplicate).
6. **`segmentation_consistency`**: see section 7 item 6. `analyze()` in `porf/seg.py` gives per-id pixel count and mask extents; annotations must match exactly, visibility within 1e-3.

7. **`seg_rerender`**: the reference is `runner.render_seg_only` for the sampled frames, produced by the verifier from the on-chain request (seed, occlusion level, resolution), never from anything the node sent. `segcompare.compare_seg` measures the global pixel agreement and, per object, `|A and B| / |A or B|` of the two masks. Pass requires agreement `>= 0.999` and, for every object whose larger mask has at least 50 pixels, an IoU `>= 0.90` (an object present in only one image has IoU 0; the pixel floor stops a flipped one-pixel sliver from being judged as fraud). The detail names the first failing objects; `metrics` carry the agreement, the number of differing pixels and the worst object.

**What the IoU term of check 4 can and cannot catch (honest assessment).** Because annotations must equal what `seg.png` yields (check 6) and the mask box must lie inside the projected box (check 4), a node whose `seg.png` and boxes are consistent with each other cannot fail the IoU term: the mask box encloses its own pixels. The detectors with teeth are the containment test, the regenerated scene, the proof and hashes, the seg/annotation equality, and, for occlusion, check 7. The IoU term stays as a weak sanity floor (`min_iou` 0.30).

### 17.3 Report (`out/verifier/<request address>/report.json` and `report.txt`)

`report.json`: `request`, `request_id`, `requester`, `node`, `num_frames`, `frame_count`, `merkle_root`, `reveal_url`; `sample` = `{seed, slot, count, indices}`; `frames` = a list of `{index, status ("checked" | "reveal_unavailable" | "not_checked"), passed, checks: [{name, passed, detail, metrics?}], reason?}`; `verdict` (`Pass` or `Fail`) and `verdict_reasons` (one string per failed check or unavailable reveal; an unavailable reveal is never phrased as a check failure); `reference_render` = `{attempts, frames, seconds}` (0 attempts when nothing was fetched); `timing` = seconds per stage `{open_challenge_s, fetch_s, rerender_s, checks_s, verdict_s, total_s}`; `thresholds` (`calibrated`, `visibility_tolerance`, per-class values, `seg_rerender` values); `verification_error` (only when the re-render failed twice and no verdict was posted); `transactions` = signatures of `open_challenge`, `submit_verdict` and `finalize_payout` or `slash_and_refund`; `outcome` = `{action ("paid" | "slashed"), to, destination, escrow_moved, stake_moved, destination_delta, final_status}`, measured from the vault and destination balances before and after the transaction. The report is written **before** the verdict transaction (so a failed transaction still leaves the findings) and updated after each later step. `report.txt` is a short human summary of the same data, including a timing line, and a note if the thresholds are uncalibrated.

### 17.4 Tests

`test_sampler.py` (22): every row of the section 5 table for several seeds, hardcoded index vectors, an independent reference implementation, the big-endian seeding pinned against little-endian, input validation. `test_verifier_checks.py` (52): an honest Blender-free node passes all seven checks on every frame across seeds and levels; each check against hand-tampered reveals (flipped proof byte and sides, wrong lengths, malformed proofs, wrong index, relabelled leaf, wrong or foreign root and frame count; a hash-consistent wrong seed, a scene swapped after the commit; each file changed after the commit; shifted, enlarged and tiny boxes, wrong class, missing, extra and duplicate objects, a box for an off-screen object, per-class thresholds; wrong resolution, garbage, JPEG, flat images, duplicates within the sample and from earlier in the run; stray ids and annotations disagreeing with `seg.png`); for check 7: the exact match, a missing or wrong-sized reference, a few flipped pixels tolerated, a shifted or erased object, scattered noise, the pixel floor, configurable thresholds, `compare_seg`; the approved threshold values; uncalibrated thresholds labelled; and the `no_occlusion` cheat code applied to files on disk, which passes checks 1 to 6 and fails check 7 in at least 6 of 8 frames. `test_verifier_client.py` (37): reveal fetching against a real local HTTP server, URL discovery, verdict aggregation, reports, the state machine against a fake chain, and the re-render inside the verifier: one Blender call for the whole sample, one retry in a fresh process, no verdict after two failures, no re-render for an offline node, the Blender preflight, the timing section, the `no_occlusion` node failed with only `seg_rerender` failing. `test_render.py` adds two Blender tests for the seg-only render (byte-identical to the full renderer's `seg.png`; honours the resolution). **Live gate, `test_verifier_flow.py` (5; real chain, real Blender, calibrated thresholds, the 60 s default window):** an honest 20-frame request at occlusion 3 with production render settings is verified and paid (the whole verification takes well under half the window); 20 frames with tampered boxes committed consistently are slashed (checks 4 and 6 fail, 1 to 3 and 7 pass); 20 frames from a node run with `--cheat no_occlusion` are slashed, every sampled frame passes checks 1 to 6, and only check 7 fails; a file changed after the commit is slashed; an offline node is slashed as "reveal unavailable".

### 17.5 Calibration record (approved)

Reproduce with `py/.venv/bin/python scripts/calibrate.py --phases render,seg,checks,cheats` (and `--phases timing`, `--phases gpu`); raw data goes to `out/calibration/*.json`. Environment: Blender 4.5.14 LTS, macOS 26.6.2 arm64, CPU Cycles. **One machine only.**

**Honest data:** 5 seeds (42, 7, 99, 2024, 31337) x occlusion levels 0 to 3 x 10 frames = 200 frames at production settings (640x480, 64 samples), 3,384 objects. Honest rendering took 56 to 72 s per 10 frames (6.4 s per frame).

*Check 7 on honest frames:* `seg.png` byte-identical to the re-render in **200 of 200** frames; global agreement **1.000000** in every frame at every level; per-object IoU **1.000000** for all 3,384 objects (including objects of 1 to 20 pixels). 75 honest objects have fewer than 50 pixels (24 fewer than 20); the median honest object has 2,464 pixels. All seven checks run on all 200 real frames with the then-placeholder thresholds: 0 failures.

*Check 4 on honest frames (mask-box overshoot beyond the projected box, px):*

| Class | n | max | p99 | p95 | slack vs 2.0 px (min / p1 / p5) | IoU / visible fraction (min / p1 / p5) |
|---|---|---|---|---|---|---|
| box | 2048 | 0.499 | 0.486 | 0.459 | 1.501 / 1.514 / 1.541 | 1.000 / 1.000 / 1.050 |
| pallet | 853 | 0.490 | 0.479 | 0.454 | 1.510 / 1.521 / 1.546 | 1.000 / 1.033 / 1.144 |
| rack | 456 | 0.491 | 0.485 | 0.452 | 1.509 / 1.515 / 1.548 | 1.000 / 1.036 / 1.078 |
| forklift | 27 | 0.172 | 0.160 | 0.119 | 1.828 / 1.840 / 1.881 | 1.410 / 1.411 / 1.460 |

**Approved thresholds:** check 7: `min_global_agreement 0.999`, `min_object_iou 0.90`, `min_object_pixels 50` (honest data is exactly 1.0 here; 0.999 tolerates about 300 flipped pixels of 307,200 on another machine). Check 4: `pixel_tolerance 1.0` for all four classes (2x the worst honest 0.499 px; one shared value because the classes behave identically; the forklift sample is small), `min_iou 0.30` (honest IoU / visible fraction is at least 1.0 by construction). Calibrated flag `True`.

**Fraud against the true re-render** (50 frames per level; agreement min / median / max; median worst IoU of objects with at least 50 px):

| Level | no_occlusion | depth-sorted painter |
|---|---|---|
| 0 | 0.9135 / 0.9715 / 0.9944; 0.418 | 0.9399 / 0.9775 / 0.9944; 0.585 |
| 1 | 0.8993 / 0.9515 / 0.9888; 0.000 | 0.8929 / 0.9669 / 0.9917; 0.381 |
| 2 | 0.8353 / 0.9368 / 0.9691; 0.000 | 0.8475 / 0.9626 / 0.9966; 0.000 |
| 3 | 0.8471 / 0.9332 / 0.9646; 0.000 | 0.9051 / 0.9620 / 0.9978; 0.316 |

Frames caught out of 50 at levels 0, 1, 2, 3, with 0 of 200 honest frames rejected by every rule shown:

| Rule (G, J, P) | no_occlusion | painter |
|---|---|---|
| 0.99, 0.5, 50 (the old placeholder) | 47, 50, 50, 50 | **43, 48, 49, 41** |
| 0.995, 0.5, 50 | 50, 50, 50, 50 | 50, 50, 49, 45 |
| **0.999, 0.90, 50 (approved)** | **50, 50, 50, 50** | **50, 50, 50, 50** |

The painter, a node doing cheap depth sorting, **evades the old placeholder rule** in up to 9 of 50 frames at level 3 (its hardest frames agree 99.78% with the true render, worst-object IoU 0.98). It is caught in all 200 frames by the approved rule only because honest data on this machine is bit-exact; see LIMITATIONS.md.

**Metal GPU versus the CPU re-render** (same machine, 12 frames over 4 levels): 0 pixels differ. A hint that the id pass is robust to the compute device, not evidence about other machines.

### 17.6 Challenge window

Verification of a 20-frame request (the sample is 7 frames, so cost does not grow with N above 7), 10 trials each, all `Pass`:

| Condition | open | fetch | re-render | checks | verdict | **total (min / mean / max)** |
|---|---|---|---|---|---|---|
| idle | 0.57 | 0.03 | 2.32 | 0.19 | 0.66 | 4.58 / 4.66 / **4.73 s** |
| under CPU load (a concurrent 64-sample render) | 3.81 | 0.05 | 4.27 | 0.29 | 2.63 | 8.52 / 12.71 / **16.93 s** |

Under load the chain calls vary most (0.9 s to 5.8 s each), more than the re-render (3.9 to 4.6 s). The rule is `max(30 s, 3 x worst loaded total)`: 3 x 16.9 = 51 s. **The demo default is 60 s (`DEFAULT_CHALLENGE_WINDOW_SECS`), rounded up from 51 s because screen recording during the demo adds CPU load** (decision by the project owner). Tests that need a short window pass one explicitly.

### 17.7 Report images and the report server

Presentation only: nothing here is read by a check or by the verdict. After the checks ran, the verifier writes, for each sampled frame whose reveal arrived, into `<report dir>/frames/<index>/` (`artifacts.py`, from arrays and boxes it already holds):
- `rgb.png`: the revealed picture, full size (as served, so a fraudulent picture is shown as it was). `overlay.png`: the same with the node's boxes (solid green; solid red only for a box that fails a check), the boxes the scene predicts for each visible object (dashed grey), and projected objects the node left unlabelled (dashed, labelled "hidden"). A box counts as failing when, per object, it is empty or outside the image, leaves its projected box by more than the tolerance (check 4), differs from the extents of the node's own `seg.png` (check 6), or its mask disagrees with the re-render below the IoU floor (check 7). This restates the checks for colouring only; the verdict never reads it.
- Only for a frame that fails check 7: `seg_node.png` (the node's `seg.png` in false colour), `seg_reference.png` (the verifier's re-render, same colours; each object id maps to a colour from a hash of the id alone, background is graphite) and `seg_diff.png` (a dim copy of the re-render with every differing pixel red).

`report.json` gains `frames[].images` = `{name: path relative to the report directory}` for the files that were written.

`python -m porf.verifier.serve [--out out/verifier] [--port 8788]` (or `porf.verifier.client --serve [PORT]`) serves that directory read-only over HTTP with CORS open: `GET /` lists the requests that have a report, `GET /<request>/report.json`, `GET /<request>/frames/<i>/<name>.png`. Only those paths are ever served, and each request reads the files on disk, so the server is stateless: it works the same when the verifier is restarted, is not running, or the report is old. The reports hold nothing that is not already public (the chain, and the node's reveal endpoint).

## 18. Demo and reliability

**Demo request:** 20 frames, occlusion level 3, 640x480, price 1 USDC per frame (escrow 20, stake 4), **seed 5150**, challenge window 60 s. The seed was chosen by seg-screening ten candidates (every level-3 frame needs at least 7 visible objects and at least 2 partly occluded ones; 1337 and 5150 and 31 passed best) and then rendering the top three at production settings and looking at all 20 frames of each. 1337 was dropped (four frames are almost entirely a rack wall), 31 was dropped (frame 11 shows the edge of the floor and the void beyond it), **5150 was chosen**: varied camera positions, a forklift in two frames, racks seen through, pallets and boxes in front of and behind shelving. Overlays of all 20 frames (green = the node's mask box, red = the projected full-object box, so a red box with no green one is a fully hidden object): `out/demo_seed/seed5150_overlay.png` (contact sheet) and `out/demo_seed/overlay_frame_<i>.png`; the plain renders are `out/demo_seed/seed5150_rgb.png`.

**Commands** (each one command, numbered output, no stack trace on the happy path; the work is done by `python -m porf.demo`): `scripts/setup.sh [--cluster localnet|devnet]`, `scripts/demo_honest.sh`, `scripts/demo_cheat.sh --mode <mode> [--fraction F]` (**the demo default is `--fraction 0.8`**, so the cheat is effectively always sampled: with 16 bad frames the detection probability is 1.000 (a sample of 7 cannot avoid them); the protocol and the node's own default stay 0.5), `scripts/demo_timeout.sh` (two refunds with 8 s deadlines and a refused early and double refund). Each run's logs and report are in `out/demo/<name>-<time>/`.

**Reliability (10 honest runs, fresh localnet, production render settings, real Blender verification):** seeds 42, 7, 99, 2024, 31337, 11, 23, 59, 101, 777 at occlusion levels 0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 20 frames each. **0 false positives: all ten were `Pass` on all seven checks and paid exactly the escrow.** Verification time (open challenge to verdict posted): 4.75 s to **5.60 s (slowest)**, of which the seg re-render was 2.3 to 2.6 s; the 60 s window leaves a factor of 10. Reproduce: `PORF_HONEST_REPEAT=1 py/.venv/bin/pytest -q py/tests/test_honest_repeat.py -s` (about 36 minutes; writes `out/honest_repeat.json`).
