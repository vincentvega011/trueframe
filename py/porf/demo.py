"""Demo orchestrator behind scripts/demo_honest.sh, demo_cheat.sh and demo_timeout.sh. Drives the REAL pieces (the chain CLI, the node
and the verifier as separate processes, Blender) and prints a numbered, camera-friendly account. No stack traces on the way:
a failure prints one line and where the logs are.

    python -m porf.demo honest  [--cluster localnet]
    python -m porf.demo cheat   --mode reuse_frames [--fraction 0.8]
    python -m porf.demo timeout

Needs the chain to be set up first (scripts/setup.sh)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .chain.client import devnet_rpc_url, rpc_host, REPO_ROOT, ChainClient, ChainError, CreateRequestParams
from .node import cheats
from .verifier.sampler import sample_indices

DEMO_SEED = 5150         # the demo request's scene seed; chosen by looking at all 20 level-3 frames (docs/PROTOCOL.md section 18)
DEMO_FRAMES, DEMO_OCCLUSION = 20, 3
DEMO_CHEAT_FRACTION = 0.8   # the demo cheat corrupts 80% of frames (the protocol default is 0.5); demo_cheat.sh passes it
USDC = 1_000_000
PY = sys.executable


class DemoError(Exception):
    pass


def usdc(n: int) -> str:
    return f"{n / USDC:,.2f} USDC"


class Demo:
    def __init__(self, cluster: str, label: str, steps: int):
        env_file = REPO_ROOT / (".localnet" if cluster == "localnet" else ".devnet") / "env.json"
        if not env_file.exists():
            raise DemoError(f"no {cluster} environment: run `scripts/setup.sh --cluster {cluster}` first")
        self.env = json.loads(env_file.read_text())
        self.cluster = cluster
        # devnet: the URL (possibly with an API key) comes from the environment and is never stored in env.json or printed
        self.rpc_url = devnet_rpc_url() if cluster == "devnet" else self.env["rpc_url"]
        self.node_poll, self.verifier_poll = ("8", "4") if cluster == "devnet" else ("1", "1")   # public devnet RPC is rate-limited
        self.wait_sleep = 3.0 if cluster == "devnet" else 1.0
        self.c = ChainClient(rpc_url=self.rpc_url)
        try:
            self.c.get_config()
        except ChainError as e:
            raise DemoError(f"cannot reach the {cluster} chain at {rpc_host(self.rpc_url)} ({e.name}); run `scripts/setup.sh --cluster {cluster}`") from None
        self.keys = self.env["keys"]
        self.tok = self.env["token_accounts"]
        self.steps, self.n = steps, 0
        self.out = REPO_ROOT / "out" / "demo" / f"{label}-{time.strftime('%H%M%S')}"
        self.out.mkdir(parents=True, exist_ok=True)
        self.procs: list[subprocess.Popen] = []

    # ---- output
    def step(self, title: str) -> None:
        self.n += 1
        print(f"\n[{self.n}/{self.steps}] {title}", flush=True)

    @staticmethod
    def say(text: str) -> None:
        print(f"      {text}", flush=True)

    # ---- chain
    def balances(self) -> dict[str, int]:
        return {"requester": self.c.token_balance(token_account=self.tok["requester"]), "node": self.c.token_balance(token_account=self.tok["node"])}

    def status(self, address: str) -> str:
        return self.c.get_request(request=address).request.status

    def wait_status(self, address: str, wanted: set[str], timeout: float, watch=None, proc: subprocess.Popen | None = None, log: Path | None = None) -> str:
        end, last_tick = time.time() + timeout, 0.0
        while time.time() < end:
            status = self.status(address)
            if status in wanted:
                return status
            if proc is not None and proc.poll() is not None:
                raise DemoError(f"a demo process exited early (code {proc.returncode}); see {log}")
            if watch and time.time() - last_tick > 2:
                watch()
                last_tick = time.time()
            time.sleep(self.wait_sleep)
        raise DemoError(f"timed out waiting for {sorted(wanted)} (still {self.status(address)})")

    def create_request(self, seed: int, frames: int, occlusion: int, claim_duration=900, commit_duration=900, price=USDC) -> tuple[str, CreateRequestParams]:
        params = CreateRequestParams(request_id=int(time.time() * 1000), seed=seed, num_frames=frames, price_per_frame=price, claim_duration=claim_duration,
                                     commit_duration=commit_duration, occlusion_level=occlusion)
        res = self.c.create_request(self.keys["requester"], params)
        return res["request"], params

    # ---- processes
    def spawn(self, name: str, args: list[str]) -> tuple[subprocess.Popen, Path]:
        log = self.out / f"{name}.log"
        env = dict(os.environ, PORF_RPC_URL=self.rpc_url, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([PY, "-m", *args], cwd=str(REPO_ROOT), env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc, log

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def show_balances(d: Demo, before: dict[str, int], after: dict[str, int]) -> None:
    for who in ("requester", "node"):
        delta = after[who] - before[who]
        d.say(f"{who:<9} {usdc(before[who]):>16}  ->  {usdc(after[who]):>16}   ({'+' if delta >= 0 else '-'}{usdc(abs(delta))})")


# ---------------------------------------------------------------------------------------------------- honest and cheat

def run_job(cluster: str, mode: str | None, fraction: float | None, label: str) -> int:
    d = Demo(cluster, label, steps=7)
    try:
        before = d.balances()
        d.step(f"A requester asks for {DEMO_FRAMES} frames of warehouse scenes with heavy occlusion (level {DEMO_OCCLUSION}) and escrows the payment")
        address, params = d.create_request(DEMO_SEED, DEMO_FRAMES, DEMO_OCCLUSION)
        view = d.c.get_request(request=address).request
        d.say(f"request {address}")
        d.say(f"seed {DEMO_SEED}, {DEMO_FRAMES} frames, {params.width}x{params.height}, occlusion {DEMO_OCCLUSION}")
        d.say(f"escrowed {usdc(view.escrow_amount)}; a node must stake {usdc(view.stake_amount)}; challenge window {params.challenge_window_secs} s")

        node_args = ["porf.node.client", "--keypair", d.keys["node"], "--out", str(d.out / "node"), "--poll", d.node_poll, "--port", "0", "--only-request", address,
                     "--announce-file", str(d.out / "reveal.json")]
        if mode:
            node_args += ["--cheat", mode, "--fraction", str(fraction if fraction is not None else cheats.DEFAULT_FRACTION)]
        d.step("An honest node stakes and claims the job" if not mode else f"A CHEATING node ({mode}) stakes and claims the job")
        node, node_log = d.spawn("node", node_args)
        d.wait_status(address, {"Claimed", "Committed"}, 90, proc=node, log=node_log)
        d.say(f"node staked {usdc(view.stake_amount)}; request status: Claimed")
        if mode:
            bad = cheats.bad_frames(mode, DEMO_SEED, DEMO_FRAMES, fraction)
            d.say(f"(this node will corrupt {len(bad)} of {DEMO_FRAMES} frames: {bad})")

        d.step(f"The node renders {DEMO_FRAMES} frames with headless Blender (RGB, depth, segmentation, boxes)")
        job_dir = d.out / "node" / str(params.request_id)
        seen = {"n": -1}

        def progress():
            n = len(list(job_dir.glob("frame_*/rgb.png")))
            if n != seen["n"]:
                seen["n"] = n
                d.say(f"{n}/{DEMO_FRAMES} frames rendered")
        d.wait_status(address, {"Committed"}, 900, watch=progress, proc=node, log=node_log)

        d.step("The node commits the Merkle root of all frame records on-chain")
        sub = d.c.get_submission(request=address)
        d.say(f"merkle root {sub.merkle_root}")
        d.say(f"{sub.frame_count} frames committed at slot {sub.commit_slot}; request status: Committed")

        d.step("The verifier draws a random sample from the chain's SlotHashes and re-checks those frames")
        reveal = json.loads((d.out / "reveal.json").read_text())["url"]
        ver, ver_log = d.spawn("verifier", ["porf.verifier.client", "--keypair", d.keys["verifier"], "--out", str(d.out / "verifier"), "--poll", d.verifier_poll,
                                            "--only-request", address, "--reveal-url", reveal, "--once"])
        d.wait_status(address, {"Challenged", "Verified", "Paid", "Slashed"}, 120, proc=ver, log=ver_log)
        sub = d.c.get_submission(request=address)
        sample = sample_indices(bytes.fromhex(sub.sample_seed), DEMO_FRAMES)
        d.say(f"sample seed {sub.sample_seed[:16]}... (slot {sub.sample_slot})")
        d.say(f"sampled frames: {sample}")
        if mode:
            d.say(f"of these, corrupted by the cheat: {[i for i in sample if i in bad]}")
        d.say("checks per frame: Merkle proof, scene regenerated from the seed, hashes, boxes vs projection, image sanity, segmentation, Blender re-render")

        d.step("Verdict")
        d.wait_status(address, {"Verified", "Paid", "Slashed"}, 300, proc=ver, log=ver_log)
        report = json.loads((d.out / "verifier" / address / "report.json").read_text())
        print_report(d, report)

        d.step("Settlement")
        if report["verdict"] == "Pass":
            d.say(f"verdict Pass: waiting for the {params.challenge_window_secs} s challenge window to close before paying the node ...")
        final = d.wait_status(address, {"Paid", "Slashed"}, 300, proc=ver, log=ver_log)
        after = d.balances()
        out = report.get("outcome") or {}
        if final == "Slashed":
            d.say(f"SLASHED: the node's stake of {usdc(view.stake_amount)} is forfeited; the requester gets its escrow of {usdc(view.escrow_amount)} back plus the stake")
        else:
            d.say(f"PAID: the node receives the escrow of {usdc(view.escrow_amount)}; its stake of {usdc(view.stake_amount)} is returned")
        show_balances(d, before, after)
        d.say(f"final status {final}; verifier report: {d.out / 'verifier' / address / 'report.txt'}")
        expected = "Slashed" if mode else "Paid"
        if final != expected:
            raise DemoError(f"expected the request to end {expected} but it ended {final}")
        return 0
    finally:
        d.cleanup()


def print_report(d: Demo, report: dict) -> None:
    names = {"merkle_proof": "1 proof", "scene_match": "2 scene", "hash_consistency": "3 hashes", "annotation_plausibility": "4 boxes",
             "image_sanity": "5 image", "segmentation_consistency": "6 seg", "seg_rerender": "7 re-render"}
    d.say(f"VERDICT: {report['verdict'].upper()}")
    d.say(f"{'frame':>6}  " + "  ".join(f"{n:<11}" for n in names.values()))
    failed_names: set[str] = set()
    for f in report["frames"]:
        if f["status"] != "checked":
            d.say(f"{f['index']:>6}  {f['status'].replace('_', ' ')}")
            continue
        marks = {c["name"]: c["passed"] for c in f["checks"]}
        failed_names |= {n for n, ok in marks.items() if not ok}
        d.say(f"{f['index']:>6}  " + "  ".join(f"{('pass' if marks[n] else 'FAIL'):<11}" for n in names))
    if report["verdict"] == "Fail":
        d.say("checks that failed: " + (", ".join(names[n] for n in names if n in failed_names) or "(frames could not be fetched)"))
        for r in report["verdict_reasons"][:4]:
            d.say(f"  - {r[:150]}")
    t = report.get("timing") or {}
    if t:
        d.say(f"verification took {t.get('total_s', 0):.1f} s (re-render {t.get('rerender_s', 0):.1f} s)")


# ---------------------------------------------------------------------------------------------------- timeout

def run_timeout(cluster: str) -> int:
    d = Demo(cluster, "timeout", steps=5)
    short = 8
    admin = d.keys["admin"]

    def refund_when_due(address: str) -> None:
        for _ in range(60):
            try:
                d.c.timeout_refund(admin, request=address)
                return
            except ChainError as e:
                if e.name != "DeadlineNotReached":
                    raise DemoError(f"timeout_refund failed: {e.name}") from None
                time.sleep(1)
        raise DemoError("the deadline never passed")

    try:
        start = d.balances()
        d.step(f"A requester escrows 5 frames of work with a claim deadline of {short} s; no node ever claims it")
        address, params = d.create_request(DEMO_SEED, 5, 1, claim_duration=short, commit_duration=600)
        view = d.c.get_request(request=address).request
        locked = d.balances()
        d.say(f"request {address}: escrow {usdc(view.escrow_amount)} locked (requester balance {usdc(start['requester'])} -> {usdc(locked['requester'])})")
        d.say("a refund before the deadline is refused:")
        try:
            d.c.timeout_refund(admin, request=address)
            raise DemoError("the early refund unexpectedly succeeded")
        except ChainError as e:
            d.say(f"  -> {e.name} (as expected)")

        d.step("The claim deadline passes; ANYONE may now call timeout_refund")
        refund_when_due(address)
        after = d.balances()
        d.say(f"status {d.status(address)}; the escrow is back with the requester:")
        show_balances(d, locked, after)
        if d.status(address) != "Expired" or after != start:
            raise DemoError("unexpected result after the refund of the unclaimed request")

        start2 = d.balances()
        d.step(f"A second request is claimed by a node (stake locked), then the node goes silent; commit deadline {short} s")
        address, params = d.create_request(DEMO_SEED, 5, 1, claim_duration=600, commit_duration=short)
        view = d.c.get_request(request=address).request
        d.c.stake_and_claim(d.keys["node"], request=address)
        before = d.balances()
        d.say(f"request {address}: escrow {usdc(view.escrow_amount)} locked, node stake {usdc(view.stake_amount)} locked, status {d.status(address)}")

        d.step("The commit deadline passes without a commitment; timeout_refund pays the escrow AND the node's stake to the requester")
        refund_when_due(address)
        after = d.balances()
        d.say(f"status {d.status(address)}")
        show_balances(d, before, after)
        d.say(f"net effect of the whole request: requester {'+' if after['requester'] >= start2['requester'] else '-'}{usdc(abs(after['requester'] - start2['requester']))}, "
              f"node {'+' if after['node'] >= start2['node'] else '-'}{usdc(abs(after['node'] - start2['node']))} (the forfeited stake)")
        if d.status(address) != "Expired" or after["requester"] - start2["requester"] != view.stake_amount or start2["node"] - after["node"] != view.stake_amount:
            raise DemoError("unexpected balances after the refund of the claimed request")

        d.step("A second refund of the same request is refused")
        try:
            d.c.timeout_refund(admin, request=address)
            raise DemoError("a double refund unexpectedly succeeded")
        except ChainError as e:
            d.say(f"  -> {e.name} (as expected)")
        d.say("\ntimeout refunds work in both cases, with exact balances.")
        return 0
    finally:
        d.cleanup()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Proof-of-Render demo")
    sub = ap.add_subparsers(dest="what", required=True)
    for name in ("honest", "cheat", "timeout"):
        p = sub.add_parser(name)
        p.add_argument("--cluster", choices=("localnet", "devnet"), default="localnet")
        if name == "cheat":
            p.add_argument("--mode", choices=cheats.CHEAT_MODES, required=True)
            p.add_argument("--fraction", type=float, default=DEMO_CHEAT_FRACTION)
    args = ap.parse_args(argv)
    try:
        if args.what == "honest":
            return run_job(args.cluster, None, None, "honest")
        if args.what == "cheat":
            return run_job(args.cluster, args.mode, args.fraction, f"cheat-{args.mode}")
        return run_timeout(args.cluster)
    except DemoError as e:
        print(f"\nDEMO FAILED: {e}", file=sys.stderr)
        return 1
    except ChainError as e:
        print(f"\nDEMO FAILED: chain error {e.name}: {str(e)[:200]}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
