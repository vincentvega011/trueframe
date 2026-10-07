import random

import pytest

from porf.verifier.sampler import expected_sample_count, sample_indices

# sha256(ROOT || 0xAB*32): the seed the on-chain open_challenge test produces (programs/.../tests/common/mod.rs)
ONCHAIN_SEED = bytes.fromhex("a77fb2584ba6b48a1f11e25e0b05b6cade9c11f2f7adfade76e5766edd32f4d0")
OTHER_SEED = bytes(range(32))

# docs/PROTOCOL.md section 5 table: N -> sample_count
TABLE = {1: 1, 2: 2, 5: 5, 6: 5, 7: 7, 8: 7, 20: 7, 1024: 7}

# Hardcoded vectors (random.Random(int.from_bytes(seed, "big")), rejection sampling, sorted)
VECTORS_ONCHAIN_SEED = {
    1: [0],
    2: [0, 1],
    5: [0, 1, 2, 3, 4],
    6: [0, 1, 2, 3, 5],
    7: [0, 1, 2, 3, 4, 5, 6],
    8: [0, 1, 2, 3, 4, 6, 7],
    20: [0, 4, 5, 6, 7, 13, 19],
    1024: [0, 151, 176, 392, 705, 734, 1023],
}
VECTORS_OTHER_SEED = {20: [0, 4, 7, 12, 16, 17, 19]}


@pytest.mark.parametrize("n,count", sorted(TABLE.items()))
def test_sampler_returns_exactly_the_onchain_sample_count_for_every_table_row(n, count):
    """The required cross-check: the Python sampler and the program's `sample_count_for` agree for each N in the table."""
    assert expected_sample_count(n) == count
    for seed in (ONCHAIN_SEED, OTHER_SEED, bytes(32), b"\xff" * 32):
        idx = sample_indices(seed, n)
        assert len(idx) == count, (n, seed.hex())
        assert len(set(idx)) == len(idx)                       # unique
        assert idx == sorted(idx)                              # sorted
        assert all(0 <= i < n for i in idx)                    # in range
        if n >= 7:
            assert idx[0] == 0 and idx[-1] == n - 1            # both edges always sampled
        if n <= 5:
            assert idx == list(range(n))                       # everything is sampled when there are at most 5 frames


def test_the_whole_range_of_small_n_matches_the_rule():
    for n in range(1, 60):
        assert len(sample_indices(ONCHAIN_SEED, n)) == (7 if n >= 7 else min(5, n))


@pytest.mark.parametrize("n,expected", sorted(VECTORS_ONCHAIN_SEED.items()))
def test_hardcoded_index_vectors(n, expected):
    assert sample_indices(ONCHAIN_SEED, n) == expected


def test_hardcoded_vector_for_a_second_seed():
    assert sample_indices(OTHER_SEED, 20) == VECTORS_OTHER_SEED[20]


def reference(seed: bytes, n: int) -> list[int]:
    """A second, deliberately naive implementation of the PROTOCOL.md rule."""
    rng = random.Random(int.from_bytes(seed, "big"))
    lo, hi, want, fixed = (1, n - 2, 5, [0, n - 1]) if n >= 7 else (0, n - 1, min(5, n), [])
    picked: list[int] = []
    while len(picked) < want:
        v = rng.randrange(lo, hi + 1)
        if v not in picked:
            picked.append(v)
    return sorted(set(picked + fixed))


def test_matches_an_independent_reference_implementation():
    for seed in (ONCHAIN_SEED, OTHER_SEED, bytes(range(1, 33))):
        for n in (1, 2, 3, 5, 6, 7, 8, 9, 20, 100, 1024):
            assert sample_indices(seed, n) == reference(seed, n)


def test_the_rng_is_seeded_big_endian_from_an_integer():
    """Pinned: int.from_bytes(seed, "big"). Little-endian (or passing the bytes) would give different samples."""
    seed = OTHER_SEED
    assert sample_indices(seed, 1024) == reference(seed, 1024)
    little = random.Random(int.from_bytes(seed, "little"))
    as_little = sorted({0, 1023} | {little.randrange(1, 1023) for _ in range(5)})
    assert as_little != sample_indices(seed, 1024)   # the two conventions really differ on this seed


def test_deterministic_and_seed_sensitive():
    assert sample_indices(ONCHAIN_SEED, 1024) == sample_indices(ONCHAIN_SEED, 1024)
    assert sample_indices(ONCHAIN_SEED, 1024) != sample_indices(OTHER_SEED, 1024)
    assert len({tuple(sample_indices(bytes([i]) * 32, 1024)) for i in range(40)}) > 30


def test_rejects_bad_input():
    for bad in (b"", b"short", bytes(31), bytes(33)):
        with pytest.raises(ValueError):
            sample_indices(bad, 10)
    for n in (0, -1):
        with pytest.raises(ValueError):
            sample_indices(ONCHAIN_SEED, n)
        with pytest.raises(ValueError):
            expected_sample_count(n)
