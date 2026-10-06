"""What a frame commits to. One definition, used by the node (to build leaves) and the verifier (to check them).

Spec: docs/PROTOCOL.md section 3 and 4.
  scene_hash      = sha256(canonical(scene graph))
  annotation_hash = sha256(canonical(annotations))
  image_hash      = sha256(rgb.png bytes)
  seg_hash        = sha256(seg.png bytes)
"""
from __future__ import annotations

from dataclasses import dataclass

from .canon import hash_json, sha256
from .merkle import leaf_hash


@dataclass(frozen=True)
class FrameHashes:
    scene_hash: bytes
    annotation_hash: bytes
    image_hash: bytes
    seg_hash: bytes

    def leaf(self, frame_index: int) -> bytes:
        return leaf_hash(frame_index, self.scene_hash, self.annotation_hash, self.image_hash, self.seg_hash)

    def to_hex(self) -> dict[str, str]:
        return {k: getattr(self, k).hex() for k in ("scene_hash", "annotation_hash", "image_hash", "seg_hash")}

    @classmethod
    def from_hex(cls, d: dict[str, str]) -> "FrameHashes":
        return cls(*(bytes.fromhex(d[k]) for k in ("scene_hash", "annotation_hash", "image_hash", "seg_hash")))


def hash_frame(scene: dict, annotations: dict, rgb_png: bytes, seg_png: bytes) -> FrameHashes:
    return FrameHashes(hash_json(scene), hash_json(annotations), sha256(rgb_png), sha256(seg_png))
