"""Fetching and parsing a node's reveal for one frame (wire format: docs/PROTOCOL.md section 16.4).

A reveal that cannot be obtained after `attempts` tries (each with its own timeout) raises RevealUnavailable. That is
reported as "reveal unavailable", not as a failed check, and it makes the verdict Fail (PROTOCOL.md section 7).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..chain.client import REPO_ROOT

DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT = 10.0
DEFAULT_PAUSE = 1.0
ANNOUNCE_FILE = REPO_ROOT / ".localnet" / "reveal.json"


class RevealUnavailable(Exception):
    """The node did not serve the frame (connection error, timeout, HTTP error, or a body that is not JSON)."""

    def __init__(self, reason: str, attempts: int):
        super().__init__(reason)
        self.reason, self.attempts = reason, attempts


@dataclass
class Reveal:
    """A parsed reveal. Parsing never raises: anything missing or malformed is recorded in `errors` and the field is
    None, so each check can fail with its own message instead of the whole verification crashing."""

    raw: dict[str, Any]
    frame_index: int | None = None
    frame_count: int | None = None
    merkle_root: str | None = None
    leaf: dict[str, Any] | None = None
    proof: Any = None
    scene: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    rgb_png: bytes | None = None
    seg_png: bytes | None = None
    errors: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: Any) -> "Reveal":
        r = cls(raw=raw if isinstance(raw, dict) else {})
        if not isinstance(raw, dict):
            r.errors.append("reveal is not a JSON object")
            return r
        r.frame_index = raw.get("frame_index") if isinstance(raw.get("frame_index"), int) and not isinstance(raw.get("frame_index"), bool) else None
        r.frame_count = raw.get("frame_count") if isinstance(raw.get("frame_count"), int) else None
        r.merkle_root = raw.get("merkle_root") if isinstance(raw.get("merkle_root"), str) else None
        r.leaf = raw.get("leaf") if isinstance(raw.get("leaf"), dict) else None
        r.proof = raw.get("proof")
        r.scene = raw.get("scene") if isinstance(raw.get("scene"), dict) else None
        r.annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else None
        for name, key in (("rgb_png", "rgb.png"), ("seg_png", "seg.png")):
            value = raw.get(name)
            if not isinstance(value, str):
                r.errors.append(f"{key} missing")
                continue
            try:
                setattr(r, name, base64.b64decode(value, validate=True))
            except (binascii.Error, ValueError):
                r.errors.append(f"{key} is not valid base64")
        for key, value in (("leaf", r.leaf), ("scene", r.scene), ("annotations", r.annotations)):
            if value is None:
                r.errors.append(f"{key} missing or not an object")
        if r.proof is None:
            r.errors.append("proof missing")
        return r


def fetch_reveal(base_url: str, request: str, index: int, attempts: int = DEFAULT_ATTEMPTS, timeout: float = DEFAULT_TIMEOUT,
                 pause: float = DEFAULT_PAUSE) -> Reveal:
    """GET <base_url>/frame/<index>?request=<request>. Up to `attempts` tries, `timeout` seconds each, `pause` between."""
    url = f"{base_url.rstrip('/')}/frame/{index}?request={quote(request)}"
    last = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read()
            return Reveal.parse(json.loads(body))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            last = f"response is not JSON ({e})"
        if attempt < attempts:
            time.sleep(pause)
    raise RevealUnavailable(f"frame {index} not served after {attempts} attempts ({timeout:g}s each); last error: {last}", attempts)


def discover_reveal_url(env: dict[str, str] | None = None, announce_file: Path = ANNOUNCE_FILE) -> str:
    # SHORTCUT: the node's reveal URL is not on-chain; the verifier takes it from PORF_REVEAL_URL or from the file the
    # node writes (see docs/LIMITATIONS.md). It therefore trusts whoever controls that variable or file.
    env = os.environ if env is None else env
    if env.get("PORF_REVEAL_URL"):
        return env["PORF_REVEAL_URL"]
    if announce_file.exists():
        try:
            url = json.loads(announce_file.read_text()).get("url")
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"cannot read the node's announced reveal URL from {announce_file}: {e}") from e
        if url:
            return url
    raise RuntimeError(f"no reveal URL: set PORF_REVEAL_URL or start a node that writes {announce_file}")
