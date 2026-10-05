"""Canonical JSON and hashing. Spec: docs/PROTOCOL.md section 3.

Anything that feeds a hash must go through this module.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

QUANT_DECIMALS = 4


def quantize(x: float) -> float:
    """The one float rule for anything that enters a hashed structure.

    Rounds to 4 decimals, normalizes -0.0 to 0.0, rejects NaN and Infinity.
    """
    x = float(x)
    if not math.isfinite(x):
        raise ValueError("NaN/Infinity cannot enter a scene graph")
    r = round(x, QUANT_DECIMALS)
    return 0.0 if r == 0.0 else r


def canonical_bytes(obj: Any) -> bytes:
    """UTF-8 canonical JSON: sorted keys, no whitespace, ASCII-escaped.

    Raises ValueError on NaN or Infinity anywhere in the structure.
    """
    text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash_json(obj: Any) -> bytes:
    """sha256 of the canonical JSON of obj (scene_hash, annotation_hash)."""
    return sha256(canonical_bytes(obj))
