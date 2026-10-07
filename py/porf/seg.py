"""Segmentation statistics: ONE definition of "the mask box and pixel count of object i in seg.png", used by the
renderer (to write annotations.json) and by the verifier (to re-derive them from the committed seg.png).

seg.png is a 16-bit grayscale PNG, pixel = object id, 0 = background (docs/PROTOCOL.md section 14.2).
Boxes are [x0, y0, x1, y1] in pixels, origin top-left, x1/y1 exclusive.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class SegObject:
    pixels: int
    bbox: tuple[int, int, int, int]


class SegError(ValueError):
    """seg.png is not a readable 16-bit grayscale PNG."""


def decode_seg(png_bytes: bytes) -> np.ndarray:
    """Decode seg.png bytes to an int64 array of object ids, shape (height, width)."""
    try:
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
    except Exception as e:  # Pillow raises many different types for broken files
        raise SegError(f"not a decodable PNG: {e}") from e
    if img.format != "PNG" or img.mode != "I;16":
        raise SegError(f"expected a 16-bit grayscale PNG, got {img.format} mode {img.mode}")
    return np.array(img).astype(np.int64)


def analyze(seg: np.ndarray) -> dict[int, SegObject]:
    """Per-id pixel count and mask extents for every non-zero id present in `seg`."""
    out: dict[int, SegObject] = {}
    ids, counts = np.unique(seg, return_counts=True)
    for obj_id, count in zip(ids.tolist(), counts.tolist()):
        if obj_id == 0:
            continue
        ys, xs = np.nonzero(seg == obj_id)
        out[obj_id] = SegObject(int(count), (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    return out
