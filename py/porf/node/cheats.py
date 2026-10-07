"""Cheat modes for the demo. Isolated here so it is obviously test tooling: nothing here may run on an honest node.

Hooks, called by the node around rendering:
  before_render(mode, seed)                 -> the seed the node will render from      (wrong_seed)
  after_render(mode, job_dir, num_frames)   -> may rewrite files before they are hashed
The leaves are built after after_render, so the committed root covers whatever the cheat left on disk: a cheat is
*consistent* (its hashes and proofs verify); only the checks on the content can catch it.

IMPLEMENTED: `no_occlusion`. The other three modes (reuse_frames, fake_boxes, wrong_seed, partial) are still no-ops.

no_occlusion: the node does no occlusion reasoning. It paints every object's full projected silhouette into seg.png in
ascending object-id order, with no depth sorting (so racks, which have the lowest ids, end up UNDER the pallets and boxes
that really stand behind them), then derives annotations.json from that seg.png with the real build_annotations. Every
hash, proof, box-inside-projected-box test and annotation-equals-seg test is therefore satisfied; only a re-render of the
true id pass exposes it. rgb.png and depth.png are left as honestly rendered.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from ..canon import canonical_bytes
from ..render.runner import build_annotations, frame_dir
from ..scene.camera import Camera
from ..scene.projection import silhouette_polygon

log = logging.getLogger("porf.node.cheats")

CHEAT_MODES = ("reuse_frames", "fake_boxes", "wrong_seed", "partial", "no_occlusion")
IMPLEMENTED = ("no_occlusion",)


def announce(mode: str | None) -> None:
    if mode in IMPLEMENTED:
        log.warning("--cheat %s: this node will deliberately COMMIT FRAUDULENT DATA (test tooling)", mode)
    elif mode:
        log.warning("--cheat %s requested, but that cheat mode is not implemented yet: this node behaves HONESTLY", mode)


def before_render(mode: str | None, seed: int) -> int:
    return seed  # no cheat changes the seed yet


def paint_silhouettes(scene: dict, order: list[dict] | None = None) -> np.ndarray:
    """Paint each object's full projected silhouette into an id image. `order` is the painting order (later paints over
    earlier ones); the default is ascending object id, i.e. no depth reasoning."""
    cam = Camera.from_dict(scene["camera"])
    img = Image.new("I", (cam.width, cam.height), 0)
    draw = ImageDraw.Draw(img)
    for obj in order if order is not None else sorted(scene["objects"], key=lambda o: o["id"]):
        poly = silhouette_polygon(cam, obj)
        if poly:
            draw.polygon([(x, y) for x, y in poly], fill=obj["id"])
    return np.array(img).astype(np.int64)


def painter_order(scene: dict) -> list[dict]:
    """Objects far to near by the distance of their centre to the camera: a cheater who does cheap depth sorting. Used by the
    calibration to measure how well the seg re-render separates this harder fraud; it is not a cheat mode of the node."""
    cx, cy, cz = scene["camera"]["location"]
    return sorted(scene["objects"], key=lambda o: sum((a - b) ** 2 for a, b in zip(o["location"], (cx, cy, cz))), reverse=True)


def _apply_no_occlusion(fdir: Path) -> None:
    scene = json.loads((fdir / "scene.json").read_bytes())
    seg = paint_silhouettes(scene)
    Image.fromarray(seg.astype(np.uint16)).save(fdir / "seg.png", format="PNG")
    (fdir / "annotations.json").write_bytes(canonical_bytes(build_annotations(scene, seg)))


def after_render(mode: str | None, job_dir: Path, num_frames: int) -> None:
    if mode == "no_occlusion":
        for i in range(num_frames):
            _apply_no_occlusion(frame_dir(job_dir, i))
