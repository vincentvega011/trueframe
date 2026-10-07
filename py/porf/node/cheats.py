"""Cheat modes for the demo. Isolated here so it is obviously test tooling: nothing here runs on an honest node (no --cheat
means none of these functions is called).

Hooks, called by the node around rendering:
  render_plan(mode, seed, n, fraction)   -> [(seed, [frames])]: which seed renders which frames (wrong_seed, partial)
  after_render(mode, job_dir, n, ...)    -> rewrites files before they are hashed
The leaves are built after after_render, so the committed root covers whatever the cheat left on disk: every cheat is
*consistent* (its hashes and proofs verify, scene.json is the honest scene of the frame unless the mode is wrong_seed);
only the checks on the content can catch it. Expected checks per mode: docs/PROTOCOL.md section 8.

Which frames are corrupted: `bad_frames(mode, seed, n, fraction)`, a pure function of (mode, request seed, n, fraction), so a
run is reproducible. The default fraction is 0.5 (half the frames, rounded up). Bad frames are drawn from the middle frames
1..n-2 first (the node's best hiding place, since frames 0 and n-1 are always sampled) and only spill onto the end frames when
the fraction is too large to avoid them. `partial` corrupts the last ceil(fraction*n) frames: it renders only the front part.

no_occlusion: the node does no occlusion reasoning. It paints every object's full projected silhouette into seg.png in
ascending object-id order, with no depth sorting (so racks, which have the lowest ids, end up UNDER the pallets and boxes
that really stand behind them), then derives annotations.json from that seg.png with the real build_annotations.
reuse_frames: a bad frame's rgb, depth, seg and annotation files are copies of those of a good frame.
fake_boxes: a bad frame's annotation boxes are shifted/rescaled (and boxes are invented for hidden objects); seg.png is honest.
wrong_seed: a bad frame is rendered from a different seed, so its scene.json is not the requested scene.
partial: only the front part is rendered; each tail frame is a copy of a rendered frame (with its own, honest scene.json).
These cheats do not save compute (except partial, which really skips the renders); they exist to be caught.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from ..canon import canonical_bytes
from ..render.runner import build_annotations, frame_dir
from ..scene.camera import Camera
from ..scene.generator import generate_scene
from ..scene.projection import silhouette_polygon

log = logging.getLogger("porf.node.cheats")

CHEAT_MODES = ("reuse_frames", "fake_boxes", "wrong_seed", "partial", "no_occlusion")
IMPLEMENTED = CHEAT_MODES
DEFAULT_FRACTION = 0.5
COPY_FILES = ("rgb.png", "depth.png", "seg.png", "annotations.json")


def check_fraction(fraction: float | None) -> float:
    f = DEFAULT_FRACTION if fraction is None else fraction
    if not (0.0 < f <= 1.0):
        raise ValueError(f"--fraction must be in (0, 1], got {fraction}")
    return f


def _rng(mode: str, seed: int, *extra: int) -> random.Random:
    material = b"porf-cheat\0" + mode.encode() + b"\0" + seed.to_bytes(8, "little") + b"".join(x.to_bytes(4, "little") for x in extra)
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "little"))


def bad_frames(mode: str, seed: int, num_frames: int, fraction: float | None = None) -> list[int]:
    """The frames this cheat corrupts, sorted. Deterministic in (mode, seed, num_frames, fraction)."""
    f = check_fraction(fraction)
    count = min(num_frames, math.ceil(f * num_frames - 1e-9))
    if mode in ("reuse_frames", "partial"):
        count = min(count, num_frames - 1)          # a copy needs at least one real frame to copy from
    if count <= 0:
        return []
    if mode == "partial":
        return list(range(num_frames - count, num_frames))
    rng = _rng(mode, seed)
    middle = list(range(1, num_frames - 1))
    ends = sorted({0, num_frames - 1})
    rng.shuffle(middle)
    rng.shuffle(ends)
    return sorted((middle + ends)[:count])


def wrong_seed_for(seed: int) -> int:
    return int.from_bytes(hashlib.sha256(b"porf-wrong-seed\0" + seed.to_bytes(8, "little")).digest()[:8], "little")


def render_plan(mode: str | None, seed: int, num_frames: int, fraction: float | None = None) -> list[tuple[int, list[int]]]:
    """[(seed, frames)] the node renders. Honest: everything from the request's seed."""
    allf = list(range(num_frames))
    if mode == "wrong_seed":
        bad = set(bad_frames(mode, seed, num_frames, fraction))
        plan = [(seed, [i for i in allf if i not in bad]), (wrong_seed_for(seed), sorted(bad))]
        return [(sd, fr) for sd, fr in plan if fr]
    if mode == "partial":
        bad = set(bad_frames(mode, seed, num_frames, fraction))
        return [(seed, [i for i in allf if i not in bad])]
    return [(seed, allf)]


def announce(mode: str | None, fraction: float | None = None, seed: int | None = None, num_frames: int | None = None) -> None:
    if mode:
        log.warning("--cheat %s (fraction %.2f): this node will deliberately COMMIT FRAUDULENT DATA (test tooling)", mode, check_fraction(fraction))


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


def _copy_sources(bad: list[int], num_frames: int) -> dict[int, int]:
    good = [i for i in range(num_frames) if i not in set(bad)]
    return {b: good[n % len(good)] for n, b in enumerate(bad)} if good else {}


def _write_honest_scene(fdir: Path, seed: int, index: int, occlusion_level: int, width: int, height: int) -> None:
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "scene.json").write_bytes(canonical_bytes(generate_scene(seed, index, occlusion_level, width, height)))


def _apply_copies(mode, job_dir, num_frames, seed, bad, occlusion_level, width, height) -> None:
    for dst, src in _copy_sources(bad, num_frames).items():
        _write_honest_scene(frame_dir(job_dir, dst), seed, dst, occlusion_level, width, height)   # its own, honest scene
        for name in COPY_FILES:
            if (frame_dir(job_dir, src) / name).exists():
                shutil.copyfile(frame_dir(job_dir, src) / name, frame_dir(job_dir, dst) / name)


def _apply_fake_boxes(fdir: Path, seed: int, index: int) -> None:
    path = fdir / "annotations.json"
    ann = json.loads(path.read_bytes())
    rng = _rng("fake_boxes", seed, index)
    w, h = ann["image"]["width"], ann["image"]["height"]
    for o in ann["objects"]:
        sx, sy = rng.choice((-1, 1)), rng.choice((-1, 1))
        if o["bbox"]:
            x0, y0, x1, y1 = o["bbox"]
            dx, dy = sx * rng.randint(12, 40), sy * rng.randint(12, 40)
            bw = max(4, int((x1 - x0) * rng.uniform(0.7, 1.4)))
            bh = max(4, int((y1 - y0) * rng.uniform(0.7, 1.4)))
            nx0 = min(max(0, x0 + dx), w - bw)
            ny0 = min(max(0, y0 + dy), h - bh)
            o["bbox"] = [nx0, ny0, nx0 + bw, ny0 + bh]
        else:   # a hidden object: invent a box for it
            bw, bh = rng.randint(20, 80), rng.randint(20, 80)
            nx0, ny0 = rng.randint(0, w - bw), rng.randint(0, h - bh)
            o["bbox"] = [nx0, ny0, nx0 + bw, ny0 + bh]
    path.write_bytes(canonical_bytes(ann))


def after_render(mode: str | None, job_dir: Path, num_frames: int, seed: int = 0, fraction: float | None = None,
                 occlusion_level: int = 0, width: int = 640, height: int = 480) -> None:
    """Rewrite the rendered files for `mode`. `seed` is the request's seed (the one the chain holds), not the cheat's."""
    if mode is None:
        return
    bad = bad_frames(mode, seed, num_frames, fraction)
    if mode == "no_occlusion":
        for i in bad:
            _apply_no_occlusion(frame_dir(job_dir, i))
    elif mode == "fake_boxes":
        for i in bad:
            _apply_fake_boxes(frame_dir(job_dir, i), seed, i)
    elif mode in ("reuse_frames", "partial"):
        _apply_copies(mode, job_dir, num_frames, seed, bad, occlusion_level, width, height)
    # wrong_seed: everything happened in render_plan
    if bad:
        log.warning("cheat %s corrupted frames %s of %d", mode, bad, num_frames)
