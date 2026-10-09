"""Fills dist-explorer/ with the data the explorer shows: both requests' reports and frame images under reports/, and the
honest run's full dataset under frames/<i>.json (the static frame-source shape, docs/PROTOCOL.md section 16.6).
Every exported frame is verified against the Merkle root read from devnet before anything is kept; a mismatch fails the build.

    py/.venv/bin/python scripts/explorer_data.py <dist-explorer dir>

Reads out/devnet/ (the devnet runs on this machine). RPC: $DEVNET_RPC_URL if set (never printed), else the public endpoint.
"""
from __future__ import annotations

import base64
import json
import shutil
import sys
from pathlib import Path

from porf.chain.client import ChainClient, devnet_rpc_url, redact
from porf.merkle import proof_from_json, verify_frame
from porf.node.job import Job, reveal_payload
from porf.records import hash_frame

ROOT = Path(__file__).resolve().parent.parent
HONEST = "FUbhyAU19SKaFB4tNfKU4URWwksVAL9jJorfeTVo5d2j"
CHEAT = "EDN2pWCD2DJ8Yo9YxP5P2SrasK11oF7UxtbhF9tc3C4y"


def main(dist: Path) -> int:
    reports_src, node_src = ROOT / "out/devnet/verifier", ROOT / "out/devnet/node"
    for address in (HONEST, CHEAT):
        src = reports_src / address
        if not (src / "report.json").exists():
            print(f"missing {src}/report.json: the devnet run is not on this machine", file=sys.stderr)
            return 1
        dst = dist / "reports" / address
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "report.json", dst / "report.json")
        if (src / "frames").is_dir():
            shutil.copytree(src / "frames", dst / "frames", dirs_exist_ok=True)

    job = next((j for j in Job.scan(node_src) if j.request == HONEST), None)
    if job is None:
        print(f"no node job for {HONEST} under {node_src}", file=sys.stderr)
        return 1
    client = ChainClient(rpc_url=devnet_rpc_url())
    sub = client.get_submission(request=HONEST)
    root, count = bytes.fromhex(sub.merkle_root), sub.frame_count
    frames_dir = dist / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = reveal_payload(job, i)
        h = hash_frame(p["scene"], p["annotations"], base64.b64decode(p["rgb_png"]), base64.b64decode(p["seg_png"]))
        if not verify_frame(i, h.scene_hash, h.annotation_hash, h.image_hash, h.seg_hash, proof_from_json(p["proof"]), root, count):
            print(f"frame {i} does not verify against the on-chain root {root.hex()}", file=sys.stderr)
            return 1
        p.pop("depth_png", None)
        (frames_dir / f"{i}.json").write_text(json.dumps(p, separators=(",", ":")))
    print(f"exported {count} frames of {HONEST}, all verified against the on-chain root {root.hex()[:16]}...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(Path(sys.argv[1]).resolve()))
    except Exception as e:  # never let an RPC URL with a key reach the terminal
        print(redact(f"{type(e).__name__}: {e}"), file=sys.stderr)
        sys.exit(1)
