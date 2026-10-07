"""The node: polls for Open requests, stakes and claims one, renders it, commits the Merkle root, serves reveals.

    python -m porf.node.client --keypair keys/node.json [--out out] [--poll 5] [--port 8787] [--samples 64]
                               [--only-request ADDR] [--cheat MODE [--fraction F]]

Chain access goes only through porf.chain.client. Job state lives in <out>/<request_id>/ (porf.node.job), so a
restart resumes where it stopped: after claim -> render; after render -> commit; after commit -> just serve.
Spec: docs/PROTOCOL.md section 16.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..chain.client import REPO_ROOT, ChainClient, ChainError, Request
from ..render.runner import DEFAULT_SAMPLES, render_frames
from . import cheats
from .job import Job, build_leaf_cache, load_leaf_cache, write_leaf_cache
from .server import RevealServer

log = logging.getLogger("porf.node")

MAX_ATTEMPTS = 3  # transient failures of one stage before a job is marked failed
DEFAULT_ANNOUNCE = REPO_ROOT / ".localnet" / "reveal.json"


@dataclass
class NodeConfig:
    keypair: str
    out_dir: Path = Path("out")
    poll_interval: float = 5.0
    samples: int = DEFAULT_SAMPLES
    gpu: bool = False
    only_request: str | None = None   # claim only this request (tests, demos on a shared validator)
    only_requester: str | None = None
    max_frames: int = 1024
    cheat: str | None = None
    cheat_fraction: float | None = None   # share of frames the cheat corrupts (default 0.5); only with cheat
    host: str = "127.0.0.1"
    port: int = 8787
    announce_file: Path | None = DEFAULT_ANNOUNCE


def _is_transient(e: ChainError) -> bool:
    return e.kind == "rpc" or e.name in ("Timeout",)


class Node:
    def __init__(self, cfg: NodeConfig, chain: ChainClient | None = None):
        self.cfg = cfg
        self.out_dir = Path(cfg.out_dir).resolve()
        self.chain = chain or ChainClient()
        self.pubkey = self.chain.pubkey(cfg.keypair)
        self.server: RevealServer | None = None

    # ------------------------------------------------------------ one poll iteration

    def step(self) -> list[str]:
        """Do whatever work is available, in this order: resume local jobs, adopt a claim we lost track of, claim a new request.
        Returns short descriptions of what was done."""
        done: list[str] = []
        requests = dict(self.chain.list_requests())  # address -> Request
        known = set()
        for job in Job.scan(self.out_dir):
            known.add(job.request)
            if job.state in ("claimed", "rendered"):
                done += self._advance(job, requests.get(job.request))
        # a claim that landed on chain but whose job.json was never written (crash between the two)
        for address, req in requests.items():
            if address not in known and req.status == "Claimed" and req.node == self.pubkey and self._wanted(address, req, adopting=True):
                log.info("adopting request %s claimed earlier by this node (no local job)", address)
                Job.guard(self.out_dir, address, req.request_id)
                job = Job.create(self.out_dir, address, req, adopted=True)
                done.append(f"adopted {address}")
                done += self._advance(job, req)
        if not done or all(d.startswith("skipped") for d in done):
            for address, req in sorted(requests.items(), key=lambda kv: kv[1].created_at):
                if req.status == "Open" and self._wanted(address, req):
                    claimed = self._claim(address, req)
                    if claimed:
                        done.append(f"claimed {address}")
                        # the snapshot above predates the claim: use the request as it is on chain now
                        done += self._advance(claimed, self.chain.get_request(request=address).request)
                        break
        return done

    def _wanted(self, address: str, req: Request, adopting: bool = False) -> bool:
        c = self.cfg
        if c.only_request and address != c.only_request:
            return False
        if c.only_requester and req.requester != c.only_requester:
            return False
        if req.scene_type != 0 or req.num_frames > c.max_frames:
            return False
        return True

    # ------------------------------------------------------------ claim

    def _claim(self, address: str, req: Request) -> Job | None:
        Job.guard(self.out_dir, address, req.request_id)  # before spending a stake
        try:
            balance = self.chain.token_balance(owner=self.pubkey, mint=req.mint)
        except ChainError as e:
            log.warning("cannot read my token balance (%s); not claiming", e)
            return None
        if balance < req.stake_amount:
            log.info("skip %s: stake %d exceeds my balance %d", address, req.stake_amount, balance)
            return None
        try:
            out = self.chain.stake_and_claim(self.cfg.keypair, request=address)
        except ChainError as e:
            if e.name in ("InvalidStatus", "ClaimDeadlinePassed"):
                log.info("lost the race for %s: %s", address, e.name)
                return None
            raise
        log.info("claimed %s (request_id %d, %d frames, stake %d), tx %s", address, req.request_id, req.num_frames, req.stake_amount, out["signature"])
        return Job.create(self.out_dir, address, req, claim_signature=out["signature"])

    # ------------------------------------------------------------ advance one job through render -> commit

    def _advance(self, job: Job, req: Request | None) -> list[str]:
        if req is None:
            log.warning("job %s: request not found on chain; leaving it", job.request)
            return [f"skipped {job.request}: not on chain"]
        if req.node != self.pubkey:
            req = self.chain.get_request(request=job.request).request  # never fail a job on a stale snapshot
            if req.node != self.pubkey:
                job.update(state="failed", error=f"on-chain node is {req.node}, not this node")
                return [f"failed {job.request}: not my claim"]
        done: list[str] = []
        try:
            if job.state == "claimed":
                if req.status != "Claimed":
                    job.update(state="failed", error=f"request is {req.status}; nothing to render for")
                    return [f"failed {job.request}: status {req.status}"]
                self._render(job)
                done.append(f"rendered {job.request}")
            if job.state == "rendered":
                self._commit(job, req)
                done.append(f"committed {job.request}" if job.state == "committed" else f"failed {job.request}")
        except ChainError as e:
            if _is_transient(e):
                log.warning("job %s: transient chain error, will retry: %s", job.request, e)
                return done + [f"skipped {job.request}: transient {e.name}"]
            return done + self._fail(job, e)
        except Exception as e:  # render failures etc.
            log.exception("job %s failed at state %s", job.request, job.state)
            return done + self._fail(job, e)
        return done

    def _fail(self, job: Job, e: Exception) -> list[str]:
        attempts = job.data.get("attempts", 0) + 1
        if attempts >= MAX_ATTEMPTS or isinstance(e, ChainError):
            job.update(state="failed", attempts=attempts, error=f"{type(e).__name__}: {e}")
            log.error("job %s marked failed: %s", job.request, e)
            return [f"failed {job.request}: {e}"]
        job.update(attempts=attempts, error=f"{type(e).__name__}: {e}")
        return [f"skipped {job.request}: attempt {attempts} failed"]

    def _render(self, job: Job) -> None:
        d = job.data
        seed = d["seed"]
        log.info("rendering %d frames of %s (seed %d, occlusion %d, %dx%d, %d samples)", d["num_frames"], job.request,
                 seed, d["occlusion_level"], d["width"], d["height"], self.cfg.samples)
        t0 = time.time()
        for plan_seed, frames in cheats.render_plan(self.cfg.cheat, seed, d["num_frames"], self.cfg.cheat_fraction):   # honest: one entry
            render_frames(plan_seed, frames, job.dir, d["occlusion_level"], d["width"], d["height"], self.cfg.samples, self.cfg.gpu)
        cheats.after_render(self.cfg.cheat, job.dir, d["num_frames"], seed, self.cfg.cheat_fraction, d["occlusion_level"], d["width"], d["height"])
        cache = build_leaf_cache(job)  # after the cheat hook: the root commits to whatever is on disk
        write_leaf_cache(job, cache)
        job.update(state="rendered", merkle_root=cache["merkle_root"], rendered_at=time.time())
        log.info("rendered %s in %.1fs, root %s", job.request, time.time() - t0, cache["merkle_root"])

    def _commit(self, job: Job, req: Request) -> None:
        if not (job.dir / "leaves.json").exists():  # crashed after rendering but before the cache was written
            if job.render_complete():
                write_leaf_cache(job, build_leaf_cache(job))
            else:
                log.warning("job %s: rendered state but files missing; rendering again", job.request)
                job.update(state="claimed")
                self._render(job)
        cache = load_leaf_cache(job)
        root = bytes.fromhex(cache["merkle_root"])
        if req.status == "Claimed":
            try:
                out = self.chain.submit_commitment(self.cfg.keypair, root, job.num_frames, request=job.request)
                log.info("committed %s root %s, tx %s", job.request, cache["merkle_root"], out["signature"])
                job.update(commit_signature=out["signature"])
            except ChainError as e:
                if _is_transient(e) or not self._already_committed(job, cache):
                    raise
                log.info("job %s was already committed on chain; reconciling", job.request)
        elif not self._already_committed(job, cache):
            job.update(state="failed", error=f"request is {req.status} and no matching commitment found")
            return
        sub = self.chain.get_submission(request=job.request)
        if sub.merkle_root != cache["merkle_root"] or sub.node != self.pubkey:
            job.update(state="failed", error="on-chain commitment differs from the local leaves")
            return
        job.update(state="committed", commit_slot=sub.commit_slot, committed_at=time.time())

    def _already_committed(self, job: Job, cache: dict) -> bool:
        try:
            return self.chain.get_submission(request=job.request).merkle_root == cache["merkle_root"]
        except ChainError:
            return False

    # ------------------------------------------------------------ long-running mode

    def start_server(self) -> RevealServer:
        self.server = RevealServer(self.out_dir, self.cfg.host, self.cfg.port).start()
        if self.cfg.announce_file:
            # SHORTCUT: the node's reveal URL is not on-chain. It is written to a local file (and the verifier is
            # pointed at it via PORF_REVEAL_URL or this file). See docs/LIMITATIONS.md.
            self.cfg.announce_file.parent.mkdir(parents=True, exist_ok=True)
            self.cfg.announce_file.write_text(json.dumps({"url": self.server.url, "node": self.pubkey, "started_at": time.time()}, indent=2))
        return self.server

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        cheats.announce(self.cfg.cheat, self.cfg.cheat_fraction)
        mint = self.chain.get_config().mint
        self.chain.create_token_account(self.cfg.keypair, mint)  # idempotent: make sure the node can receive stake and payout
        self.start_server()
        log.info("node %s polling every %.1fs, out dir %s", self.pubkey, self.cfg.poll_interval, self.out_dir)
        try:
            while not stop.is_set():
                try:
                    for line in self.step():
                        log.info("step: %s", line)
                except ChainError as e:
                    log.warning("poll failed: %s", e)
                except Exception:
                    log.exception("unexpected error in poll loop")
                stop.wait(self.cfg.poll_interval)
        finally:
            if self.server:
                self.server.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Proof-of-Render node")
    ap.add_argument("--keypair", required=True, help="node keypair file (relative paths resolve against the repo root)")
    ap.add_argument("--out", default="out", help="job directory (default: out)")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between polls (default 5)")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="render samples for rgb.png")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787, help="reveal server port (0 = pick a free one)")
    ap.add_argument("--only-request", help="claim only this request address")
    ap.add_argument("--only-requester", help="claim only requests from this requester")
    ap.add_argument("--max-frames", type=int, default=1024)
    ap.add_argument("--announce-file", default=str(DEFAULT_ANNOUNCE), help="where to write the reveal URL for the verifier ('' to disable)")
    ap.add_argument("--cheat", choices=cheats.CHEAT_MODES, help="demo cheat mode (test tooling: commits fraudulent data)")
    ap.add_argument("--fraction", type=float, help="share of frames the cheat corrupts, in (0, 1] (default 0.5; needs --cheat)")
    args = ap.parse_args(argv)
    if args.fraction is not None and not args.cheat:
        ap.error("--fraction needs --cheat")
    if args.fraction is not None and not 0.0 < args.fraction <= 1.0:
        ap.error("--fraction must be in (0, 1]")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = NodeConfig(
        keypair=args.keypair, out_dir=Path(args.out), poll_interval=args.poll, samples=args.samples, gpu=args.gpu,
        only_request=args.only_request, only_requester=args.only_requester, max_frames=args.max_frames, cheat=args.cheat, cheat_fraction=args.fraction,
        host=args.host, port=args.port, announce_file=Path(args.announce_file) if args.announce_file else None,
    )
    try:
        Node(cfg).run()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
