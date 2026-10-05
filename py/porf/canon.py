"""Canonical JSON and hashing. Spec: docs/PROTOCOL.md section 3.

Anything that feeds a hash must go through this module.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


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
