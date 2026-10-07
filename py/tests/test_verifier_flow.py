"""The Day 4 gate, end to end, with the calibrated thresholds: a real localnet, real Blender renders, a real node with its reveal
server, the real verifier including its seg re-render, real token balances, and the demo-default 60 s challenge window.

    honest node, 20 frames at occlusion 3, production render settings  -> verified and paid
    tampered boxes (committed consistently)                             -> slashed (checks 4 and 6)
    each of the five --cheat modes (the node's real code path), 20 frames -> slashed
    a file changed after the commit / a node that is offline            -> slashed
Skips cleanly without a validator or Blender."""
import hashlib
import json
import time

import pytest

import livenet
from porf.canon import canonical_bytes
from porf.chain.client import DEFAULT_CHALLENGE_WINDOW_SECS, CreateRequestParams
from porf.node import cheats
from porf.node.client import Node, NodeConfig
from porf.node.job import Job, build_leaf_cache, write_leaf_cache
from porf.render.runner import find_blender
from porf.verifier.checks import CHECK_NAMES
from porf.verifier.client import Verifier, VerifierConfig
from porf.verifier.sampler import sample_indices

USDC = 1_000_000
pytestmark = [pytest.mark.slow, pytest.mark.needs_validator, pytest.mark.skipif(find_blender() is None, reason="Blender not installed")]


@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


class Scenario:
    """One request on the live chain plus a node and a verifier wired to it."""

    def __init__(self, net, tmp_path, name, frames, samples=None, cheat=None):
        self.net, self.c, self.e = net, net.client, net.env
        self.frames = frames
        self.escrow = frames * USDC
        self.stake = self.escrow * 2000 // 10_000
        self.tmp = tmp_path / name
        self.req_tok, self.node_tok = self.e["token_accounts"]["requester"], self.e["token_accounts"]["node"]
        self.before = self.balances()
        rid = next(net.ids)
        params = CreateRequestParams(request_id=rid, seed=42, num_frames=frames, price_per_frame=USDC, claim_duration=900, commit_duration=900,
                                     occlusion_level=3)       # challenge_window_secs: the demo default
        assert params.challenge_window_secs == DEFAULT_CHALLENGE_WINDOW_SECS == 60
        self.address = self.c.create_request(self.e["keys"]["requester"], params)["request"]
        cfg = dict(keypair=self.e["keys"]["node"], out_dir=self.tmp / "node", only_request=self.address, port=0, announce_file=None, cheat=cheat)
        if samples is not None:
            cfg["samples"] = samples                          # otherwise the production default (64)
        self.node = Node(NodeConfig(**cfg), self.c)
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
        return Verifier(cfg, chain=self.c)   # no injected re-render: the real Blender seg-only pass

    def report(self):
        return json.loads((self.tmp / "verifier" / self.address / "report.json").read_text())

    def failing_checks(self, rep):
        return {c["name"] for f in rep["frames"] for c in f["checks"] if not c["passed"]}

    def close(self):
        if self.server:
            self.server.stop()


@pytest.fixture
def scenario(net, tmp_path):
    made = []

    def make(name, frames, **kw):
        s = Scenario(net, tmp_path, name, frames, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_honest_20_frames_at_occlusion_3_is_verified_and_paid(scenario, net):
    s = scenario("honest", 20).honest_commit()                     # production render settings (64 samples)
    s.verifier().run_until_settled(timeout=400)

    rep = s.report()
    assert rep["verdict"] == "Pass" and rep["verdict_reasons"] == []
    indices = sample_indices(bytes.fromhex(rep["sample"]["seed"]), 20)
    assert rep["sample"]["indices"] == indices and rep["sample"]["count"] == 7 and indices[0] == 0 and indices[-1] == 19
    assert len(rep["frames"]) == 7
    for f in rep["frames"]:
        assert f["status"] == "checked" and f["passed"], f
        assert [c["name"] for c in f["checks"]] == list(CHECK_NAMES) and all(c["passed"] for c in f["checks"]), f
        seg = next(c for c in f["checks"] if c["name"] == "seg_rerender")
        assert seg["metrics"]["global_agreement"] == 1.0 and seg["metrics"]["worst_object"]["iou"] == 1.0   # bit-exact on this machine
    assert rep["thresholds"]["calibrated"] is True and rep["reference_render"]["attempts"] == 1
    assert set(rep["transactions"]) == {"open_challenge", "submit_verdict", "finalize_payout"}
    assert rep["outcome"] == {"action": "paid", "to": "node", "destination": net.env["pubkeys"]["node"], "escrow_moved": s.escrow,
                              "stake_moved": s.stake, "destination_delta": s.escrow + s.stake, "final_status": "Paid"}
    assert s.balances() == {"requester": s.before["requester"] - s.escrow, "node": s.before["node"] + s.escrow}   # the node nets +escrow
    # the 60 s default window leaves ample room: the whole verification took a fraction of it
    assert rep["timing"]["total_s"] < DEFAULT_CHALLENGE_WINDOW_SECS / 2, rep["timing"]
    print("\nhonest gate run: timing", rep["timing"])


def test_tampered_boxes_over_20_frames_are_slashed(scenario, net):
    """Hash-consistent fraud: the node edits annotations.json BEFORE committing, so every hash and proof verifies."""
    s = scenario("fake_boxes", 20, samples=8)
    job = s.node._claim(s.address, s.c.get_request(request=s.address).request)
    s.node._render(job)
    for i in range(s.frames):
        path = job.frame_dir(i) / "annotations.json"
        ann = json.loads(path.read_text())
        visible = [o for o in ann["objects"] if o["bbox"]]
        if visible:
            big = max(visible, key=lambda o: (o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]))
            big["bbox"] = [big["bbox"][0] + 15, big["bbox"][1], big["bbox"][2] + 15, big["bbox"][3]]
            path.write_bytes(canonical_bytes(ann))
    write_leaf_cache(job, build_leaf_cache(job))                 # committed, not caught by hashing
    assert s.node.step() == [f"committed {s.address}"]
    s.server = s.node.start_server()

    s.verifier().run_until_settled(timeout=300)
    rep = s.report()
    assert rep["verdict"] == "Fail"
    failing = s.failing_checks(rep)
    assert {"annotation_plausibility", "segmentation_consistency"} <= failing
    assert not ({"merkle_proof", "scene_match", "hash_consistency", "seg_rerender"} & failing)   # a consistent lie about the BOXES only
    assert rep["outcome"]["action"] == "slashed" and rep["outcome"]["destination_delta"] == s.escrow + s.stake
    assert s.balances() == {"requester": s.before["requester"] + s.stake, "node": s.before["node"] - s.stake}
    assert s.c.get_request(request=s.address).request.status == "Slashed"


@pytest.mark.parametrize("mode", cheats.CHEAT_MODES)
def test_every_cheat_mode_over_20_frames_at_occlusion_3_is_slashed(scenario, net, mode):
    """The node's real --cheat code path, default fraction (0.5), production render settings. Every cheat commits a consistent
    Merkle tree over its fake data, so the proofs and the hashes always verify; only the content checks can catch it."""
    s = scenario(mode, 20, cheat=mode)
    s.honest_commit()                                            # the node renders, cheats, and commits
    req = s.c.get_request(request=s.address).request
    job = Job.load(s.node.out_dir / str(req.request_id))
    assert job.state == "committed"
    bad = cheats.bad_frames(mode, 42, 20)
    assert len(bad) == 10

    s.verifier().run_until_settled(timeout=400)
    rep = s.report()
    sampled = rep["sample"]["indices"]
    per_frame = {f["index"]: sorted(c["name"] for c in f["checks"] if not c["passed"]) for f in rep["frames"]}
    result = {"mode": mode, "verdict": rep["verdict"], "bad_frames": bad, "sampled": sampled, "sampled_bad": [i for i in sampled if i in bad],
              "failed_checks_by_frame": per_frame, "failing_checks": sorted(s.failing_checks(rep)), "outcome": rep["outcome"],
              "balances_before": s.before, "balances_after": s.balances(), "stake": s.stake, "escrow": s.escrow}
    print("\nCHEAT_RESULT " + json.dumps(result, sort_keys=True))
    (s.tmp / "cheat_result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    assert rep["verdict"] == "Fail", f"{mode} was NOT caught: {result}"
    assert not ({"merkle_proof", "hash_consistency"} & s.failing_checks(rep))      # a consistent lie: never a trivial proof/hash failure
    assert rep["outcome"]["action"] == "slashed" and rep["outcome"]["destination_delta"] == s.escrow + s.stake
    assert s.balances() == {"requester": s.before["requester"] + s.stake, "node": s.before["node"] - s.stake}
    assert s.c.get_request(request=s.address).request.status == "Slashed"
    # a clean frame is never failed on content; the one exception is a good frame that a sampled copy duplicates (image_sanity only)
    assert all(i in bad or failing == ["image_sanity"] for i, failing in per_frame.items() if failing), per_frame
    assert mode in ("reuse_frames", "partial") or all(i in bad for i, failing in per_frame.items() if failing), per_frame


def test_a_node_that_changes_a_file_after_committing_is_slashed(scenario, net):
    s = scenario("swapped_after_commit", 7, samples=4).honest_commit()
    job = Job.load(s.node.out_dir / str(s.c.get_request(request=s.address).request.request_id))
    (job.frame_dir(0) / "rgb.png").write_bytes((job.frame_dir(1) / "rgb.png").read_bytes())   # serve frame 1's picture as frame 0

    s.verifier().run_until_settled(timeout=300)
    rep = s.report()
    assert rep["verdict"] == "Fail"
    assert any("frame 0: hash_consistency failed" in r and "rgb.png" in r for r in rep["verdict_reasons"])
    assert any("image_sanity" in r and "duplicated image" in r for r in rep["verdict_reasons"])   # the copy is also a duplicate
    assert s.balances() == {"requester": s.before["requester"] + s.stake, "node": s.before["node"] - s.stake}


def test_a_node_that_cannot_be_reached_is_slashed_as_reveal_unavailable(scenario, net):
    s = scenario("offline", 7)
    s.c.stake_and_claim(s.e["keys"]["node"], request=s.address)
    s.c.submit_commitment(s.e["keys"]["node"], hashlib.sha256(b"a root for frames that were never rendered").digest(), s.frames, request=s.address)

    s.verifier(url="http://127.0.0.1:1").run_until_settled(timeout=300)
    rep = s.report()
    assert rep["verdict"] == "Fail"
    assert [f["status"] for f in rep["frames"]] == ["reveal_unavailable"] + ["not_checked"] * 6
    assert len(rep["verdict_reasons"]) == 1 and "reveal unavailable" in rep["verdict_reasons"][0] and "failed:" not in rep["verdict_reasons"][0]
    assert "after 3 attempts" in rep["frames"][0]["reason"]
    assert rep["reference_render"] == {"attempts": 0, "frames": [], "seconds": 0.0}      # nothing arrived, so nothing was re-rendered
    assert s.balances() == {"requester": s.before["requester"] + s.stake, "node": s.before["node"] - s.stake}
    assert "UNAVAILABLE" in (s.tmp / "verifier" / s.address / "report.txt").read_text()
