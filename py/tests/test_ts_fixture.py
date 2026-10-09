"""Fixture shared with the console's TypeScript tests (app/src/__fixtures__/frames.json): frames, leaves and proofs produced by
the Python merkle module for a 5-frame tree. The TS tests verify every frame against this root, so the two implementations
cannot drift apart; this test fails if the committed file is not what Python produces today.
Regenerate: PORF_WRITE_TS_FIXTURE=1 pytest py/tests/test_ts_fixture.py"""
import base64
import json
import os

from porf.canon import canonical_bytes
from porf.merkle import build_proof, leaf_hash, merkle_root, proof_to_json
from porf.records import hash_frame

from porf.chain.client import REPO_ROOT

PATH = REPO_ROOT / "app" / "src" / "__fixtures__" / "frames.json"


def build() -> dict:
    n, frames, leaves = 5, [], []
    for i in range(n):
        scene = {"frame_index": i, "objects": [{"id": 1, "yaw": 1.0, "x": 2.5 + i, "name": "café"}], "seed": 42}
        ann = {"objects": [{"instance_id": 1, "bbox": [i, i, i + 5, i + 5], "visibility": 1.0}]}
        rgb, seg = b"\x89PNGrgb" + bytes([i]) * 7, b"\x89PNGseg" + bytes([255 - i]) * 7
        h = hash_frame(scene, ann, rgb, seg)
        leaves.append(leaf_hash(i, h.scene_hash, h.annotation_hash, h.image_hash, h.seg_hash))
        frames.append({"scene_json": canonical_bytes(scene).decode(), "annotations_json": canonical_bytes(ann).decode(),
                       "rgb_png": base64.b64encode(rgb).decode(), "seg_png": base64.b64encode(seg).decode(),
                       "leaf": {"frame_index": i, "scene_hash": h.scene_hash.hex(), "annotation_hash": h.annotation_hash.hex(),
                                "image_hash": h.image_hash.hex(), "seg_hash": h.seg_hash.hex(), "leaf_hash": leaves[-1].hex()}})
    root = merkle_root(leaves)
    for i, f in enumerate(frames):
        f.update({"request": "FixtureRequest", "frame_index": i, "frame_count": n, "proof": proof_to_json(build_proof(leaves, i))})
    return {"frame_count": n, "merkle_root": root.hex(), "frames": frames}


def test_ts_fixture_is_current():
    fixture = build()
    text = json.dumps(fixture, indent=1, sort_keys=True) + "\n"
    if os.environ.get("PORF_WRITE_TS_FIXTURE"):
        PATH.write_text(text)
    assert PATH.read_text() == text, "app/src/__fixtures__/frames.json is stale: PORF_WRITE_TS_FIXTURE=1 pytest py/tests/test_ts_fixture.py"
