"""Pixel-level comparison of two object-id images: the node's revealed seg.png against the verifier's own re-render.

Measures only; the thresholds that turn these numbers into a pass or a fail live in checks.SegRerenderThresholds.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ObjectAgreement:
    object_id: int
    revealed_pixels: int
    reference_pixels: int
    intersection: int
    iou: float                # |A and B| / |A or B| of the two masks of this object

    @property
    def larger(self) -> int:
        return max(self.revealed_pixels, self.reference_pixels)


@dataclass(frozen=True)
class SegComparison:
    global_agreement: float   # fraction of ALL pixels (background included) whose id is identical
    mismatched_pixels: int
    objects: dict[int, ObjectAgreement]   # every non-zero id present in either image


def compare_seg(revealed: np.ndarray, reference: np.ndarray) -> SegComparison:
    if revealed.shape != reference.shape:
        raise ValueError(f"shape mismatch: revealed {revealed.shape}, reference {reference.shape}")
    k = int(max(revealed.max(initial=0), reference.max(initial=0))) + 1
    counts = np.bincount((revealed.ravel() * k + reference.ravel()).astype(np.int64), minlength=k * k).reshape(k, k)
    inter = np.diag(counts)
    revealed_px, reference_px = counts.sum(axis=1), counts.sum(axis=0)
    total = int(revealed.size)
    objects = {}
    for obj_id in range(1, k):
        a, b, i = int(revealed_px[obj_id]), int(reference_px[obj_id]), int(inter[obj_id])
        if a or b:
            objects[obj_id] = ObjectAgreement(obj_id, a, b, i, i / (a + b - i))
    matched = int(inter.sum())
    return SegComparison(matched / total if total else 1.0, total - matched, objects)
