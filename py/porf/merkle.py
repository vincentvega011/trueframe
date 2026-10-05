"""Merkle commitment over per-frame records. Spec: docs/PROTOCOL.md section 4.

Leaf: sha256(0x00 || frame_index_u32_le || scene_hash || annotation_hash || image_hash || seg_hash)
Node: sha256(0x01 || left || right)
An odd node at a level is duplicated. Proof: list of (sibling_hash, sibling_is_left),
leaf level first.
"""
from __future__ import annotations

from .canon import sha256

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
HASH_LEN = 32
MAX_FRAME_INDEX = 2**32 - 1

Proof = list[tuple[bytes, bool]]


def _check_hash(name: str, value: bytes) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) != HASH_LEN:
        raise ValueError(f"{name} must be {HASH_LEN} bytes")


def leaf_hash(
    frame_index: int,
    scene_hash: bytes,
    annotation_hash: bytes,
    image_hash: bytes,
    seg_hash: bytes,
) -> bytes:
    if not 0 <= frame_index <= MAX_FRAME_INDEX:
        raise ValueError("frame_index must fit in u32")
    for name, value in (
        ("scene_hash", scene_hash),
        ("annotation_hash", annotation_hash),
        ("image_hash", image_hash),
        ("seg_hash", seg_hash),
    ):
        _check_hash(name, value)
    return sha256(
        LEAF_PREFIX
        + frame_index.to_bytes(4, "little")
        + scene_hash
        + annotation_hash
        + image_hash
        + seg_hash
    )


def node_hash(left: bytes, right: bytes) -> bytes:
    _check_hash("left", left)
    _check_hash("right", right)
    return sha256(NODE_PREFIX + left + right)


def proof_length(frame_count: int) -> int:
    """Number of levels above the leaves: ceil(log2(frame_count)); 0 for one leaf."""
    if frame_count < 1:
        raise ValueError("frame_count must be >= 1")
    return (frame_count - 1).bit_length()


def build_levels(leaves: list[bytes]) -> list[list[bytes]]:
    """All tree levels, leaves first, root level (length 1) last.

    Odd levels are padded with a copy of their last node only while computing
    the parent level; the stored levels keep the real node count.
    """
    if not leaves:
        raise ValueError("cannot build a tree with no leaves")
    for leaf in leaves:
        _check_hash("leaf", leaf)
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = []
        for i in range(0, len(cur), 2):
            left = cur[i]
            right = cur[i + 1] if i + 1 < len(cur) else cur[i]
            nxt.append(node_hash(left, right))
        levels.append(nxt)
    return levels


def merkle_root(leaves: list[bytes]) -> bytes:
    return build_levels(leaves)[-1][0]


def build_proof(leaves: list[bytes], index: int) -> Proof:
    levels = build_levels(leaves)
    if not 0 <= index < len(leaves):
        raise IndexError("index out of range")
    proof: Proof = []
    pos = index
    for level in levels[:-1]:
        if pos % 2 == 0:
            sibling = level[pos + 1] if pos + 1 < len(level) else level[pos]
            proof.append((sibling, False))
        else:
            proof.append((level[pos - 1], True))
        pos //= 2
    return proof


def verify_proof(
    leaf: bytes,
    frame_index: int,
    proof: Proof,
    root: bytes,
    frame_count: int,
) -> bool:
    """True iff the proof folds `leaf` to `root` AND is consistent with the position.

    Checks, in order: frame_index < frame_count; proof length == ceil(log2(frame_count));
    the position implied by the proof (bit i = 1 iff sibling_is_left at level i) equals
    frame_index; the folded hash equals root. Never raises on malformed proofs.
    """
    try:
        if frame_count < 1 or not 0 <= frame_index < frame_count:
            return False
        if len(proof) != proof_length(frame_count):
            return False
        cur = leaf
        _check_hash("leaf", cur)
        position = 0
        for level, (sibling, sibling_is_left) in enumerate(proof):
            if not isinstance(sibling_is_left, bool):
                return False
            if sibling_is_left:
                position |= 1 << level
                cur = node_hash(sibling, cur)
            else:
                cur = node_hash(cur, sibling)
        return position == frame_index and cur == root
    except (ValueError, TypeError):
        return False


def verify_frame(
    frame_index: int,
    scene_hash: bytes,
    annotation_hash: bytes,
    image_hash: bytes,
    seg_hash: bytes,
    proof: Proof,
    root: bytes,
    frame_count: int,
) -> bool:
    """Recompute the leaf from revealed fields, then verify its proof."""
    try:
        leaf = leaf_hash(frame_index, scene_hash, annotation_hash, image_hash, seg_hash)
    except ValueError:
        return False
    return verify_proof(leaf, frame_index, proof, root, frame_count)
