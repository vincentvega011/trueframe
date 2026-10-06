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

**Closing order (for Day 4+):** at a terminal status the vaults must be **emptied and closed first (rent returned to the requester), and only then may the `Request` account be closed**, because the `Request` PDA is the vault authority and a vault cannot be closed without it. If closing proves complicated, skip closing entirely and record that in LIMITATIONS.md; funds are never at risk either way.

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

`InvalidStakeBps, ZeroFrames, TooManyFrames, ZeroPrice, InvalidDuration, InvalidOcclusionLevel, InvalidSceneType, InvalidResolution, ResolutionTooLarge, ZeroStake, MathOverflow, WrongMint, InvalidStatus, UnauthorizedVerifier, UnauthorizedNode, ClaimDeadlinePassed, CommitDeadlinePassed, DeadlineNotReached, ChallengeWindowOpen, ChallengeWindowClosed, FrameCountMismatch, ZeroMerkleRoot, SlotHashesUnavailable, SlotHashNotNewer, VerdictAlreadySet, VerdictNotFail, VerdictIsFail, NotImplemented`. Token-account mismatches surface as Anchor's own `ConstraintTokenMint` / `ConstraintTokenOwner` / `ConstraintSeeds`; a `mint` account that is not `config.mint` is `WrongMint`.

### 15.5 Test status

Tests are in `programs/proof-of-render/tests/` (`config.rs`, `create_request.rs`, `stake_and_claim.rs`, `stubs.rs`, shared harness in `common/`). Written for Day 2: every transition of the three implemented instructions with exact balances; init twice; wrong mint (mint account, requester token account, node token account); wrong token owner; double claim; claim after the deadline and exactly at it; claim on each of the seven non-`Open` statuses; each parameter bound, both sides; overflow; zero stake; insufficient balance; wrong stake vault; missing signer; and a check that the six unimplemented instructions return `NotImplemented`.

**Pending (§11 list), by day:** Day 4: `open_challenge` with SlotHashes, verdict after the window must fail, payout on `Pending` after the window must succeed, double payout, payout before the window ends, slash on `Pass`, payout on `Fail`, wrong mint on payouts/refunds. Day 5: `timeout_refund` (deadline not reached; claim without commit sends the stake to the requester; no claim).

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

`node/cheats.py` exposes `before_render(mode, seed) -> seed` and `after_render(mode, job_dir, num_frames)`, called around the render and before the leaves are built, so a cheat's changes are what gets committed. Both are no-ops today: `--cheat <mode>` is accepted (`reuse_frames`, `fake_boxes`, `wrong_seed`, `partial`), logs a warning that the node is behaving honestly, and does nothing else. Day 5 implements them.

### 16.7 Tests (`py/tests/test_node.py`)

No validator needed: frame hashing against the spec and an independent recomputation, leaf cache, reveal payloads verifying for 1, 2, 3, 7 and 8 frames, out-of-range indexes, a file changed after the commit (not hidden; leaf block still shows the committed values), server routes and every 404 case, request selection and latest-commit default, job state machine and collision guard, unreadable job files, cheat hooks are no-ops. Against a live localnet with real Blender renders: **the gate** (a 10-frame request is picked up, rendered and committed; the on-chain root equals the root recomputed independently from the files on disk; every `/frame/<i>` proof verifies against the on-chain root; the served scene equals `generate_scene` for the on-chain parameters; out-of-range is 404; a second poll does nothing), the four recovery cases above, a claim by another node left alone, and the real `python -m porf.node.client` process end to end.
