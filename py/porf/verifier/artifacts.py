"""Images the verifier keeps for the report viewer (docs/PROTOCOL.md section 17.7). Pure presentation: built from arrays and
boxes the verifier already holds, after the checks ran; nothing here feeds a check or the verdict.

Written to <report dir>/frames/<index>/:
  rgb.png          the revealed picture, full size
  overlay.png      rgb with the node's boxes (solid green; solid red when that box fails a check), the boxes the scene
                   predicts (dashed grey) and projected objects with no mask (dashed, labelled "hidden")
  seg_node.png     the node's seg.png in false colour        } only for a frame that fails check 7
  seg_reference.png the verifier's re-render, same colours   }
  seg_diff.png     differing pixels red on a dim copy of the re-render
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..scene.camera import Camera
from ..scene.projection import OK, project_box
from ..seg import SegError, analyze, decode_seg
from .reveal import Reveal
from .segcompare import compare_seg

log = logging.getLogger("porf.verifier.artifacts")

# Red means a failure and nothing else (the console follows the same rule).
GREEN, RED, GREY, DIM = (61, 220, 132), (255, 77, 77), (135, 148, 160), (92, 104, 115)
BACKGROUND = (14, 17, 20)


def false_colour(seg: np.ndarray) -> Image.Image:
    """Each object id gets a stable, well-separated colour (derived from the id alone); background is the page's graphite."""
    rgb = np.empty(seg.shape + (3,), dtype=np.uint8)
    rgb[:] = BACKGROUND
    for obj_id in np.unique(seg).tolist():
        if obj_id == 0:
            continue
        h = hashlib.sha256(b"porf-seg-colour" + int(obj_id).to_bytes(4, "little")).digest()
        c = [64 + h[i] % 192 for i in range(3)]
        rgb[seg == obj_id] = c
    return Image.fromarray(rgb, "RGB")


def diff_image(revealed: np.ndarray, reference: np.ndarray) -> Image.Image:
    base = np.array(false_colour(reference)).astype(np.float32) * 0.35
    base[revealed != reference] = RED
    return Image.fromarray(base.astype(np.uint8), "RGB")


def failing_objects(reveal: Reveal, scene: dict, reference: np.ndarray | None, thresholds: Any) -> set[int]:
    """Objects whose claimed box fails a check, for colouring only. It restates the per-object part of checks 4, 6 and 7:
    the box is empty or outside the image, sticks out of the projected box by more than the tolerance, differs from the
    extents of the node's own seg.png, or (when a re-render exists) the object's mask disagrees with it. The verdict never
    reads this."""
    out: set[int] = set()
    anns = {o.get("instance_id"): o for o in (reveal.annotations or {}).get("objects", []) if isinstance(o, dict)}
    cam = Camera.from_dict(scene["camera"])
    node_seg, stats = None, {}
    if reveal.seg_png is not None:
        try:
            node_seg = decode_seg(reveal.seg_png)
            stats = analyze(node_seg)
        except SegError:
            pass
    iou_by_id: dict[int, float] = {}
    if node_seg is not None and reference is not None and node_seg.shape == reference.shape:
        th7 = thresholds.seg_rerender
        for oid, ag in compare_seg(node_seg, reference).objects.items():
            if ag.larger >= th7.min_object_pixels:
                iou_by_id[oid] = ag.iou
        min_iou = th7.min_object_iou
    for obj in scene["objects"]:
        box = (anns.get(obj["id"]) or {}).get("bbox")
        if box is None:
            continue
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
            out.add(obj["id"])
            continue
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= cam.width and 0 <= y0 < y1 <= cam.height):
            out.add(obj["id"])
            continue
        proj = project_box(cam, obj)
        if proj.status != OK:
            out.add(obj["id"])
            continue
        px0, py0, px1, py1 = proj.box
        if max(px0 - x0, py0 - y0, x1 - px1, y1 - py1) > thresholds.for_class(obj["class"]).pixel_tolerance:
            out.add(obj["id"])
        if node_seg is not None and (obj["id"] not in stats or tuple(box) != tuple(stats[obj["id"]].bbox)):
            out.add(obj["id"])
        if obj["id"] in iou_by_id and iou_by_id[obj["id"]] < min_iou:
            out.add(obj["id"])
    return out


def _dashed_rect(draw: ImageDraw.ImageDraw, box, colour, dash: int, gap: int, width: int = 2) -> None:
    x0, y0, x1, y1 = box
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        length = max(abs(bx - ax), abs(by - ay))
        if length == 0:
            continue
        dx, dy = (bx - ax) / length, (by - ay) / length
        pos = 0.0
        while pos < length:
            end = min(pos + dash, length)
            draw.line([(ax + dx * pos, ay + dy * pos), (ax + dx * end, ay + dy * end)], fill=colour, width=width)
            pos += dash + gap


def _label(draw: ImageDraw.ImageDraw, xy, text: str, colour, taken: list[tuple[float, float, float, float]]) -> bool:
    """Draw a label unless it would overlap one already drawn (the box outline is drawn regardless). Returns whether it was drawn."""
    font = ImageFont.load_default(size=13)
    x, y = xy
    rect = (x, y, x + draw.textlength(text, font=font) + 6, y + 17)
    if any(rect[0] < t[2] + 2 and t[0] - 2 < rect[2] and rect[1] < t[3] + 2 and t[1] - 2 < rect[3] for t in taken):
        return False
    draw.rectangle(rect, fill=BACKGROUND)
    draw.text((x + 3, y + 1), text, fill=colour, font=font)
    taken.append(rect)
    return True


def _overlay(rgb_png: bytes, annotations: dict | None, scene: dict, failing: set[int]) -> Image.Image:
    """Dashed grey: where the scene says each visible object is. Dashed dim + "hidden": a projected object with no mask, which
    is correctly unlabelled. Solid green: the node's box. Solid red: a node box that fails a check."""
    img = Image.open(io.BytesIO(rgb_png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    cam = Camera.from_dict(scene["camera"])
    anns = {o.get("instance_id"): o for o in (annotations or {}).get("objects", []) if isinstance(o, dict)}
    labels: list[tuple[float, float, float, float]] = []
    for obj in scene["objects"]:
        proj = project_box(cam, obj)
        if proj.status != OK:
            continue
        hidden = (anns.get(obj["id"]) or {}).get("bbox") is None
        if hidden:
            _dashed_rect(draw, list(proj.box), DIM, 4, 4, 1)
            _label(draw, (proj.box[0], max(0, proj.box[1] - 17)), "hidden", GREY, labels)
        else:
            _dashed_rect(draw, list(proj.box), GREY, 8, 5)
    for obj in scene["objects"]:
        box = (anns.get(obj["id"]) or {}).get("bbox")
        if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
            draw.rectangle(box, outline=RED if obj["id"] in failing else GREEN, width=2)
    return img


def save_frame_images(directory: Path, index: int, reveal: Reveal, scene: dict, reference: np.ndarray | None, seg_check_failed: bool,
                      thresholds: Any) -> dict[str, str]:
    """Write the images for one sampled frame. Returns {name: path relative to the report directory}. Never raises:
    a reveal too broken to draw simply yields fewer images."""
    out: dict[str, str] = {}
    fdir = directory / "frames" / str(index)
    try:
        fdir.mkdir(parents=True, exist_ok=True)

        def put(name: str, img: Image.Image) -> None:
            img.save(fdir / f"{name}.png", format="PNG")
            out[name] = f"frames/{index}/{name}.png"

        if reveal.rgb_png is not None:
            try:
                (fdir / "rgb.png").write_bytes(reveal.rgb_png)
                out["rgb"] = f"frames/{index}/rgb.png"
                put("overlay", _overlay(reveal.rgb_png, reveal.annotations, scene, failing_objects(reveal, scene, reference, thresholds)))
            except Exception as e:  # not a decodable picture: image_sanity already says so
                log.debug("frame %d: no overlay: %s", index, e)
        if seg_check_failed and reference is not None and reveal.seg_png is not None:
            try:
                node = decode_seg(reveal.seg_png)
                if node.shape == reference.shape:
                    put("seg_node", false_colour(node))
                    put("seg_reference", false_colour(reference))
                    put("seg_diff", diff_image(node, reference))
            except SegError as e:
                log.debug("frame %d: no seg comparison: %s", index, e)
    except OSError as e:
        log.warning("frame %d: could not save images: %s", index, e)
    return out
