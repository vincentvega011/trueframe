#!/usr/bin/env python3
"""Threshold calibration for the verifier: measures honest and fraudulent frames and prints distributions.

    py/.venv/bin/python scripts/calibrate.py --out out/calibration                  # all phases except timing and gpu
    py/.venv/bin/python scripts/calibrate.py --phases render,seg,checks,cheats      # choose phases
    py/.venv/bin/python scripts/calibrate.py --phases timing                        # needs scripts/localnet_up.sh running
    py/.venv/bin/python scripts/calibrate.py --phases gpu                           # Metal vs CPU, same machine

Phases (each saves its raw numbers to <out>/<phase>.json and can be re-run alone):
  render   honest renders at production settings (640x480, 64 samples, CPU): seeds x occlusion levels x frames
  seg      the verifier's seg-only re-render of the same frames, compared with the honest seg.png (check 7 on honest data)
  checks   all seven checks on every honest frame, from the real files, via the node's own reveal code (check 4 statistics)
  cheats   the same comparison for two frauds: no_occlusion (id-order painting) and a depth-sorted painter
  timing   end-to-end verification time of 20-frame requests on a live localnet, idle and under CPU load
  gpu      renders with Metal and compares with the CPU re-render (a rough proxy for cross-machine variance)
Nothing here changes a threshold: it only measures and prints. docs/PROTOCOL.md section 17.5 records the approved numbers.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))

from porf.chain.client import Request, Submission  # noqa: E402
from porf.node.cheats import paint_silhouettes, painter_order  # noqa: E402
from porf.node.job import Job, build_leaf_cache, reveal_payload, write_leaf_cache  # noqa: E402
from porf.render.runner import find_blender, frame_dir, read_seg, render_frames, render_seg_only  # noqa: E402
from porf.scene.generator import generate_scene  # noqa: E402
from porf.verifier.checks import CHECK_NAMES, DEFAULT_THRESHOLDS, SeenImages, VerifyContext, run_checks  # noqa: E402
from porf.verifier.reveal import Reveal  # noqa: E402
from porf.verifier.segcompare import compare_seg  # noqa: E402

W, H = 640, 480
DEFAULT_SEEDS = [42, 7, 99, 2024, 31337]
LEVELS = [0, 1, 2, 3]
P_MINS = [0, 20, 50, 100, 200, 500]


def pct(values, name="") -> dict:
    a = np.asarray(values, dtype=float)
    if a.size == 0:
        return {"n": 0}
    q = np.percentile(a, [1, 5, 50, 95, 99])
    return {"n": int(a.size), "min": float(a.min()), "p1": float(q[0]), "p5": float(q[1]), "median": float(q[2]),
            "p95": float(q[3]), "p99": float(q[4]), "max": float(a.max())}


def save(out: Path, phase: str, data) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{phase}.json").write_text(json.dumps(data, indent=1))


def load(out: Path, phase: str):
    return json.loads((out / f"{phase}.json").read_text())


def combos(seeds, levels):
    return [(s, l) for s in seeds for l in levels]


def rdir(out: Path, s: int, l: int) -> Path:
    return out / "render" / f"s{s}_l{l}"


def refdir(out: Path, s: int, l: int) -> Path:
    return out / "reference" / f"s{s}_l{l}"


def fake_request(seed: int, level: int, frames: int) -> Request:
    return Request(requester="R", request_id=1, mint="M", scene_type=0, seed=seed, num_frames=frames, width=W, height=H, occlusion_level=level,
                   price_per_frame=1, escrow_amount=frames, stake_amount=1, claim_duration=1, commit_duration=1, challenge_window_secs=1,
                   node="N", status="Challenged", created_at=0, claimed_at=0, claim_deadline=1, commit_deadline=1,
                   challenge_window_end=10**12, bump=255)


# ---------------------------------------------------------------- phases

def phase_render(args, out: Path):
    blender = find_blender()
    info = {"blender": subprocess.run([blender, "--version"], capture_output=True, text=True).stdout.splitlines()[0],
            "platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(), "samples": args.samples}
    times = {}
    for s, l in combos(args.seeds, args.levels):
        d = rdir(out, s, l)
        if all((frame_dir(d, i) / "annotations.json").exists() for i in range(args.frames)):
            print(f"render s{s} l{l}: exists, skipped", flush=True)
            continue
        t = time.time()
        render_frames(s, list(range(args.frames)), d, l, W, H, samples=args.samples)
        times[f"s{s}_l{l}"] = time.time() - t
        print(f"render s{s} l{l}: {args.frames} frames in {times[f's{s}_l{l}']:.1f}s", flush=True)
    save(out, "render", {"environment": info, "seconds": times})


def phase_seg(args, out: Path):
    frames_out = []
    for s, l in combos(args.seeds, args.levels):
        ref = refdir(out, s, l)
        t = time.time()
        reference = render_seg_only(s, list(range(args.frames)), ref, l, W, H)
        dt = time.time() - t
        scene_objs = {}
        for i in range(args.frames):
            honest_path = frame_dir(rdir(out, s, l), i) / "seg.png"
            honest = read_seg(honest_path)
            scene = generate_scene(s, i, l, W, H)
            classes = {o["id"]: o["class"] for o in scene["objects"]}
            c = compare_seg(honest, reference[i])
            frames_out.append({
                "seed": s, "level": l, "frame": i, "global_agreement": c.global_agreement, "mismatched_pixels": c.mismatched_pixels,
                "byte_identical": honest_path.read_bytes() == (frame_dir(ref, i) / "seg.png").read_bytes(),
                "objects": [{"id": o.object_id, "class": classes.get(o.object_id), "revealed_px": o.revealed_pixels,
                             "reference_px": o.reference_pixels, "iou": o.iou} for o in c.objects.values()],
            })
        print(f"seg s{s} l{l}: re-rendered {args.frames} frames in {dt:.2f}s; mismatched pixels {sum(f['mismatched_pixels'] for f in frames_out[-args.frames:])}", flush=True)
    save(out, "seg", frames_out)


def build_reveals(out: Path, s: int, l: int, frames: int):
    """Real files -> the exact JSON the node's reveal server would return, plus the on-chain-like request and submission."""
    d = rdir(out, s, l)
    job = Job(d, {"request": f"calib_s{s}_l{l}", "num_frames": frames, "state": "committed"})
    cache = build_leaf_cache(job)
    write_leaf_cache(job, cache)
    sub = Submission(request=job.request, node="N", merkle_root=cache["merkle_root"], frame_count=frames, commit_slot=1, sample_seed="ab" * 32,
                     sample_slot=2, sample_count=min(7, frames), verdict="Pending", bump=255)
    return job, fake_request(s, l, frames), sub


def phase_checks(args, out: Path):
    seen = SeenImages()
    failures, per_object, c7 = [], [], []
    n_frames = 0
    for s, l in combos(args.seeds, args.levels):
        job, req, sub = build_reveals(out, s, l, args.frames)
        reference = {i: read_seg(frame_dir(refdir(out, s, l), i) / "seg.png") for i in range(args.frames)}
        ctx = VerifyContext(job.request, req, sub, DEFAULT_THRESHOLDS, seen, reference_seg=reference)
        for i in range(args.frames):
            n_frames += 1
            for r in run_checks(Reveal.parse(reveal_payload(job, i)), i, ctx):
                if not r.passed:
                    failures.append({"seed": s, "level": l, "frame": i, "check": r.name, "detail": r.detail})
                if r.name == "annotation_plausibility":
                    for o in r.metrics.get("objects", []):
                        per_object.append({"seed": s, "level": l, "frame": i, **o})
                if r.name == "seg_rerender":
                    c7.append({"seed": s, "level": l, "frame": i, **r.metrics})
        print(f"checks s{s} l{l}: done ({len(failures)} failures so far)", flush=True)
    save(out, "checks", {"frames": n_frames, "failures": failures, "check4_objects": per_object, "check7": c7})


def phase_cheats(args, out: Path):
    rows = []
    for s, l in combos(args.seeds, args.levels):
        for i in range(args.frames):
            reference = read_seg(frame_dir(refdir(out, s, l), i) / "seg.png")
            scene = generate_scene(s, i, l, W, H)
            classes = {o["id"]: o["class"] for o in scene["objects"]}
            for name, seg in (("no_occlusion", paint_silhouettes(scene)), ("painter", paint_silhouettes(scene, painter_order(scene)))):
                c = compare_seg(seg, reference)
                rows.append({"seed": s, "level": l, "frame": i, "cheat": name, "global_agreement": c.global_agreement,
                             "mismatched_pixels": c.mismatched_pixels,
                             "objects": [{"id": o.object_id, "class": classes.get(o.object_id), "revealed_px": o.revealed_pixels,
                                          "reference_px": o.reference_pixels, "iou": o.iou} for o in c.objects.values()]})
        print(f"cheats s{s} l{l}: done", flush=True)
    save(out, "cheats", rows)


def worst_iou(objects, p_min):
    judged = [o["iou"] for o in objects if max(o["revealed_px"], o["reference_px"]) >= p_min]
    return min(judged) if judged else 1.0


def phase_gpu(args, out: Path):
    rows = []
    for level in args.levels:
        s = args.seeds[0]
        d = out / "gpu" / f"s{s}_l{level}"
        try:
            render_frames(s, [0, 1, 2], d, level, W, H, samples=4, gpu=True)
        except Exception as e:  # noqa: BLE001
            save(out, "gpu", {"error": str(e)[-800:], "rows": rows})
            print("GPU render failed:", str(e)[-300:])
            return
        reference = render_seg_only(s, [0, 1, 2], out / "gpu_ref" / f"s{s}_l{level}", level, W, H)
        scene_cls = {i: {o["id"]: o["class"] for o in generate_scene(s, i, level, W, H)["objects"]} for i in range(3)}
        for i in range(3):
            c = compare_seg(read_seg(frame_dir(d, i) / "seg.png"), reference[i])
            rows.append({"seed": s, "level": level, "frame": i, "global_agreement": c.global_agreement, "mismatched_pixels": c.mismatched_pixels,
                         "objects": [{"id": o.object_id, "class": scene_cls[i].get(o.object_id), "revealed_px": o.revealed_pixels,
                                      "reference_px": o.reference_pixels, "iou": o.iou} for o in c.objects.values()]})
        print(f"gpu level {level}: done", flush=True)
    save(out, "gpu", {"rows": rows})


def phase_timing(args, out: Path):
    import threading
    from porf.chain.client import ChainClient, CreateRequestParams
    from porf.node.client import Node, NodeConfig
    from porf.verifier.client import Verifier, VerifierConfig

    env = json.loads((REPO / ".localnet" / "env.json").read_text())
    chain = ChainClient(rpc_url=env["rpc_url"])
    seed, level, frames = 42, 3, 20
    src = out / "timing" / "render20"
    if not all((frame_dir(src, i) / "annotations.json").exists() for i in range(frames)):
        t = time.time()
        render_frames(seed, list(range(frames)), src, level, W, H, samples=args.samples)
        print(f"timing: rendered the 20 source frames in {time.time() - t:.0f}s", flush=True)

    def trial(tag: str, n: int):
        results = []
        for k in range(n):
            rid = int(time.time() * 1000) + k
            params = CreateRequestParams(request_id=rid, seed=seed, num_frames=frames, price_per_frame=500_000, claim_duration=900,
                                         commit_duration=900, challenge_window_secs=600, occlusion_level=level)
            address = chain.create_request(env["keys"]["requester"], params)["request"]
            tdir = out / "timing" / f"{tag}{k}"
            shutil.rmtree(tdir, ignore_errors=True)
            node = Node(NodeConfig(keypair=env["keys"]["node"], out_dir=tdir / "node", samples=4, only_request=address, port=0, announce_file=None), chain)
            job = node._claim(address, chain.get_request(request=address).request)
            for i in range(frames):                                    # reuse the already-rendered files: the node resumes at "rendered"
                shutil.copytree(frame_dir(src, i), job.frame_dir(i))
            write_leaf_cache(job, build_leaf_cache(job))
            job.update(state="rendered")
            assert node.step() == [f"committed {address}"]
            server = node.start_server()
            v = Verifier(VerifierConfig(keypair=env["keys"]["verifier"], out_dir=tdir / "verifier", only_request=address, reveal_url=server.url,
                                        poll_interval=0.5), chain=chain)
            t = time.time()
            v.step()
            wall = time.time() - t
            rep = json.loads((tdir / "verifier" / address / "report.json").read_text())
            server.stop()
            results.append({"verdict": rep["verdict"], "wall_s": wall, **rep["timing"], "rerender_attempts": rep["reference_render"]["attempts"]})
            print(f"timing {tag}{k}: verdict {rep['verdict']}, total {rep['timing']['total_s']:.1f}s ({rep['timing']})", flush=True)
        return results

    idle = trial("idle", args.timing_trials)
    load = subprocess.Popen([sys.executable, "-m", "porf.render.runner", "--seed", "7", "--frames", "60", "--samples", "64", "--out", str(out / "timing" / "load")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(REPO))
    time.sleep(5)
    try:
        loaded = trial("loaded", args.timing_trials)
    finally:
        load.terminate()
    save(out, "timing", {"idle": idle, "loaded": loaded, "sample_size": 7, "frames_in_request": frames})


# ---------------------------------------------------------------- summary

def summarize(out: Path, args):
    print("\n" + "=" * 100 + "\nCALIBRATION SUMMARY\n" + "=" * 100)
    if (out / "render.json").exists():
        r = load(out, "render")
        print("environment:", json.dumps(r["environment"]))
    if (out / "seg.json").exists():
        seg = load(out, "seg")
        agree = [f["global_agreement"] for f in seg]
        print(f"\n[check 7] HONEST frames: {len(seg)}  byte-identical seg.png: {sum(f['byte_identical'] for f in seg)}/{len(seg)}"
              f"  frames with any differing pixel: {sum(1 for f in seg if f['mismatched_pixels'])}")
        print("  global agreement:", json.dumps(pct(agree)))
        objs = [o for f in seg for o in f["objects"]]
        print(f"  objects compared: {len(objs)}; per-object IoU:", json.dumps(pct([o["iou"] for o in objs])))
        for p in P_MINS[1:]:
            sub = [o["iou"] for o in objs if max(o["revealed_px"], o["reference_px"]) >= p]
            print(f"    objects with >= {p:>3} px in either image: n={len(sub):>5}  min IoU {min(sub) if sub else float('nan'):.6f}")
        for lvl in sorted({f["level"] for f in seg}):
            sub = [f["global_agreement"] for f in seg if f["level"] == lvl]
            print(f"  level {lvl}: frames {len(sub)}  min global agreement {min(sub):.6f}")
    if (out / "checks.json").exists():
        c = load(out, "checks")
        print(f"\n[all checks, placeholders] honest frames {c['frames']}: failures {len(c['failures'])}")
        for f in c["failures"][:10]:
            print("   ", f)
        by = {}
        for o in c["check4_objects"]:
            by.setdefault(o["class"], []).append(o)
        print("\n[check 4] mask box vs projected box, per class (overshoot_px > 0 means the mask box sticks out of the projected box)")
        for cls in sorted(by):
            ov = [o["overshoot_px"] for o in by[cls]]
            slack = [2.0 - v for v in ov]
            ratio = [o["iou"] / o["visible_fraction"] for o in by[cls] if o["visible_fraction"] > 0]
            print(f"  {cls:9s} n={len(ov):5d}  overshoot px: max {max(ov):.3f} p99 {np.percentile(ov, 99):.3f} p95 {np.percentile(ov, 95):.3f}"
                  f" | slack vs 2.0px: min {min(slack):.3f} p1 {np.percentile(slack, 1):.3f} p5 {np.percentile(slack, 5):.3f}"
                  f" | IoU/visible_fraction: min {min(ratio):.3f} p1 {np.percentile(ratio, 1):.3f} p5 {np.percentile(ratio, 5):.3f}")
        allov = [o["overshoot_px"] for o in c["check4_objects"]]
        print(f"  ALL       n={len(allov):5d}  overshoot px: max {max(allov):.3f} p99 {np.percentile(allov, 99):.3f} p95 {np.percentile(allov, 95):.3f}")
    if (out / "cheats.json").exists() and (out / "seg.json").exists():
        ch = load(out, "cheats")
        seg = load(out, "seg")
        print("\n[check 7] FRAUD vs honest, per occlusion level (global agreement; worst per-object IoU at the pixel floors)")
        for cheat in ("no_occlusion", "painter"):
            print(f"  --- {cheat}")
            for lvl in sorted({f["level"] for f in ch}):
                rows = [f for f in ch if f["cheat"] == cheat and f["level"] == lvl]
                hon = [f for f in seg if f["level"] == lvl]
                ga = [f["global_agreement"] for f in rows]
                line = f"   level {lvl}: n={len(rows)}  agreement min {min(ga):.4f} median {np.median(ga):.4f} max {max(ga):.4f}"
                line += "  | worst IoU (>=50px) median " + f"{np.median([worst_iou(f['objects'], 50) for f in rows]):.3f}"
                line += f" | honest min agreement {min(f['global_agreement'] for f in hon):.6f}"
                print(line)
        print("\n[check 7] rule grid: (G, J, P_min) -> honest false positives / frames caught, per cheat and level")
        for G, J, P in [(0.99, 0.5, 50), (0.995, 0.5, 50), (0.999, 0.9, 50), (0.9995, 0.9, 50), (0.9999, 0.95, 100), (0.99, 0.9, 50), (0.999, 0.99, 100)]:
            fp = sum(1 for f in seg if f["global_agreement"] < G or worst_iou(f["objects"], P) < J)
            row = f"  G={G} J={J} Pmin={P}: honest FP {fp}/{len(seg)} |"
            for cheat in ("no_occlusion", "painter"):
                for lvl in sorted({f["level"] for f in ch}):
                    rows = [f for f in ch if f["cheat"] == cheat and f["level"] == lvl]
                    caught = sum(1 for f in rows if f["global_agreement"] < G or worst_iou(f["objects"], P) < J)
                    row += f" {cheat[:4]}-L{lvl} {caught}/{len(rows)}"
            print(row)
    if (out / "gpu.json").exists():
        g = load(out, "gpu")
        print("\n[GPU (Metal) vs CPU re-render]", g.get("error", ""))
        rows = g.get("rows", [])
        if rows:
            print("  frames", len(rows), "min global agreement", min(r["global_agreement"] for r in rows),
                  "total mismatched px", sum(r["mismatched_pixels"] for r in rows),
                  "worst IoU(>=50px)", min(worst_iou(r["objects"], 50) for r in rows))
    if (out / "timing.json").exists():
        t = load(out, "timing")
        print("\n[timing] verification of a 20-frame request (sample of 7), seconds")
        for kind in ("idle", "loaded"):
            rows = t[kind]
            print(f"  {kind}: n={len(rows)} verdicts {sorted({r['verdict'] for r in rows})}")
            for key in ("open_challenge_s", "fetch_s", "rerender_s", "checks_s", "verdict_s", "total_s"):
                v = [r[key] for r in rows]
                print(f"    {key:17s} min {min(v):6.2f}  mean {np.mean(v):6.2f}  max {max(v):6.2f}")
        worst = max(r["total_s"] for k in ("idle", "loaded") for r in t[k])
        print(f"  worst total over all trials: {worst:.1f}s  ->  3x = {3 * worst:.0f}s  ->  recommended window = max(30, ceil(3x)) = {max(30, math.ceil(3 * worst))}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out/calibration")
    ap.add_argument("--phases", default="render,seg,checks,cheats")
    ap.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS)
    ap.add_argument("--levels", type=int, nargs="+", default=LEVELS)
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--samples", type=int, default=64, help="production default for rgb quality (does not affect seg)")
    ap.add_argument("--timing-trials", type=int, default=10)
    args = ap.parse_args()
    out = (REPO / args.out).resolve()
    phases = {"render": phase_render, "seg": phase_seg, "checks": phase_checks, "cheats": phase_cheats, "timing": phase_timing, "gpu": phase_gpu}
    for name in args.phases.split(","):
        if name in phases:
            t = time.time()
            print(f"\n=== phase {name}", flush=True)
            phases[name](args, out)
            print(f"=== phase {name} finished in {time.time() - t:.0f}s", flush=True)
    summarize(out, args)


if __name__ == "__main__":
    main()
