"""A fully consistent, Blender-free "honest node" for verifier unit tests.

Each frame: the real scene generator output; a 16-bit seg.png where every object's projected box is painted (far to near,
so nearer objects occlude farther ones); annotations from the renderer's own build_annotations; a noise rgb.png. The
commitment and the reveal payloads are produced by the node's own code (build_leaf_cache, reveal_payload), so a tampered
variant starts from something that verifies, and the format cannot drift from what the real server returns.

`mutate(index, files)` lets a test change what the node COMMITS (hash-consistent lies); editing the returned raw reveal
dict instead models changing what is SERVED after the commit (hash mismatches).
"""
from __future__ import annotations

import copy
import io
import math
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from porf.canon import canonical_bytes
from porf.chain.client import Request, Submission
from porf.node.job import Job, build_leaf_cache, load_leaf_cache, reveal_payload, write_leaf_cache
from porf.render.runner import build_annotations
from porf.scene.camera import Camera
from porf.scene.generator import generate_scene
from porf.scene.projection import OK, project_box
from porf.verifier.checks import DEFAULT_THRESHOLDS, SeenImages, VerifyContext
from porf.verifier.reveal import Reveal

W, H = 320, 240
ADDRESS = "FixtureRequestAddress1111111111111111111111111"


def png_bytes(arr: np.ndarray, mode: str | None = None) -> bytes:
    """PNG of an array. The mode is inferred from the dtype/shape (uint8 HxWx3 -> RGB, uint16 HxW -> I;16); `mode` only asserts it."""
    img = Image.fromarray(arr)
    assert mode is None or img.mode == mode, (img.mode, mode)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def synthetic_seg(scene: dict) -> np.ndarray:
    cam = Camera.from_dict(scene["camera"])
    seg = np.zeros((cam.height, cam.width), dtype=np.uint16)
    cx, cy, cz = cam.location

    def dist(o):
        x, y, z = o["location"]
        return (x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2

    for obj in sorted(scene["objects"], key=dist, reverse=True):  # far to near: near objects overwrite
        proj = project_box(cam, obj)
        if proj.status == OK:
            x0, y0, x1, y1 = proj.box
            # pixel-centre sampling, like the real 1-sample id pass: pixel i is covered when i + 0.5 lies inside the box
            seg[int(math.ceil(y0 - 0.5)): int(math.ceil(y1 - 0.5)), int(math.ceil(x0 - 0.5)): int(math.ceil(x1 - 0.5))] = obj["id"]
    return seg


def synthetic_rgb(index: int, seed: int) -> bytes:
    rng = np.random.RandomState(seed * 1000 + index)
    return png_bytes(rng.randint(0, 256, (H, W, 3)).astype(np.uint8), "RGB")


class Files:
    """The four committed files of one frame (what a `mutate` hook may change)."""

    def __init__(self, scene: dict, annotations: dict, rgb: bytes, seg: bytes):
        self.scene, self.annotations, self.rgb, self.seg = scene, annotations, rgb, seg


class HonestNode:
    def __init__(self, tmp_path: Path, n: int = 8, seed: int = 42, occlusion: int = 3,
                 mutate: Callable[[int, Files], None] | None = None, scene_seed: int | None = None):
        self.n, self.seed, self.occlusion, self.address = n, seed, occlusion, ADDRESS
        self.request = Request(
            requester="Requester111", request_id=7, mint="Mint111", scene_type=0, seed=seed, num_frames=n, width=W, height=H,
            occlusion_level=occlusion, price_per_frame=1_000_000, escrow_amount=n * 1_000_000, stake_amount=n * 200_000,
            claim_duration=600, commit_duration=900, challenge_window_secs=30, node="Node111", status="Challenged",
            created_at=1, claimed_at=2, claim_deadline=601, commit_deadline=902, challenge_window_end=999_999_999_999, bump=255)
        self.job = Job.create(tmp_path, ADDRESS, self.request)
        # the verifier's own re-render of the id pass: the TRUE image for the REQUESTED scene, whatever the node later commits
        self.reference = {i: synthetic_seg(generate_scene(seed, i, occlusion, W, H)).astype(np.int64) for i in range(n)}
        for i in range(n):
            scene = generate_scene(scene_seed if scene_seed is not None else seed, i, occlusion, W, H)
            seg = synthetic_seg(scene)
            files = Files(scene, build_annotations(scene, seg.astype(np.int64)), synthetic_rgb(i, seed), png_bytes(seg, "I;16"))
            if mutate:
                mutate(i, files)
            d = self.job.frame_dir(i)
            d.mkdir(parents=True)
            (d / "scene.json").write_bytes(canonical_bytes(files.scene))
            (d / "annotations.json").write_bytes(canonical_bytes(files.annotations))
            (d / "rgb.png").write_bytes(files.rgb)
            (d / "seg.png").write_bytes(files.seg)
        self.cache = build_leaf_cache(self.job)
        write_leaf_cache(self.job, self.cache)
        self.job.update(state="committed")
        self.submission = Submission(
            request=ADDRESS, node="Node111", merkle_root=self.cache["merkle_root"], frame_count=n, commit_slot=100,
            sample_seed="ab" * 32, sample_slot=149, sample_count=min(n, 7), verdict="Pending", bump=250)

    def recommit(self) -> None:
        """Rebuild the commitment from whatever is on disk now (a cheat that rewrote files before committing)."""
        import dataclasses
        self.cache = build_leaf_cache(self.job)
        write_leaf_cache(self.job, self.cache)
        self.submission = dataclasses.replace(self.submission, merkle_root=self.cache["merkle_root"])

    def raw(self, index: int) -> dict:
        """What the reveal server would return for this frame (a fresh copy to tamper with)."""
        return copy.deepcopy(reveal_payload(self.job, index))

    def reveal(self, index: int) -> Reveal:
        return Reveal.parse(self.raw(index))

    def ctx(self, thresholds=DEFAULT_THRESHOLDS, seen: SeenImages | None = None) -> VerifyContext:
        return VerifyContext(ADDRESS, self.request, self.submission, thresholds, seen or SeenImages(), reference_seg=self.reference)


def other_root_submission(node: HonestNode, root_hex: str) -> Submission:
    s = node.submission
    return Submission(s.request, s.node, root_hex, s.frame_count, s.commit_slot, s.sample_seed, s.sample_slot, s.sample_count, s.verdict, s.bump)
