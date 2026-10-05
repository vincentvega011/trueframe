"""Generates scene graphs, renders them in ONE Blender process per batch, writes annotations.

    python -m porf.render.runner --seed 42 --frame 0 --out out/dev

Output per frame, in <out>/frame_<i>/ : scene.json, rgb.png, depth.png, seg.png, annotations.json.
scene.json and annotations.json are written as canonical JSON, so sha256(file bytes) equals
scene_hash / annotation_hash. Spec: docs/PROTOCOL.md section 14.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from ..canon import canonical_bytes, quantize
from ..scene.camera import DEFAULT_HEIGHT, DEFAULT_WIDTH, Camera, blender_camera_spec
from ..scene.generator import CLASS_IDS, generate_scene
from ..scene.projection import silhouette_area

BLENDER_JOB = Path(__file__).with_name("blender_job.py")
MAC_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"
DEFAULT_SAMPLES = 64
VISIBILITY_METHOD = "mask_pixels / silhouette_area, clamped to [0, 1]"


def find_blender() -> str | None:
    for cand in (os.environ.get("BLENDER"), shutil.which("blender"), MAC_BLENDER):
        if cand and os.path.exists(cand):
            return cand
    return None


def frame_dir(out_dir: str | Path, frame_index: int) -> Path:
    return Path(out_dir) / f"frame_{frame_index}"


def read_seg(path: Path) -> np.ndarray:
    return np.array(Image.open(path)).astype(np.int64)


def check_seg_integrity(seg: np.ndarray, scene: dict) -> list[int]:
    """Every pixel must be 0 or a real object id. Returns the ids present."""
    valid = {0} | {o["id"] for o in scene["objects"]}
    present = set(int(v) for v in np.unique(seg))
    stray = present - valid
    if stray:
        raise AssertionError(f"seg.png contains values that are not 0 or an object id: {sorted(stray)[:10]}")
    return sorted(present)


def build_annotations(scene: dict, seg: np.ndarray) -> dict:
    cam = Camera.from_dict(scene["camera"])
    objects = []
    for obj in scene["objects"]:
        mask = seg == obj["id"]
        n = int(mask.sum())
        if n:
            ys, xs = np.nonzero(mask)
            bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]  # half-open pixel bounds
        else:
            bbox = None
        area = silhouette_area(cam, obj)
        vis = 0.0 if n == 0 else (1.0 if area <= 0.0 else min(1.0, n / area))
        objects.append(
            {
                "instance_id": obj["id"],
                "class": obj["class"],
                "class_id": CLASS_IDS[obj["class"]],
                "bbox": bbox,
                "mask_pixels": n,
                "visibility": quantize(vis),
            }
        )
    h, w = seg.shape
    return {
        "image": {"width": int(w), "height": int(h)},
        "bbox_format": "[x0, y0, x1, y1] pixels, origin top-left, x1/y1 exclusive; null if fully hidden",
        "seg_encoding": "uint16 object id, 0 = background",
        "visibility_method": VISIBILITY_METHOD,
        "objects": objects,
    }


def render_frames(
    seed: int,
    frames: list[int],
    out_dir: str | Path,
    occlusion_level: int = 0,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    samples: int = DEFAULT_SAMPLES,
    gpu: bool = False,
    blender: str | None = None,
    test_inflate: dict | None = None,
) -> dict:
    """Render `frames` of (seed, occlusion_level) with a single Blender process."""
    blender = blender or find_blender()
    if not blender:
        raise RuntimeError("Blender not found: set $BLENDER or install it (see docs/ENV.md)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scenes, spec_frames = {}, []
    for i in frames:
        scene = generate_scene(seed, i, occlusion_level, width, height)
        scenes[i] = scene
        fdir = frame_dir(out_dir, i)
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "scene.json").write_bytes(canonical_bytes(scene))
        spec_frames.append(
            {
                "frame_index": i,
                "scene": str((fdir / "scene.json").resolve()),
                "out_dir": str(fdir.resolve()),
                "camera": blender_camera_spec(Camera.from_dict(scene["camera"])),
            }
        )
    spec = {"width": width, "height": height, "samples": samples, "gpu": gpu, "frames": spec_frames}
    if test_inflate:  # TEST ONLY: {"object_id": int, "part": int, "extra": metres}; makes Blender fail loudly
        spec["test_inflate"] = test_inflate
    spec_path = out_dir / "render_spec.json"
    spec_path.write_text(json.dumps(spec, indent=1))

    cmd = [blender, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "1",
           "-P", str(BLENDER_JOB), "--", "--spec", str(spec_path.resolve())]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.time() - t0
    (out_dir / "blender.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise RuntimeError(f"Blender failed (exit {proc.returncode}). Last output:\n{tail}")

    timings = [json.loads(l.split(" ", 1)[1]) for l in proc.stdout.splitlines() if l.startswith("PORF_TIMING")]
    seg_checks = [json.loads(l.split(" ", 1)[1]) for l in proc.stdout.splitlines() if l.startswith("PORF_SEG_CHECK")]

    results = {}
    for i in frames:
        fdir = frame_dir(out_dir, i)
        seg = read_seg(fdir / "seg.png")
        ids = check_seg_integrity(seg, scenes[i])
        ann = build_annotations(scenes[i], seg)
        (fdir / "annotations.json").write_bytes(canonical_bytes(ann))
        for name, expect_mode in (("rgb.png", "RGB"), ("depth.png", None), ("seg.png", None)):
            im = Image.open(fdir / name)
            assert im.size == (width, height), (name, im.size)
            if expect_mode:
                assert im.mode == expect_mode, (name, im.mode)
        results[i] = {"dir": str(fdir), "seg_ids": ids, "annotations": ann}
    return {"wall_seconds": wall, "timings": timings, "seg_checks": seg_checks, "frames": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, required=True)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--frame", type=int, help="render this single frame index")
    which.add_argument("--frames", type=int, metavar="N", help="render frames 0..N-1 in one Blender process")
    ap.add_argument("--occlusion", type=int, default=0, choices=(0, 1, 2, 3), help="occlusion_level")
    ap.add_argument("--out", required=True, help="output directory (frame_<i>/ is created inside)")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="samples for rgb.png (denoised)")
    ap.add_argument("--gpu", action="store_true", help="use Metal GPU instead of CPU")
    args = ap.parse_args(argv)

    frames = [args.frame] if args.frame is not None else list(range(args.frames))
    t0 = time.time()
    res = render_frames(args.seed, frames, args.out, args.occlusion, args.width, args.height, args.samples, args.gpu)
    total = time.time() - t0
    n = len(frames)
    print(f"{n} frame(s), occlusion {args.occlusion}: blender wall {res['wall_seconds']:.2f}s, "
          f"total {total:.2f}s ({total / n:.2f}s/frame incl. process startup)")
    per_frame = [t for t in res["timings"] if t.get("phase") == "frame"]
    if per_frame:
        work = [t["build"] + t["rgb"] + t["aux"] + t["encode"] for t in per_frame]
        print(f"  per-frame render work: mean {sum(work) / len(work):.2f}s, max {max(work):.2f}s")
    for i, r in res["frames"].items():
        objs = r["annotations"]["objects"]
        hidden = [o["instance_id"] for o in objs if o["bbox"] is None]
        print(f"frame {i}: {len(objs)} objects, {len(objs) - len(hidden)} visible, hidden ids {hidden}; "
              f"seg ids ok ({len(r['seg_ids']) - 1} objects + background)")
        if n == 1:
            for p in sorted(Path(r["dir"]).iterdir()):
                print(f"  {p.name:18s} {p.stat().st_size:>9d} bytes")
            for o in objs:
                print(f"  obj {o['instance_id']:>2} {o['class']:8s} bbox={o['bbox']} px={o['mask_pixels']:>6} vis={o['visibility']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
