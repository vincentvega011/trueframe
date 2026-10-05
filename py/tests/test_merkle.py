"""Merkle test vectors. Expected values were produced by a separate, minimal
reference implementation (hashlib only), not by porf.merkle, so any encoding change
here must break a test.

Test fields for frame i: scene/ann/img/seg hash = sha256(b"scene%d" / b"ann%d" / b"img%d" / b"seg%d" % i).
"""
import hashlib

import pytest

from porf.merkle import (
    build_levels,
    build_proof,
    leaf_hash,
    merkle_root,
    node_hash,
    proof_length,
    verify_frame,
    verify_proof,
)

LEAVES_HEX = [
    "696717fc55e3f9632ebcc11084a2e989f8f19bfb2d2c797035b7e78a981ab1cf",
    "2229f07e9e592888c92a98f81b47ac2bb71335d1eddb1b86c4c45d6308cb38ef",
    "a561191f15d7ea54219b40abe23bb5189f66db32bc920e63a80c38038416771b",
    "0008a65d034d8d85b69d3e95142c89b356887f66cfc349a4827a0c239d468c6b",
    "94c62925b47754537981fda75456ffdca64918fdf1ab61704a5d7001c9b8a670",
    "ce71731a1b38a6cbeeccca94353cb7a3244412dd6f205d585efb44c835e8e00d",
    "e507c0d6a2b9f35ec71414c6da8404975e47d2d8d73f36de5180569796fa06c9",
    "0ee687abd754804bb925c163b78c3d66cc836ddc632ad2ce721345edb41e28f2",
]
ROOTS_HEX = {
    1: "696717fc55e3f9632ebcc11084a2e989f8f19bfb2d2c797035b7e78a981ab1cf",
    2: "cef98881d20c10b19d32e951fbdbba45439abc1259684d50bffc71b9715ab2d1",
    3: "831dc8a7f17dc9262b50dd29d5547d27b5b29479bfabfd53e55684b9d1832f08",
    7: "0ac6ec8a06cf2970f60e14211efe55ad2dec0de21a46da64509ff44dc14f7582",
    8: "d39c2436d142d4bd34fc7cae69e3ce2d778c774f79291793230b3765b2e5cbc3",
}


def fields(i):
    return [hashlib.sha256(f"{k}{i}".encode()).digest() for k in ("scene", "ann", "img", "seg")]


def leaves(n):
    return [leaf_hash(i, *fields(i)) for i in range(n)]


def test_leaf_vectors():
    for i, expected in enumerate(LEAVES_HEX):
        assert leaf_hash(i, *fields(i)).hex() == expected


def test_worked_example_from_protocol_doc():
    leaf = leaf_hash(5, b"\x11" * 32, b"\x22" * 32, b"\x33" * 32, b"\x44" * 32)
    assert leaf.hex() == "5b4faa09ef2bcb0f4a444889da86952db0534a0f1e6cf209b81e411e5de53fd4"
    preimage = b"\x00" + (5).to_bytes(4, "little") + b"\x11" * 32 + b"\x22" * 32 + b"\x33" * 32 + b"\x44" * 32
    assert len(preimage) == 133
    assert hashlib.sha256(preimage).digest() == leaf


@pytest.mark.parametrize("n", sorted(ROOTS_HEX))
def test_roots(n):
    assert merkle_root(leaves(n)).hex() == ROOTS_HEX[n]


def test_node_hash_domain_separation():
    a, b = leaves(2)
    assert node_hash(a, b) == hashlib.sha256(b"\x01" + a + b).digest()
    assert node_hash(a, b) != node_hash(b, a)


def test_odd_node_is_duplicated():
    l3 = leaves(3)
    h01 = node_hash(l3[0], l3[1])
    h22 = node_hash(l3[2], l3[2])
    assert merkle_root(l3) == node_hash(h01, h22)


def test_proof_lengths():
    assert [proof_length(n) for n in (1, 2, 3, 4, 5, 7, 8, 9)] == [0, 1, 2, 2, 3, 3, 3, 4]


@pytest.mark.parametrize("n", sorted(ROOTS_HEX))
def test_proofs_first_middle_last(n):
    ls = leaves(n)
    root = merkle_root(ls)
    for idx in sorted({0, n // 2, n - 1}):
        proof = build_proof(ls, idx)
        assert len(proof) == proof_length(n)
        assert verify_proof(ls[idx], idx, proof, root, n)
        assert verify_frame(idx, *fields(idx), proof, root, n)


def test_every_index_verifies_in_odd_and_even_trees():
    for n in (1, 2, 3, 5, 7, 8, 9, 20):
        ls = leaves(n)
        root = merkle_root(ls)
        for idx in range(n):
            assert verify_proof(ls[idx], idx, build_proof(ls, idx), root, n), (n, idx)


def test_hardcoded_proof_three_leaves_last():
    ls = [bytes.fromhex(h) for h in LEAVES_HEX[:3]]
    h01 = hashlib.sha256(b"\x01" + ls[0] + ls[1]).digest()
    proof = [(ls[2], False), (h01, True)]  # sibling of leaf 2 is itself (duplicated)
    assert build_proof(ls, 2) == proof
    assert verify_proof(ls[2], 2, proof, bytes.fromhex(ROOTS_HEX[3]), 3)


def test_tampered_proof_fails():
    ls = leaves(8)
    root = merkle_root(ls)
    proof = build_proof(ls, 3)
    for level in range(len(proof)):
        sib, side = proof[level]
        bad = list(proof)
        bad[level] = (bytes([sib[0] ^ 1]) + sib[1:], side)
        assert not verify_proof(ls[3], 3, bad, root, 8)
    flipped = [(s, not side) for s, side in proof]
    assert not verify_proof(ls[3], 3, flipped, root, 8)
    assert not verify_proof(ls[3], 3, proof[:-1], root, 8)
    assert not verify_proof(ls[3], 3, proof + [proof[-1]], root, 8)
    assert not verify_proof(ls[3], 3, proof, bytes(32), 8)


def test_proof_for_wrong_index_fails():
    ls = leaves(8)
    root = merkle_root(ls)
    proof = build_proof(ls, 3)
    assert verify_proof(ls[3], 3, proof, root, 8)
    for wrong in (0, 2, 4, 7):
        assert not verify_proof(ls[3], wrong, proof, root, 8)
    # leaf recomputed with a different index is a different leaf
    assert not verify_frame(4, *fields(3), proof, root, 8)


def test_out_of_range_index_fails():
    ls = leaves(8)
    root = merkle_root(ls)
    proof = build_proof(ls, 7)
    assert not verify_proof(ls[7], 8, proof, root, 8)
    assert not verify_proof(ls[7], -1, proof, root, 8)
    assert not verify_proof(ls[7], 7, proof, root, 0)


def test_duplicated_last_leaf_forgery_fails():
    """Odd tree: the last leaf is paired with itself. A forger flips the sibling side
    to claim the phantom slot index == frame_count. The fold still reaches the root,
    so only the range/position checks stop it."""
    n = 3
    ls = leaves(n)
    root = merkle_root(ls)
    honest = build_proof(ls, 2)
    assert verify_proof(ls[2], 2, honest, root, n)

    sib, _ = honest[0]
    forged = [(sib, True)] + honest[1:]  # duplicate leaf placed on the left: implies index 3
    # the hash fold is unchanged, which is why the structural checks are required
    cur = node_hash(forged[0][0], ls[2])
    assert node_hash(forged[1][0], cur) == root
    # claiming the phantom index (== frame_count) must fail
    assert not verify_proof(ls[2], n, forged, root, n)
    # claiming the real index with the forged proof must fail (position mismatch)
    assert not verify_proof(ls[2], 2, forged, root, n)
    # same through the field-level API: phantom index 3 with leaf 2's fields
    assert not verify_frame(3, *fields(2), forged, root, n)


def test_duplicated_last_leaf_forgery_larger_odd_tree():
    for n in (5, 7, 9):
        ls = leaves(n)
        root = merkle_root(ls)
        honest = build_proof(ls, n - 1)
        forged = [(honest[0][0], True)] + honest[1:]
        assert not verify_proof(ls[n - 1], n, forged, root, n)
        assert not verify_proof(ls[n - 1], n - 1, forged, root, n)


def test_malformed_inputs_do_not_raise():
    ls = leaves(4)
    root = merkle_root(ls)
    assert not verify_proof(b"short", 0, build_proof(ls, 0), root, 4)
    assert not verify_proof(ls[0], 0, [(b"x", False), (b"y", True)], root, 4)
    assert not verify_proof(ls[0], 0, [(ls[1], 1), (ls[2], 0)], root, 4)  # non-bool side


def test_input_validation():
    with pytest.raises(ValueError):
        leaf_hash(-1, *fields(0))
    with pytest.raises(ValueError):
        leaf_hash(2**32, *fields(0))
    with pytest.raises(ValueError):
        leaf_hash(0, b"short", *fields(0)[1:])
    with pytest.raises(ValueError):
        merkle_root([])
    with pytest.raises(IndexError):
        build_proof(leaves(3), 3)


def test_build_levels_shape():
    levels = build_levels(leaves(7))
    assert [len(lv) for lv in levels] == [7, 4, 2, 1]
