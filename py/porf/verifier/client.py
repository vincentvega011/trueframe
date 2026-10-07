"""The verifier: opens the challenge on a committed request, fetches the sampled frames from the node, runs the checks,
posts the verdict, then slashes (Fail) or pays out after the window (Pass). Chain access only through porf.chain.client.

    python -m porf.verifier.client --keypair keys/verifier.json [--out out/verifier] [--poll 2] [--only-request ADDR] [--once]

Spec: docs/PROTOCOL.md sections 5, 6, 7 and 17. The verifier is the single trusted key of the prototype.
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..chain.client import ChainClient, ChainError, Request, Submission
from ..render.runner import find_blender, render_seg_only
from .checks import DEFAULT_THRESHOLDS, SeenImages, Thresholds, VerifyContext, run_checks
from .report import CHECKED, NOT_CHECKED, UNAVAILABLE, FrameResult, decide, load_report, write_report
from .reveal import DEFAULT_ATTEMPTS, DEFAULT_PAUSE, DEFAULT_TIMEOUT, Reveal, RevealUnavailable, discover_reveal_url, fetch_reveal
from .sampler import sample_indices

log = logging.getLogger("porf.verifier")

FetchFn = Callable[[str, str, int], Reveal]  # (reveal url, request address, frame index) -> Reveal, or raises RevealUnavailable
RerenderFn = Callable[[Request, list[int]], dict[int, np.ndarray]]  # (request, frame indices) -> the verifier's own id-pass images


class ReferenceRenderError(RuntimeError):
    """The seg re-render (check 7's reference) failed twice. The verifier then posts NO verdict: passing would hide fraud,
    failing would slash a node for the verifier's own fault."""


@dataclass
class VerifierConfig:
    keypair: str
    out_dir: Path = Path("out/verifier")
    poll_interval: float = 2.0
    only_request: str | None = None
    reveal_url: str | None = None          # default: PORF_REVEAL_URL or the node's announce file
    thresholds: Thresholds = DEFAULT_THRESHOLDS
    reveal_attempts: int = DEFAULT_ATTEMPTS
    reveal_timeout: float = DEFAULT_TIMEOUT
    reveal_pause: float = DEFAULT_PAUSE
    open_attempts: int = 20                # open_challenge retries on SlotHashNotNewer
    open_delay: float = 0.4
    payout_margin: float = 0.5             # seconds past challenge_window_end before trying the payout


class Verifier:
    def __init__(self, cfg: VerifierConfig, chain: ChainClient | None = None, fetch: FetchFn | None = None,
                 rerender: RerenderFn | None = None):
        if rerender is None and not find_blender():  # preflight: check 7 needs Blender, so refuse to start without it
            raise RuntimeError("Blender not found: the verifier re-renders the segmentation pass (check 7). Install it or set $BLENDER (docs/ENV.md)")
        self.cfg = cfg
        self.out_dir = Path(cfg.out_dir).resolve()
        self.chain = chain or ChainClient()
        self.seen = SeenImages()  # image hashes seen in this run, across requests
        self._fetch = fetch or self._default_fetch
        self._rerender = rerender or self._default_rerender
        self._timing: dict[str, dict[str, float]] = {}  # per request: stage timings carried from _advance into _judge

    # ------------------------------------------------------------ plumbing

    def _default_fetch(self, url: str, request: str, index: int) -> Reveal:
        c = self.cfg
        return fetch_reveal(url, request, index, c.reveal_attempts, c.reveal_timeout, c.reveal_pause)

    @staticmethod
    def _default_rerender(req: Request, indices: list[int]) -> dict[int, np.ndarray]:
        with tempfile.TemporaryDirectory(prefix="porf_rerender_") as tmp:   # a fresh Blender process every call
            return render_seg_only(req.seed, indices, tmp, req.occlusion_level, req.width, req.height)

    def _reference(self, req: Request, indices: list[int]) -> tuple[dict[int, np.ndarray], int, float]:
        """The verifier's own id pass for `indices`, in ONE Blender process; on failure one retry in a fresh process.
        Returns (images, attempts used, seconds)."""
        t0, last = time.time(), None
        for attempt in (1, 2):
            try:
                return self._rerender(req, indices), attempt, time.time() - t0
            except Exception as e:
                last = e
                log.warning("seg re-render attempt %d of 2 failed: %s", attempt, e)
        raise ReferenceRenderError(f"the seg re-render failed twice, each in a fresh Blender process: {last}") from last

    def report_dir(self, address: str) -> Path:
        return self.out_dir / address

    def _report(self, address: str, req: Request) -> dict[str, Any]:
        rep = load_report(self.report_dir(address)) or {"version": 1, "transactions": {}}
        rep.update(request=address, request_id=req.request_id, requester=req.requester, node=req.node, num_frames=req.num_frames)
        rep.setdefault("transactions", {})
        return rep

    def _save(self, address: str, report: dict[str, Any]) -> None:
        write_report(self.report_dir(address), report)

    # ------------------------------------------------------------ one poll

    def step(self) -> list[str]:
        """Advance every relevant request one stage. Returns short descriptions of what was done."""
        done: list[str] = []
        try:
            requests = sorted(self.chain.list_requests(), key=lambda kv: kv[1].created_at)
        except ChainError as e:  # e.g. the validator is down: wait and try again, do not crash
            log.warning("cannot list requests: %s", e)
            return [f"skipped: cannot list requests ({e.name})"]
        for address, req in requests:
            if self.cfg.only_request and address != self.cfg.only_request:
                continue
            if req.status not in ("Committed", "Challenged", "Verified"):
                continue
            try:
                done += self._advance(address, req)
            except ChainError as e:
                if e.kind == "rpc" or e.name == "Timeout":
                    log.warning("request %s: transient chain error, will retry: %s", address, e)
                    done.append(f"skipped {address}: transient {e.name}")
                else:
                    log.error("request %s: %s", address, e)
                    done.append(f"error {address}: {e.name}")
        return done

    def _advance(self, address: str, req: Request) -> list[str]:
        done: list[str] = []
        keypair = self.cfg.keypair
        started = time.time()
        if req.status == "Committed":
            out = self.chain.open_challenge(keypair, request=address, max_attempts=self.cfg.open_attempts, delay=self.cfg.open_delay)
            self._timing[address] = {"started": started, "open_challenge_s": time.time() - started}
            rep = self._report(address, req)
            rep["transactions"]["open_challenge"] = out["signature"]
            self._save(address, rep)
            done.append(f"opened challenge {address}")
            req = self.chain.get_request(request=address).request
        if req.status == "Challenged":
            sub = self.chain.get_submission(request=address)
            if sub.verdict == "Pending":
                if time.time() >= req.challenge_window_end:
                    log.warning("request %s: the challenge window closed before a verdict was posted; leaving it to the optimistic payout", address)
                    return done + [f"skipped {address}: window closed without a verdict"]
                try:
                    done += self._judge(address, req, sub)
                except ReferenceRenderError as e:
                    log.error("request %s: %s. NO verdict is posted; if this persists until the window closes the node is paid optimistically", address, e)
                    rep = self._report(address, req)
                    rep["verification_error"] = str(e)
                    self._save(address, rep)
                    return done + [f"error {address}: seg re-render failed, no verdict posted"]
                sub = self.chain.get_submission(request=address)
            if sub.verdict == "Fail":
                done += self._settle(address, req, "slash")
        elif req.status == "Verified":
            if time.time() < req.challenge_window_end + self.cfg.payout_margin:
                done.append(f"waiting {address}: challenge window open")
            else:
                done += self._settle(address, req, "pay")
        return done

    # ------------------------------------------------------------ verify and judge

    def _judge(self, address: str, req: Request, sub: Submission) -> list[str]:
        indices = sample_indices(bytes.fromhex(sub.sample_seed), sub.frame_count)
        if len(indices) != sub.sample_count:  # the sampler and the program disagree: a bug, not a verdict
            raise RuntimeError(f"sampler returned {len(indices)} indices but the program recorded sample_count {sub.sample_count}")
        url = self.cfg.reveal_url or discover_reveal_url()
        log.info("verifying %s: frames %s via %s", address, indices, url)
        timing = self._timing.pop(address, {"started": time.time()})
        t = time.time()
        fetched: list[tuple[int, Reveal]] = []
        tail: list[FrameResult] = []
        for position, index in enumerate(indices):
            try:
                fetched.append((index, self._fetch(url, address, index)))
            except RevealUnavailable as e:
                tail.append(FrameResult(index, UNAVAILABLE, reason=e.reason))
                # The verdict is Fail already, and waiting out three 10 s timeouts per remaining frame could outlast the window.
                tail += [FrameResult(i, NOT_CHECKED, reason="not fetched: an earlier sampled reveal was unavailable, so the verdict is already Fail")
                         for i in indices[position + 1:]]
                break
        timing["fetch_s"] = time.time() - t

        reference, attempts = None, 0
        if fetched:  # nothing to compare when no reveal arrived (an offline node is judged without a re-render)
            reference, attempts, timing["rerender_s"] = self._reference(req, [i for i, _ in fetched])
        ctx = VerifyContext(address, req, sub, self.cfg.thresholds, self.seen, reference_seg=reference)
        t = time.time()
        frames = [FrameResult(i, CHECKED, run_checks(rv, i, ctx)) for i, rv in fetched] + tail
        timing["checks_s"] = time.time() - t
        verdict, reasons = decide(frames)

        rep = self._report(address, req)
        rep.update(
            frame_count=sub.frame_count, merkle_root=sub.merkle_root, reveal_url=url,
            sample={"seed": sub.sample_seed, "slot": sub.sample_slot, "count": sub.sample_count, "indices": indices},
            frames=[f.to_dict() for f in frames], verdict=verdict, verdict_reasons=reasons,
            reference_render={"attempts": attempts, "frames": [i for i, _ in fetched], "seconds": round(timing.get("rerender_s", 0.0), 3)},
            thresholds={"calibrated": self.cfg.thresholds.calibrated, "visibility_tolerance": self.cfg.thresholds.visibility_tolerance,
                        "per_class": {k: vars(v) for k, v in self.cfg.thresholds.per_class.items()},
                        "seg_rerender": vars(self.cfg.thresholds.seg_rerender)},
        )
        self._save(address, rep)  # on disk before the transaction, so a failed transaction still leaves the findings
        t = time.time()
        out = self.chain.submit_verdict(self.cfg.keypair, verdict, request=address)
        timing["verdict_s"] = time.time() - t
        timing["total_s"] = time.time() - timing["started"]
        rep["transactions"]["submit_verdict"] = out["signature"]
        rep["timing"] = {k: round(v, 3) for k, v in timing.items() if k != "started"}   # seconds, per stage
        self._save(address, rep)
        log.info("verdict for %s: %s%s", address, verdict, "".join(f"\n  - {r}" for r in reasons))
        return [f"verdict {verdict} {address}"]

    # ------------------------------------------------------------ settle

    def _settle(self, address: str, req: Request, how: str) -> list[str]:
        """Slash (verdict Fail) or pay out (verdict Pass, window over). Records what actually moved."""
        slash = how == "slash"
        owner = req.requester if slash else req.node
        before_view = self.chain.get_request(request=address)
        before = self.chain.token_balance(owner=owner, mint=req.mint)
        try:
            out = (self.chain.slash_and_refund if slash else self.chain.finalize_payout)(self.cfg.keypair, request=address)
        except ChainError as e:
            if not slash and e.name == "ChallengeWindowOpen":  # the chain's clock trails ours a little; try again next poll
                return [f"waiting {address}: chain says the window is still open"]
            raise
        after_view = self.chain.get_request(request=address)
        after = self.chain.token_balance(owner=owner, mint=req.mint)
        rep = self._report(address, req)
        rep["transactions"]["slash_and_refund" if slash else "finalize_payout"] = out["signature"]
        rep["outcome"] = {
            "action": "slashed" if slash else "paid", "to": "requester" if slash else "node", "destination": owner,
            "escrow_moved": (before_view.escrow_balance or 0) - (after_view.escrow_balance or 0),
            "stake_moved": (before_view.stake_vault_balance or 0) - (after_view.stake_vault_balance or 0),
            "destination_delta": after - before, "final_status": after_view.request.status,
        }
        self._save(address, rep)
        log.info("%s %s: %s", "slashed" if slash else "paid", address, rep["outcome"])
        return [f"{'slashed' if slash else 'paid'} {address}"]

    # ------------------------------------------------------------ drivers

    def run_until_settled(self, timeout: float = 600.0) -> list[str]:
        """Poll until every targeted request is Paid or Slashed (or timeout). For scripts and tests."""
        deadline, log_lines = time.time() + timeout, []
        while time.time() < deadline:
            log_lines += self.step()
            try:
                targets = [(a, r) for a, r in self.chain.list_requests() if not self.cfg.only_request or a == self.cfg.only_request]
            except ChainError:
                targets = []
            if targets and all(r.status in ("Paid", "Slashed", "Expired") for _, r in targets):
                return log_lines
            time.sleep(self.cfg.poll_interval)
        raise TimeoutError(f"requests not settled within {timeout}s")

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("verifier polling every %.1fs, reports in %s", self.cfg.poll_interval, self.out_dir)
        while not stop.is_set():
            try:
                for line in self.step():
                    log.info("step: %s", line)
            except Exception:
                log.exception("unexpected error in poll loop")
            stop.wait(self.cfg.poll_interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Proof-of-Render verifier")
    ap.add_argument("--keypair", required=True, help="verifier keypair file (must be the configured verifier)")
    ap.add_argument("--out", default="out/verifier")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--only-request", help="verify only this request address")
    ap.add_argument("--reveal-url", help="the node's reveal server (default: PORF_REVEAL_URL or .localnet/reveal.json)")
    ap.add_argument("--once", action="store_true", help="work until the targeted requests are settled, then exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = VerifierConfig(keypair=args.keypair, out_dir=Path(args.out), poll_interval=args.poll, only_request=args.only_request, reveal_url=args.reveal_url)
    verifier = Verifier(cfg)
    try:
        if args.once:
            verifier.run_until_settled()
        else:
            verifier.run()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
