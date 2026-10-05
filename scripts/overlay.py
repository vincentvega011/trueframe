#!/usr/bin/env python3
"""Debug overlay: mask boxes (green) and projected full-object boxes (red) on rgb.png.

    py/.venv/bin/python scripts/overlay.py out/dev/frame_0            # writes frame_0/overlay.png
    py/.venv/bin/python scripts/overlay.py out/dev/frame_0 -o x.png

Green = box from the segmentation mask (what the node reports). Red = box projected from the
3D scene box through the pinhole camera. Each green box should sit inside its red box.
Fully hidden objects have no green box; their red box is drawn dashed-thin with a trailing '*'.
"""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from porf.scene.camera import Camera
from porf.scene.projection import OK, project_box

GREEN, RED = (0, 255, 0), (255, 0, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("frame_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()

    fdir = args.frame_dir
    scene = json.loads((fdir / "scene.json").read_text())
    ann = {o["instance_id"]: o for o in json.loads((fdir / "annotations.json").read_text())["objects"]}
    cam = Camera.from_dict(scene["camera"])
    img = Image.open(fdir / "rgb.png").convert("RGB")
    draw = ImageDraw.Draw(img)

    for obj in scene["objects"]:
        oid = obj["id"]
        proj = project_box(cam, obj)
        a = ann[oid]
        label = f"{oid}{'*' if a['bbox'] is None else ''}"
        if proj.status == OK:
            x0, y0, x1, y1 = proj.box
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=RED, width=1)
            draw.text((x0 + 2, y0 + 1), label, fill=RED)
        if a["bbox"] is not None:
            x0, y0, x1, y1 = a["bbox"]
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=GREEN, width=1)
    out = args.out or fdir / "overlay.png"
    img.save(out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
