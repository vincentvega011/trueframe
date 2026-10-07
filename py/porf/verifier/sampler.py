"""Which frames the verifier checks. A pure function of the on-chain sample seed and the frame count.

Rule (docs/PROTOCOL.md section 5):
  * N >= 7: frames 0 and N-1, plus 5 unique draws from 1..N-2          -> 7 indices
  * N <  7: min(5, N) unique draws from 0..N-1
The RNG is seeded explicitly: random.Random(int.from_bytes(sample_seed, "big")), never by passing the bytes to
Random (which would hash them with a different, version-dependent scheme). Each draw is rng.randrange(lo, hi + 1);
duplicates are rejected and redrawn until the count is reached. The result is sorted.
"""
from __future__ import annotations

import random

SEED_LEN = 32
EDGE_THRESHOLD = 7   # N at which frames 0 and N-1 are always included
DRAWN = 5            # unique random draws


def expected_sample_count(frame_count: int) -> int:
    """The number of sampled frames the on-chain program records as `sample_count` (PROTOCOL.md section 5 table)."""
    if frame_count < 1:
        raise ValueError("frame_count must be at least 1")
    return DRAWN + 2 if frame_count >= EDGE_THRESHOLD else min(DRAWN, frame_count)


def _draw_unique(rng: random.Random, lo: int, hi: int, count: int) -> set[int]:
    """`count` unique integers in [lo, hi], by rejection sampling. `count` must not exceed hi - lo + 1."""
    chosen: set[int] = set()
    while len(chosen) < count:
        chosen.add(rng.randrange(lo, hi + 1))
    return chosen


def sample_indices(sample_seed: bytes, frame_count: int) -> list[int]:
    if len(sample_seed) != SEED_LEN:
        raise ValueError(f"sample_seed must be {SEED_LEN} bytes, got {len(sample_seed)}")
    if frame_count < 1:
        raise ValueError("frame_count must be at least 1")
    rng = random.Random(int.from_bytes(sample_seed, "big"))
    if frame_count >= EDGE_THRESHOLD:
        indices = {0, frame_count - 1} | _draw_unique(rng, 1, frame_count - 2, DRAWN)
    else:
        indices = _draw_unique(rng, 0, frame_count - 1, min(DRAWN, frame_count))
    return sorted(indices)
