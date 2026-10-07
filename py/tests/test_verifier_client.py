"""Reveal fetching, verdict aggregation, reports and the verifier's state machine, against a local HTTP server and a fake
chain. (The real chain and real renders are exercised in test_verifier_flow.py.)"""
import dataclasses
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from porf.chain.client import ChainError, RequestView
from porf.verifier import report as R
from porf.verifier.checks import CheckResult
from porf.verifier.client import Verifier, VerifierConfig
from porf.verifier.report import CHECKED, NOT_CHECKED, UNAVAILABLE, FrameResult, decide, summarize
from porf.verifier.reveal import Reveal, RevealUnavailable, discover_reveal_url, fetch_reveal
from porf.verifier.sampler import sample_indices
from verifier_fixtures import ADDRESS, HonestNode

# ---------------------------------------------------------------- reveal fetching against a real HTTP server


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        s = self.server
        s.hits.append(self.path)
        mode = s.mode(len(s.hits)) if callable(s.mode) else s.mode
        if mode == "hang":
            time.sleep(2)
            return
        status, body = {"ok": (200, json.dumps({"frame_index": 1})), "404": (404, "{}"), "500": (500, "{}"), "badjson": (200, "<html>nope")}[mode]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.hits, srv.mode = [], "ok"
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def test_fetch_reveal_asks_for_the_frame_and_the_request(server):
    rv = fetch_reveal(server.url, "Req/Addr", 7, attempts=3, timeout=2, pause=0)
    assert isinstance(rv, Reveal) and rv.frame_index == 1
    assert server.hits == ["/frame/7?request=Req/Addr"] or server.hits == ["/frame/7?request=Req%2FAddr"]


@pytest.mark.parametrize("mode", ["404", "500", "badjson", "hang"])
def test_an_unservable_reveal_is_unavailable_after_exactly_three_attempts(server, mode):
    server.mode = mode
    with pytest.raises(RevealUnavailable) as e:
        fetch_reveal(server.url, "R", 3, attempts=3, timeout=0.3, pause=0)
    assert e.value.attempts == 3 and len(server.hits) == 3
    assert "frame 3 not served after 3 attempts" in e.value.reason and "0.3s each" in e.value.reason


def test_a_refused_connection_is_unavailable_too():
    with pytest.raises(RevealUnavailable) as e:
        fetch_reveal("http://127.0.0.1:1", "R", 0, attempts=3, timeout=0.3, pause=0)
    assert "last error" in e.value.reason


def test_a_transient_failure_is_retried(server):
    server.mode = lambda n: "500" if n < 3 else "ok"
    assert fetch_reveal(server.url, "R", 1, attempts=3, timeout=2, pause=0).frame_index == 1
    assert len(server.hits) == 3
    server.hits.clear()
    server.mode = lambda n: "500" if n < 4 else "ok"      # succeeds only on the 4th try: one too many
    with pytest.raises(RevealUnavailable):
        fetch_reveal(server.url, "R", 1, attempts=3, timeout=2, pause=0)


def test_defaults_are_three_attempts_of_ten_seconds():
    import inspect
    sig = inspect.signature(fetch_reveal)
    assert (sig.parameters["attempts"].default, sig.parameters["timeout"].default) == (3, 10.0)


def test_reveal_parsing_never_raises():
    for junk in (None, 5, [], "x", {"proof": 1}, {"rgb_png": "!!!notbase64", "seg_png": 3}):
        rv = Reveal.parse(junk)
        assert rv.errors and rv.rgb_png is None


def test_reveal_url_discovery_prefers_the_environment_then_the_announce_file(tmp_path):
    f = tmp_path / "reveal.json"
    f.write_text(json.dumps({"url": "http://file:1"}))
    assert discover_reveal_url({"PORF_REVEAL_URL": "http://env:2"}, f) == "http://env:2"
    assert discover_reveal_url({}, f) == "http://file:1"
    with pytest.raises(RuntimeError, match="PORF_REVEAL_URL"):
        discover_reveal_url({}, tmp_path / "missing.json")
    f.write_text("{not json")
    with pytest.raises(RuntimeError, match="cannot read"):
        discover_reveal_url({}, f)


# ---------------------------------------------------------------- verdict aggregation and reports

def ok(name="x"):
    return CheckResult(name, True, "fine")


def test_decide_passes_only_when_every_sampled_frame_passed_every_check():
    assert decide([FrameResult(0, CHECKED, [ok(), ok()]), FrameResult(1, CHECKED, [ok()])]) == ("Pass", [])
    verdict, reasons = decide([FrameResult(0, CHECKED, [ok()]), FrameResult(1, CHECKED, [ok("a"), CheckResult("merkle_proof", False, "bad proof")])])
    assert verdict == "Fail" and reasons == ["frame 1: merkle_proof failed: bad proof"]


def test_an_unavailable_reveal_is_reported_as_such_not_as_a_check_failure():
    frames = [FrameResult(0, CHECKED, [ok()]), FrameResult(4, UNAVAILABLE, reason="HTTP 404"), FrameResult(9, NOT_CHECKED, reason="skipped")]
    verdict, reasons = decide(frames)
    assert verdict == "Fail" and reasons == ["frame 4: reveal unavailable (HTTP 404)"]
    assert "failed:" not in reasons[0]


def test_an_empty_sample_never_passes():
    assert decide([])[0] == "Fail"
    assert not FrameResult(0, CHECKED, []).passed      # a frame on which no check ran has not passed


# ---------------------------------------------------------------- the verifier against a fake chain

class FakeChain:
    """Just enough chain to drive Verifier: one request whose state changes as the verifier acts."""

    def __init__(self, node: HonestNode, status="Committed", verdict="Pending", window_end=None):
        self.node = node
        self.req = dataclasses.replace(node.request, status=status, challenge_window_end=window_end if window_end is not None else 0)
        self.sub = dataclasses.replace(node.submission, verdict=verdict)
        self.calls: list[tuple] = []
        self.balances = {"Requester111": 1000, "Node111": 2000}
        self.escrow, self.stake = node.request.escrow_amount, node.request.stake_amount
        self.fail_with: dict[str, ChainError] = {}
        self.window_secs = 600

    def _maybe_fail(self, name):
        if name in self.fail_with:
            raise self.fail_with[name]

    def list_requests(self, status=None, requester=None):
        self._maybe_fail("list_requests")
        return [(ADDRESS, self.req)]

    def get_request(self, request=None, **kw):
        return RequestView(ADDRESS, "esc", "stk", "sub", self.escrow, self.stake, self.req)

    def get_submission(self, request=None, **kw):
        return self.sub

    def token_balance(self, token_account=None, owner=None, mint=None):
        return self.balances[owner]

    def open_challenge(self, signer, request=None, max_attempts=1, delay=0.4, **kw):
        self.calls.append(("open_challenge", max_attempts, delay))
        self._maybe_fail("open_challenge")
        self.req = dataclasses.replace(self.req, status="Challenged", challenge_window_end=int(time.time()) + self.window_secs)
        return {"signature": "sigOpen"}

    def submit_verdict(self, signer, verdict, request=None, **kw):
        self.calls.append(("submit_verdict", verdict))
        self._maybe_fail("submit_verdict")
        self.sub = dataclasses.replace(self.sub, verdict=verdict)
        if verdict == "Pass":
            self.req = dataclasses.replace(self.req, status="Verified")
        return {"signature": "sigVerdict"}

    def _move(self, to, status):
        self.balances[to] += self.escrow + self.stake
        self.escrow = self.stake = 0
        self.req = dataclasses.replace(self.req, status=status)

    def finalize_payout(self, signer, request=None, **kw):
        self.calls.append(("finalize_payout",))
        self._maybe_fail("finalize_payout")
        self._move("Node111", "Paid")
        return {"signature": "sigPay"}

    def slash_and_refund(self, signer, request=None, **kw):
        self.calls.append(("slash_and_refund",))
        self._maybe_fail("slash_and_refund")
        self._move("Requester111", "Slashed")
        return {"signature": "sigSlash"}


def make_verifier(tmp_path, chain, node, fetch=None, **cfg):
    fetch = fetch or (lambda url, request, index: node.reveal(index))
    config = VerifierConfig(keypair="keys/verifier.json", out_dir=tmp_path / "reports", reveal_url="http://node.invalid", **cfg)
    return Verifier(config, chain=chain, fetch=fetch)


@pytest.fixture(scope="module")
def honest(tmp_path_factory):
    return HonestNode(tmp_path_factory.mktemp("honest"), n=8)


def read_report(tmp_path):
    return json.loads((tmp_path / "reports" / ADDRESS / "report.json").read_text())


def test_honest_node_is_judged_pass_and_the_report_says_why(tmp_path, honest):
    chain = FakeChain(honest, status="Committed")
    v = make_verifier(tmp_path, chain, honest)
    actions = v.step()
    assert actions == [f"opened challenge {ADDRESS}", f"verdict Pass {ADDRESS}"]
    assert chain.calls[0] == ("open_challenge", 20, 0.4)          # the bounded retry budget
    assert chain.calls[1] == ("submit_verdict", "Pass") and chain.req.status == "Verified"

    rep = read_report(tmp_path)
    indices = sample_indices(bytes.fromhex(honest.submission.sample_seed), 8)
    assert rep["sample"] == {"seed": honest.submission.sample_seed, "slot": 149, "count": 7, "indices": indices}
    assert [f["index"] for f in rep["frames"]] == indices and all(f["passed"] for f in rep["frames"])
    assert all([c["name"] for c in f["checks"]] == list(__import__("porf.verifier.checks", fromlist=["CHECK_NAMES"]).CHECK_NAMES) for f in rep["frames"])
    assert rep["verdict"] == "Pass" and rep["verdict_reasons"] == []
    assert rep["transactions"] == {"open_challenge": "sigOpen", "submit_verdict": "sigVerdict"}
    assert rep["thresholds"]["calibrated"] is False and rep["reveal_url"] == "http://node.invalid"
    txt = (tmp_path / "reports" / ADDRESS / "report.txt").read_text()
    assert "VERDICT: Pass" in txt and "PLACEHOLDERS" in txt and "frame    0: PASS" in txt


def test_a_pass_is_paid_only_after_the_window_with_the_amounts_recorded(tmp_path, honest):
    chain = FakeChain(honest, status="Committed")
    v = make_verifier(tmp_path, chain, honest, payout_margin=0.0)
    v.step()                                                       # open + Pass
    assert v.step() == [f"waiting {ADDRESS}: challenge window open"] and ("finalize_payout",) not in chain.calls
    chain.req = dataclasses.replace(chain.req, challenge_window_end=int(time.time()) - 1)     # the window is over
    assert v.step() == [f"paid {ADDRESS}"]
    rep = read_report(tmp_path)
    assert rep["transactions"]["finalize_payout"] == "sigPay"
    assert rep["outcome"] == {"action": "paid", "to": "node", "destination": "Node111", "escrow_moved": honest.request.escrow_amount,
                              "stake_moved": honest.request.stake_amount, "destination_delta": honest.request.escrow_amount + honest.request.stake_amount,
                              "final_status": "Paid"}
    assert "Outcome: paid -> node" in (tmp_path / "reports" / ADDRESS / "report.txt").read_text()


def shifted(i, files):
    from verifier_fixtures import W
    a = max((o for o in files.annotations["objects"] if o["bbox"]), key=lambda o: (o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]))
    x0, y0, x1, y1 = a["bbox"]
    d = 12 if x1 + 12 <= W else -12
    a["bbox"] = [x0 + d, y0, x1 + d, y1]


def test_a_tampered_node_is_judged_fail_and_slashed_immediately(tmp_path):
    node = HonestNode(tmp_path / "node", n=8, mutate=shifted)       # the node COMMITTED shifted boxes in every frame
    chain = FakeChain(node, status="Committed")
    v = make_verifier(tmp_path, chain, node)
    assert v.step() == [f"opened challenge {ADDRESS}", f"verdict Fail {ADDRESS}", f"slashed {ADDRESS}"]
    assert chain.req.status == "Slashed"
    rep = read_report(tmp_path)
    assert rep["verdict"] == "Fail" and any("annotation_plausibility" in r for r in rep["verdict_reasons"])
    assert rep["outcome"]["action"] == "slashed" and rep["outcome"]["to"] == "requester"
    assert rep["outcome"]["destination_delta"] == node.request.escrow_amount + node.request.stake_amount
    assert rep["transactions"]["slash_and_refund"] == "sigSlash"
    assert "VERDICT: Fail" in (tmp_path / "reports" / ADDRESS / "report.txt").read_text()


def test_every_sampled_frame_is_checked_and_reported_not_just_the_first_failure(tmp_path):
    node = HonestNode(tmp_path / "node", n=8, mutate=shifted)
    chain = FakeChain(node)
    make_verifier(tmp_path, chain, node).step()
    frames = read_report(tmp_path)["frames"]
    assert len(frames) == 7 and all(f["status"] == CHECKED for f in frames)
    assert sum(1 for f in frames if not f["passed"]) >= 2


def test_an_unavailable_reveal_fails_the_request_without_blaming_a_check(tmp_path, honest):
    calls = []

    def flaky(url, request, index):
        calls.append(index)
        if len(calls) == 3:
            raise RevealUnavailable("frame not served after 3 attempts (10s each); last error: HTTP 404", 3)
        return honest.reveal(index)

    chain = FakeChain(honest)
    v = make_verifier(tmp_path, chain, honest, fetch=flaky)
    v.step()
    rep = read_report(tmp_path)
    statuses = [f["status"] for f in rep["frames"]]
    assert statuses == [CHECKED, CHECKED, UNAVAILABLE] + [NOT_CHECKED] * 4
    assert len(calls) == 3                                            # it stopped fetching: the verdict was already Fail
    assert rep["verdict"] == "Fail" and len(rep["verdict_reasons"]) == 1
    assert "reveal unavailable" in rep["verdict_reasons"][0] and "failed:" not in rep["verdict_reasons"][0]
    assert chain.req.status == "Slashed"
    assert "UNAVAILABLE" in (tmp_path / "reports" / ADDRESS / "report.txt").read_text()


def test_a_node_that_is_entirely_down_is_failed_after_one_frame_of_timeouts(tmp_path, honest):
    def down(url, request, index):
        raise RevealUnavailable("connection refused", 3)

    chain = FakeChain(honest)
    make_verifier(tmp_path, chain, honest, fetch=down).step()
    rep = read_report(tmp_path)
    assert [f["status"] for f in rep["frames"]] == [UNAVAILABLE] + [NOT_CHECKED] * 6 and rep["verdict"] == "Fail"


def test_a_copy_of_another_requests_images_fails_image_sanity(tmp_path, honest):
    """Two requests whose nodes served the same images: the second is flagged by the run's seen set."""
    chain = FakeChain(honest)
    v = make_verifier(tmp_path, chain, honest)
    v.step()                                                          # request A: all fine, images now "seen"
    assert read_report(tmp_path)["verdict"] == "Pass"
    # judge the SAME images again under a different request address, with the same Verifier (same seen set)
    chain.req = dataclasses.replace(chain.req, status="Challenged")
    chain.sub = dataclasses.replace(chain.sub, verdict="Pending", request="OtherRequestAddress")
    v._judge("OtherRequestAddress", chain.req, chain.sub)
    rep = json.loads((tmp_path / "reports" / "OtherRequestAddress" / "report.json").read_text())
    assert rep["verdict"] == "Fail" and any("image_sanity" in r and "duplicated image" in r for r in rep["verdict_reasons"])
    # ... while re-judging request A itself is not a duplicate of itself
    v._judge(ADDRESS, chain.req, chain.sub)
    assert read_report(tmp_path)["verdict"] == "Pass"


def test_the_sampler_and_the_program_disagreeing_is_an_error_not_a_verdict(tmp_path, honest):
    chain = FakeChain(honest, status="Challenged")
    chain.sub = dataclasses.replace(chain.sub, sample_count=5, verdict="Pending")
    chain.req = dataclasses.replace(chain.req, challenge_window_end=int(time.time()) + 600)
    with pytest.raises(RuntimeError, match="sample_count"):
        make_verifier(tmp_path, chain, honest).step()
    assert not any(c[0] == "submit_verdict" for c in chain.calls)


def test_a_window_that_closed_before_the_verdict_is_left_alone(tmp_path, honest):
    chain = FakeChain(honest, status="Challenged", window_end=int(time.time()) - 5)
    assert make_verifier(tmp_path, chain, honest).step() == [f"skipped {ADDRESS}: window closed without a verdict"]
    assert chain.calls == []


def test_resuming_after_a_fail_verdict_slashes(tmp_path, honest):
    chain = FakeChain(honest, status="Challenged", verdict="Fail", window_end=int(time.time()) + 600)
    assert make_verifier(tmp_path, chain, honest).step() == [f"slashed {ADDRESS}"]
    assert [c[0] for c in chain.calls] == ["slash_and_refund"]


def test_the_chains_clock_trailing_ours_is_not_an_error(tmp_path, honest):
    chain = FakeChain(honest, status="Verified", verdict="Pass", window_end=int(time.time()) - 1)
    chain.fail_with["finalize_payout"] = ChainError("ChallengeWindowOpen", "too early", kind="program")
    assert make_verifier(tmp_path, chain, honest, payout_margin=0.0).step() == [f"waiting {ADDRESS}: chain says the window is still open"]


def test_irrelevant_requests_are_ignored_and_errors_do_not_kill_the_poll(tmp_path, honest):
    for status in ("Open", "Claimed", "Paid", "Slashed", "Expired"):
        chain = FakeChain(honest, status=status)
        assert make_verifier(tmp_path, chain, honest).step() == [] and chain.calls == []
    chain = FakeChain(honest, status="Committed")
    assert make_verifier(tmp_path, chain, honest, only_request="SomeOtherRequest").step() == []
    chain.fail_with["open_challenge"] = ChainError("Timeout", "slow", kind="other")
    assert "transient" in make_verifier(tmp_path, chain, honest).step()[0]
    chain.fail_with["open_challenge"] = ChainError("UnauthorizedVerifier", "not me", kind="program")
    assert make_verifier(tmp_path, chain, honest).step() == [f"error {ADDRESS}: UnauthorizedVerifier"]


def test_a_failed_verdict_transaction_still_leaves_the_findings_on_disk(tmp_path, honest):
    chain = FakeChain(honest, status="Challenged", window_end=int(time.time()) + 600)
    chain.fail_with["submit_verdict"] = ChainError("ChallengeWindowClosed", "too late", kind="program")
    v = make_verifier(tmp_path, chain, honest)
    assert v.step() == [f"error {ADDRESS}: ChallengeWindowClosed"]
    rep = read_report(tmp_path)
    assert rep["verdict"] == "Pass" and "submit_verdict" not in rep["transactions"]


def test_summary_text_lists_every_check_and_reason():
    rep = {"request": "R", "request_id": 1, "node": "N", "frame_count": 8, "reveal_url": "http://x", "verdict": "Fail",
           "sample": {"count": 2, "indices": [0, 7], "seed": "ab" * 32, "slot": 5},
           "frames": [FrameResult(0, CHECKED, [ok("merkle_proof")]).to_dict(), FrameResult(7, UNAVAILABLE, reason="HTTP 500").to_dict()],
           "verdict_reasons": ["frame 7: reveal unavailable (HTTP 500)"], "transactions": {"submit_verdict": "s" * 20}, "thresholds": {"calibrated": False}}
    text = summarize(rep)
    assert "[ok] merkle_proof" in text and "UNAVAILABLE" in text and "VERDICT: Fail" in text and "reveal unavailable (HTTP 500)" in text


def test_an_unreachable_chain_is_waited_out_not_fatal(tmp_path, honest):
    chain = FakeChain(honest, status="Committed")
    chain.fail_with["list_requests"] = ChainError("RpcError", "fetch failed", kind="rpc")
    v = make_verifier(tmp_path, chain, honest)
    assert v.step() == ["skipped: cannot list requests (RpcError)"] and chain.calls == []


def test_run_until_settled_returns_when_settled_and_times_out_otherwise(tmp_path, honest):
    done = FakeChain(honest, status="Paid")
    assert make_verifier(tmp_path, done, honest, poll_interval=0.01).run_until_settled(timeout=5) == []
    stuck = FakeChain(honest, status="Open")           # never becomes terminal
    with pytest.raises(TimeoutError):
        make_verifier(tmp_path, stuck, honest, poll_interval=0.01).run_until_settled(timeout=0.2)
    down = FakeChain(honest, status="Paid")
    down.fail_with["list_requests"] = ChainError("RpcError", "fetch failed", kind="rpc")
    with pytest.raises(TimeoutError):                    # keeps retrying through the outage instead of crashing
        make_verifier(tmp_path, down, honest, poll_interval=0.01).run_until_settled(timeout=0.2)
