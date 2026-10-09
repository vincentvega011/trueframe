"""Per-request job state and per-frame commitment data, all on disk under <out>/<request_id>/.

    job.json     state machine: claimed -> rendered -> committed   (or failed)
    leaves.json  the committed leaves: per-frame hashes, leaf hashes, root. Written once, right before commit.
    frame_<i>/   scene.json annotations.json rgb.png seg.png depth.png   (written by porf.render.runner)

Everything needed to resume after a crash, and to answer reveal requests, is read back from these files.
Spec: docs/PROTOCOL.md section 16.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canon import canonical_bytes
from ..merkle import build_levels, build_proof, merkle_root, proof_to_json, verify_proof
from ..records import FrameHashes, hash_frame
from ..render.runner import frame_dir

log = logging.getLogger("porf.node")

STATES = ("claimed", "rendered", "committed", "failed")
FRAME_FILES = ("scene.json", "annotations.json", "rgb.png", "seg.png")  # committed; depth.png is served but not hashed


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@dataclass
class Job:
    dir: Path
    data: dict[str, Any]

    # ---- identity / state
    @property
    def request(self) -> str:
        return self.data["request"]

    @property
    def state(self) -> str:
        return self.data["state"]

    @property
    def num_frames(self) -> int:
        return self.data["num_frames"]

    def frame_dir(self, index: int) -> Path:
        return frame_dir(self.dir, index)

    def save(self) -> None:
        self.data["updated_at"] = time.time()
        _atomic_write(self.dir / "job.json", json.dumps(self.data, indent=2, sort_keys=True).encode())

    def update(self, **fields: Any) -> None:
        if fields.get("state", self.state) not in STATES:
            raise ValueError(f"unknown job state {fields.get('state')!r}")
        self.data.update(fields)
        self.save()

    # ---- construction
    @staticmethod
    def guard(out_dir: Path, request_address: str, request_id: int) -> None:
        """Raise if <out>/<request_id>/ belongs to a different request. Call BEFORE staking, not after."""
        existing = out_dir / str(request_id) / "job.json"
        if existing.exists():
            other = json.loads(existing.read_text())
            if other.get("request") != request_address:
                raise RuntimeError(
                    f"{existing.parent} already belongs to request {other.get('request')}; request_id {request_id} "
                    f"collides with {request_address} (ids are only unique per requester): use another --out"
                )

    @classmethod
    def create(cls, out_dir: Path, request_address: str, request: Any, **extra: Any) -> "Job":
        """`request` is a porf.chain.client.Request. Returns the existing job if this request already has one."""
        cls.guard(out_dir, request_address, request.request_id)
        directory = out_dir / str(request.request_id)
        if (directory / "job.json").exists():
            return cls.load(directory)
        directory.mkdir(parents=True, exist_ok=True)
        job = cls(directory, {
            "version": 1, "request": request_address, "requester": request.requester, "request_id": request.request_id,
            "seed": request.seed, "num_frames": request.num_frames, "width": request.width, "height": request.height,
            "occlusion_level": request.occlusion_level, "state": "claimed", "created_at": time.time(), **extra,
        })
        job.save()
        return job

    @classmethod
    def load(cls, directory: Path) -> "Job":
        return cls(directory, json.loads((directory / "job.json").read_text()))

    @classmethod
    def scan(cls, out_dir: Path) -> list["Job"]:
        jobs = []
        for p in sorted(out_dir.glob("*/job.json")) if out_dir.exists() else []:
            try:
                jobs.append(cls.load(p.parent))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                log.warning("ignoring unreadable job file %s: %s", p, e)
        return jobs

    # ---- rendered output
    def render_complete(self) -> bool:
        return all((self.frame_dir(i) / f).exists() for i in range(self.num_frames) for f in (*FRAME_FILES, "depth.png"))


# ---------------------------------------------------------------- frame hashes and the leaf cache

def read_frame(job: Job, index: int) -> tuple[dict, dict, bytes, bytes]:
    d = job.frame_dir(index)
    return (
        json.loads((d / "scene.json").read_bytes()),
        json.loads((d / "annotations.json").read_bytes()),
        (d / "rgb.png").read_bytes(),
        (d / "seg.png").read_bytes(),
    )


def frame_hashes(job: Job, index: int) -> FrameHashes:
    return hash_frame(*read_frame(job, index))


def build_leaf_cache(job: Job) -> dict[str, Any]:
    """Hash every rendered frame from disk and build the commitment. Does not write anything."""
    hashes = [frame_hashes(job, i) for i in range(job.num_frames)]
    leaves = [h.leaf(i) for i, h in enumerate(hashes)]
    return {
        "frame_count": job.num_frames,
        "merkle_root": merkle_root(leaves).hex(),
        "frames": [{"frame_index": i, **h.to_hex(), "leaf_hash": leaves[i].hex()} for i, h in enumerate(hashes)],
    }


def write_leaf_cache(job: Job, cache: dict[str, Any]) -> None:
    _atomic_write(job.dir / "leaves.json", canonical_bytes(cache))


def load_leaf_cache(job: Job) -> dict[str, Any]:
    return json.loads((job.dir / "leaves.json").read_text())


# ---------------------------------------------------------------- reveal

class FrameNotFound(Exception):
    pass


def _b64(path: Path) -> str | None:
    return base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None


def reveal_payload(job: Job, index: int) -> dict[str, Any]:
    """The JSON the reveal server returns for one frame (PROTOCOL.md section 16.4).

    Content comes from the files on disk NOW; the leaf fields and the proof come from the cache written at commit
    time. If a file changed after the commit the two disagree, and the verifier's hash check rejects the frame:
    the node does not hide or repair that.
    """
    cache = load_leaf_cache(job)
    count = cache["frame_count"]
    if not isinstance(index, int) or not 0 <= index < count:
        raise FrameNotFound(f"frame index {index} out of range (0..{count - 1})")
    entry = cache["frames"][index]
    leaves = [bytes.fromhex(f["leaf_hash"]) for f in cache["frames"]]
    proof = build_proof(leaves, index)
    root = bytes.fromhex(cache["merkle_root"])
    if not verify_proof(leaves[index], index, proof, root, count):  # internal consistency of the cache itself
        raise RuntimeError(f"leaf cache of {job.request} does not reproduce its own root")
    d = job.frame_dir(index)
    try:
        scene, annotations, rgb, seg = read_frame(job, index)
    except FileNotFoundError as e:
        raise FrameNotFound(f"frame {index} files are missing: {e.filename}") from e
    if hash_frame(scene, annotations, rgb, seg).leaf(index).hex() != entry["leaf_hash"]:
        log.warning("frame %d of %s differs from what was committed; serving it as-is", index, job.request)
    return {
        "request": job.request,
        "frame_index": index,
        "frame_count": count,
        "merkle_root": cache["merkle_root"],
        "leaf": {k: entry[k] for k in ("frame_index", "scene_hash", "annotation_hash", "image_hash", "seg_hash", "leaf_hash")},
        "proof": proof_to_json(proof),
        "scene": scene,
        "annotations": annotations,
        # the exact bytes of the two JSON files (what the leaf hashes cover), so a client that cannot reproduce Python's
        # canonical float formatting (a browser) can hash them as served
        "scene_json": (d / "scene.json").read_text(encoding="utf-8"),
        "annotations_json": (d / "annotations.json").read_text(encoding="utf-8"),
        "rgb_png": base64.b64encode(rgb).decode("ascii"),
        "seg_png": base64.b64encode(seg).decode("ascii"),
        "depth_png": _b64(d / "depth.png"),
    }
