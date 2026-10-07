"""The Day 4 gate, end to end: a real localnet, real Blender renders, a real node with its reveal server, the real verifier,
real token balances. Honest node -> paid. Tampered nodes -> slashed. Skips cleanly without a validator or Blender."""
import hashlib
import json
import time

import pytest

import livenet
from porf.chain.client import CreateRequestParams
from porf.merkle import merkle_root
from porf.node.client import Node, NodeConfig
from porf.node.job import Job, build_leaf_cache, write_leaf_cache
from porf.render.runner import find_blender
from porf.verifier.checks import CHECK_NAMES
from porf.verifier.client import Verifier, VerifierConfig

USDC = 1_000_000
FRAMES = 7                      # N = 7: every frame is sampled (frames 0 and 6 plus all five in between)
ESCROW, STAKE = FRAMES * USDC, FRAMES * USDC * 2000 // 10_000
WINDOW = 14                     # seconds: room for open + 7 fetches/checks + verdict even on a busy machine
pytestmark = [pytest.mark.slow, pytest.mark.needs_validator, pytest.mark.skipif(find_blender() is None, reason="Blender not installed")]


@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


class Scenario:
    """One request on the live chain plus a node and a verifier wired to it."""

    def __init__(self, net, tmp_path, name):
        self.net, self.c, self.e = net, net.client, net.env
        self.tmp = tmp_path / name
        self.req_tok, self.node_tok = self.e["token_accounts"]["requester"], self.e["token_accounts"]["node"]
        self.before = self.balances()
        rid = next(net.ids)
        params = CreateRequestParams(request_id=rid, seed=42, num_frames=FRAMES, price_per_frame=USDC, claim_duration=600, commit_duration=900,
                                     challenge_window_secs=WINDOW, occlusion_level=3)
        self.address = self.c.create_request(self.e["keys"]["requester"], params)["request"]
        self.node = Node(NodeConfig(keypair=self.e["keys"]["node"], out_dir=self.tmp / "node", samples=4, only_request=self.address, port=0,
                                    announce_file=None), self.c)
        self.server = None

    def balances(self):
        return {"requester": self.c.token_balance(token_account=self.req_tok), "node": self.c.token_balance(token_account=self.node_tok)}

    def honest_commit(self):
        assert self.node.step()[-1] == f"committed {self.address}"
        self.server = self.node.start_server()
        return self

    def verifier(self, url=None, **over):
        cfg = VerifierConfig(keypair=self.e["keys"]["verifier"], out_dir=self.tmp / "verifier", only_request=self.address,
                             reveal_url=url or self.server.url, poll_interval=0.5, reveal_attempts=3, reveal_timeout=5.0, reveal_pause=0.1, **over)
        return Verifier(cfg, chain=self.c)

    def report(self):
        return json.loads((self.tmp / "verifier" / self.address / "report.json").read_text())

    def close(self):
        if self.server:
            self.server.stop()


@pytest.fixture
def scenario(net, tmp_path, request):
    made = []

    def make(name):
        s = Scenario(net, tmp_path, name)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_honest_node_is_verified_and_paid(scenario, net):
    s = scenario("honest").honest_commit()
    s.verifier().run_until_settled(timeout=120)

    rep = s.report()
    assert rep["verdict"] == "Pass" and rep["verdict_reasons"] == []
    assert rep["sample"]["indices"] == list(range(FRAMES)) and rep["sample"]["count"] == FRAMES
    assert len(rep["frames"]) == FRAMES
    for f in rep["frames"]:
        assert f["status"] == "checked" and f["passed"], f
        assert [c["name"] for c in f["checks"]] == list(CHECK_NAMES) and all(c["passed"] for c in f["checks"]), f
    assert set(rep["transactions"]) == {"open_challenge", "submit_verdict", "finalize_payout"}
    assert rep["outcome"] == {"action": "paid", "to": "node", "destination": net.env["pubkeys"]["node"], "escrow_moved": ESCROW,
                              "stake_moved": STAKE, "destination_delta": ESCROW + STAKE, "final_status": "Paid"}
    assert s.balances() == {"requester": s.before["requester"] - ESCROW, "node": s.before["node"] + ESCROW}   # node nets +escrow
    assert (s.tmp / "verifier" / s.address / "report.txt").read_text().count("[ok]") == FRAMES * len(CHECK_NAMES)

    # measurements for the threshold calibration (PROTOCOL.md section 17.5): real renders against the placeholder thresholds
    metrics = [next(c for c in f["checks"] if c["name"] == "annotation_plausibility")["metrics"] for f in rep["frames"]]
    assert sum(m["boxes_checked"] for m in metrics) > 20
    assert max(m["max_overshoot_px"] for m in metrics if m["max_overshoot_px"] is not None) < 2.0
    print("\nreal renders: boxes", sum(m["boxes_checked"] for m in metrics), "| worst overshoot px",
          max(m["max_overshoot_px"] for m in metrics if m["max_overshoot_px"] is not None), "| smallest IoU margin",
          min(m["min_iou_margin"] for m in metrics if m["min_iou_margin"] is not None))


def test_a_node_that_committed_fabricated_boxes_is_slashed(scenario, net):
    """Hash-consistent fraud: the node edits annotations.json BEFORE committing, so every hash and proof verifies."""
    s = scenario("fake_boxes")
    req = s.c.get_request(request=s.address).request
    job = s.node._claim(s.address, req)
    s.node._render(job)
    for i in range(FRAMES):
        path = job.frame_dir(i) / "annotations.json"
        ann = json.loads(path.read_text())
        visible = [o for o in ann["objects"] if o["bbox"]]
        if visible:
            big = max(visible, key=lambda o: (o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]))
            big["bbox"] = [big["bbox"][0] + 15, big["bbox"][1], big["bbox"][2] + 15, big["bbox"][3]]
            from porf.canon import canonical_bytes
            path.write_bytes(canonical_bytes(ann))
    write_leaf_cache(job, build_leaf_cache(job))                 # the fraud is committed, not caught by hashing
    assert s.node.step() == [f"committed {s.address}"]
    s.server = s.node.start_server()

    s.verifier().run_until_settled(timeout=120)
    rep = s.report()
    assert rep["verdict"] == "Fail"
    failing = {c["name"] for f in rep["frames"] for c in f["checks"] if not c["passed"]}
    assert "annotation_plausibility" in failing and "segmentation_consistency" in failing
    assert not ({"merkle_proof", "scene_match", "hash_consistency"} & failing)        # it committed its lie consistently
    assert rep["outcome"]["action"] == "slashed" and rep["outcome"]["destination_delta"] == ESCROW + STAKE
    assert s.balances() == {"requester": s.before["requester"] + STAKE, "node": s.before["node"] - STAKE}
    assert s.c.get_request(request=s.address).request.status == "Slashed"


def test_a_node_that_changes_a_file_after_committing_is_slashed(scenario, net):
    s = scenario("swapped_after_commit").honest_commit()
    job = Job.load(s.node.out_dir / str(s.c.get_request(request=s.address).request.request_id))
    (job.frame_dir(0) / "rgb.png").write_bytes((job.frame_dir(1) / "rgb.png").read_bytes())   # serve frame 1's picture as frame 0

    s.verifier().run_until_settled(timeout=120)
    rep = s.report()
    assert rep["verdict"] == "Fail"
    assert any("frame 0: hash_consistency failed" in r and "rgb.png" in r for r in rep["verdict_reasons"])
    assert any("image_sanity" in r and "duplicated image" in r for r in rep["verdict_reasons"])   # the copy is also a duplicate
    assert s.balances() == {"requester": s.before["requester"] + STAKE, "node": s.before["node"] - STAKE}


def test_a_node_that_cannot_be_reached_is_slashed_as_reveal_unavailable(scenario, net):
    s = scenario("offline")
    s.c.stake_and_claim(s.e["keys"]["node"], request=s.address)
    s.c.submit_commitment(s.e["keys"]["node"], hashlib.sha256(b"a root for frames that were never rendered").digest(), FRAMES, request=s.address)

    s.verifier(url="http://127.0.0.1:1").run_until_settled(timeout=120)
    rep = s.report()
    assert rep["verdict"] == "Fail"
    assert [f["status"] for f in rep["frames"]] == ["reveal_unavailable"] + ["not_checked"] * (FRAMES - 1)
    assert len(rep["verdict_reasons"]) == 1 and "reveal unavailable" in rep["verdict_reasons"][0] and "failed:" not in rep["verdict_reasons"][0]
    assert "after 3 attempts" in rep["frames"][0]["reason"]
    assert s.balances() == {"requester": s.before["requester"] + STAKE, "node": s.before["node"] - STAKE}
    assert "UNAVAILABLE" in (s.tmp / "verifier" / s.address / "report.txt").read_text()
