"""Node tests. Group 1 needs nothing (synthetic frames on disk, an in-process reveal server). Group 2 runs the real
node against a live localnet with real Blender renders; it skips cleanly without them."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import livenet
from porf.canon import canonical_bytes, hash_json
from porf.chain.client import REPO_ROOT, ChainClient, CreateRequestParams
from porf.merkle import build_proof, leaf_hash, merkle_root, proof_from_json, verify_frame, verify_proof
from porf.node import cheats
from porf.node import client as node_client
from porf.node.client import Node, NodeConfig
from porf.node.job import FRAME_FILES, FrameNotFound, Job, build_leaf_cache, load_leaf_cache, reveal_payload, write_leaf_cache
from porf.node.server import RevealServer
from porf.records import FrameHashes, hash_frame
from porf.render.runner import find_blender
from porf.scene.generator import generate_scene

USDC = 1_000_000


# ============================================================ no validator, no Blender

def fake_request(request_id=7, num_frames=5, requester="Req1111111111111111111111111111111111111111"):
    return SimpleNamespace(request_id=request_id, requester=requester, seed=42, num_frames=num_frames, width=64, height=48, occlusion_level=1)


def make_job(tmp_path, address="ReqAddr1", request_id=7, n=5, state="rendered", seed_bytes=b"A") -> Job:
    """A job directory with synthetic (but canonical-JSON) frame files, as the renderer would leave them."""
    job = Job.create(tmp_path, address, fake_request(request_id, n))
    for i in range(n):
        d = job.frame_dir(i)
        d.mkdir(parents=True)
        (d / "scene.json").write_bytes(canonical_bytes({"frame_index": i, "objects": [{"id": 1, "yaw": 0.5}], "seed": 42}))
        (d / "annotations.json").write_bytes(canonical_bytes({"objects": [{"instance_id": 1, "bbox": [i, i, i + 5, i + 5]}]}))
        (d / "rgb.png").write_bytes(seed_bytes + b"rgb" + bytes([i]) * 200)
        (d / "seg.png").write_bytes(seed_bytes + b"seg" + bytes([i]) * 100)
        (d / "depth.png").write_bytes(seed_bytes + b"depth" + bytes([i]) * 50)
    cache = build_leaf_cache(job)
    write_leaf_cache(job, cache)
    job.update(state=state, merkle_root=cache["merkle_root"], committed_at=time.time())
    return job


def test_frame_hashes_follow_the_spec():
    scene, ann, rgb, seg = {"a": 1}, {"b": [2]}, b"rgb-bytes", b"seg-bytes"
    h = hash_frame(scene, ann, rgb, seg)
    assert h.scene_hash == hashlib.sha256(b'{"a":1}').digest() == hash_json(scene)
    assert h.annotation_hash == hashlib.sha256(b'{"b":[2]}').digest()
    assert h.image_hash == hashlib.sha256(rgb).digest() and h.seg_hash == hashlib.sha256(seg).digest()
    assert h.leaf(3) == leaf_hash(3, h.scene_hash, h.annotation_hash, h.image_hash, h.seg_hash)
    assert FrameHashes.from_hex(h.to_hex()) == h


def test_leaf_cache_equals_an_independent_recomputation(tmp_path):
    job = make_job(tmp_path, n=7)
    leaves = []
    for i in range(7):
        d = job.frame_dir(i)
        parts = [hashlib.sha256((d / f).read_bytes()).digest() for f in ("scene.json", "annotations.json", "rgb.png", "seg.png")]
        leaves.append(hashlib.sha256(b"\x00" + i.to_bytes(4, "little") + b"".join(parts)).digest())
    cache = load_leaf_cache(job)
    assert cache["merkle_root"] == merkle_root(leaves).hex()  # merkle_root() is itself pinned by the vectors in test_merkle
    assert [f["leaf_hash"] for f in cache["frames"]] == [l.hex() for l in leaves]
    assert cache["frame_count"] == 7


def verify_payload(payload: dict, onchain_root: bytes, frame_count: int) -> bool:
    """What the verifier will do with a reveal: recompute hashes from the served CONTENT, then verify the proof."""
    h = hash_frame(payload["scene"], payload["annotations"], base64.b64decode(payload["rgb_png"]), base64.b64decode(payload["seg_png"]))
    return verify_frame(payload["frame_index"], h.scene_hash, h.annotation_hash, h.image_hash, h.seg_hash,
                        proof_from_json(payload["proof"]), onchain_root, frame_count)


def test_reveal_payload_verifies_for_every_frame_and_every_count(tmp_path):
    for n in (1, 2, 3, 7, 8):
        job = make_job(tmp_path / f"n{n}", n=n)
        root = bytes.fromhex(load_leaf_cache(job)["merkle_root"])
        for i in range(n):
            p = reveal_payload(job, i)
            assert p["frame_index"] == i and p["frame_count"] == n and p["merkle_root"] == root.hex()
            assert set(p) == {"request", "frame_index", "frame_count", "merkle_root", "leaf", "proof", "scene", "annotations", "rgb_png", "seg_png", "depth_png"}
            assert verify_payload(p, root, n), (n, i)
            assert base64.b64decode(p["depth_png"]).startswith(b"Adepth")


def test_reveal_rejects_out_of_range_indexes(tmp_path):
    job = make_job(tmp_path, n=5)
    for bad in (5, 6, 10**6, -1):
        with pytest.raises(FrameNotFound):
            reveal_payload(job, bad)


def test_reveal_does_not_hide_a_file_changed_after_the_commit(tmp_path):
    job = make_job(tmp_path, n=5)
    root = bytes.fromhex(load_leaf_cache(job)["merkle_root"])
    (job.frame_dir(2) / "rgb.png").write_bytes(b"swapped after commit")
    p = reveal_payload(job, 2)  # still served, as-is
    assert base64.b64decode(p["rgb_png"]) == b"swapped after commit"
    assert not verify_payload(p, root, 5)  # ... and the verifier's recomputation rejects it
    # the leaf block still states what was COMMITTED, so the served bytes visibly disagree with it
    committed = load_leaf_cache(job)["frames"][2]
    assert p["leaf"] == {k: committed[k] for k in ("frame_index", "scene_hash", "annotation_hash", "image_hash", "seg_hash", "leaf_hash")}
    assert p["leaf"]["image_hash"] != hashlib.sha256(base64.b64decode(p["rgb_png"])).hexdigest()
    assert verify_payload(reveal_payload(job, 1), root, 5)  # other frames are unaffected


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_server_serves_committed_frames_and_404s_the_rest(tmp_path):
    job = make_job(tmp_path, n=5, state="committed")
    make_job(tmp_path, address="ReqAddr2", request_id=8, n=3, state="rendered")  # rendered but NOT committed: never served
    srv = RevealServer(tmp_path, port=0).start()
    try:
        status, body = get(srv.url + "/frame/3")
        assert status == 200 and body["frame_index"] == 3 and body["request"] == "ReqAddr1"
        assert verify_payload(body, bytes.fromhex(load_leaf_cache(job)["merkle_root"]), 5)

        assert get(srv.url + "/frame/5")[0] == 404          # == frame_count
        assert get(srv.url + "/frame/99999")[0] == 404
        assert get(srv.url + "/frame/-1")[0] == 404
        assert get(srv.url + "/frame/abc")[0] == 404
        assert get(srv.url + "/frame/")[0] == 404
        assert get(srv.url + "/nope")[0] == 404
        assert get(srv.url + "/frame/0?request=ReqAddr2")[0] == 404   # known job, but not committed
        assert get(srv.url + "/frame/0?request=Unknown")[0] == 404
        status, body = get(srv.url + "/frame/5")
        assert "out of range" in body["error"]

        status, health = get(srv.url + "/health")
        assert (status, health) == (200, {"ok": True, "committed": ["ReqAddr1"]})
    finally:
        srv.stop()


def test_server_with_nothing_committed_returns_404(tmp_path):
    srv = RevealServer(tmp_path / "missing", port=0).start()
    try:
        assert get(srv.url + "/frame/0")[0] == 404
        assert get(srv.url + "/health")[1] == {"ok": True, "committed": []}
    finally:
        srv.stop()


def test_server_selects_by_request_and_defaults_to_the_latest_commit(tmp_path):
    a = make_job(tmp_path, address="ReqA", request_id=1, n=3, state="committed", seed_bytes=b"A")
    b = make_job(tmp_path, address="ReqB", request_id=2, n=4, state="committed", seed_bytes=b"B")
    a.update(committed_at=1000.0)
    b.update(committed_at=2000.0)
    srv = RevealServer(tmp_path, port=0).start()
    try:
        assert get(srv.url + "/frame/0")[1]["request"] == "ReqB"                 # latest commit by default
        assert get(srv.url + "/frame/0?request=ReqA")[1]["request"] == "ReqA"
        assert get(srv.url + "/frame/3")[0] == 200 and get(srv.url + "/frame/3?request=ReqA")[0] == 404   # ReqA has 3 frames
    finally:
        srv.stop()


def test_job_state_machine_and_collision_guard(tmp_path):
    job = Job.create(tmp_path, "ReqX", fake_request(request_id=9))
    assert job.state == "claimed" and (tmp_path / "9" / "job.json").exists()
    with pytest.raises(ValueError):
        job.update(state="bogus")
    job.update(state="rendered", merkle_root="ab" * 32)
    assert Job.load(tmp_path / "9").data["merkle_root"] == "ab" * 32           # persisted
    assert Job.create(tmp_path, "ReqX", fake_request(request_id=9)).state == "rendered"   # same request: resumes, not reset
    with pytest.raises(RuntimeError, match="collides"):
        Job.create(tmp_path, "OtherRequest", fake_request(request_id=9))      # same id, different request
    with pytest.raises(RuntimeError, match="collides"):
        Job.guard(tmp_path, "OtherRequest", 9)
    Job.guard(tmp_path, "ReqX", 9)                                             # no error for the owner
    assert not list(tmp_path.glob("**/*.tmp"))                                 # atomic writes leave no temp files


def test_unreadable_job_files_are_ignored(tmp_path):
    make_job(tmp_path, n=2)
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "job.json").write_text("{not json")
    assert [j.request for j in Job.scan(tmp_path)] == ["ReqAddr1"]


def test_cheat_hooks_are_noops_for_now(tmp_path, caplog):
    assert cheats.before_render("reuse_frames", 42) == 42
    job = make_job(tmp_path, n=3)
    before = {p: p.read_bytes() for p in job.dir.rglob("*") if p.is_file()}
    assert cheats.after_render("reuse_frames", job.dir, 3) is None
    assert {p: p.read_bytes() for p in job.dir.rglob("*") if p.is_file()} == before
    with caplog.at_level("WARNING"):
        cheats.announce("fake_boxes")
    assert "HONESTLY" in caplog.text
    assert set(cheats.CHEAT_MODES) == {"reuse_frames", "fake_boxes", "wrong_seed", "partial"}


# ============================================================ real node, real localnet, real Blender

needs_blender = pytest.mark.skipif(find_blender() is None, reason="Blender not installed")
live = [pytest.mark.needs_validator, pytest.mark.slow, needs_blender]


@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


def make_request(net, num_frames: int, seed: int = 42, occlusion: int = 3) -> tuple[int, str]:
    rid = next(net.ids)
    params = CreateRequestParams(request_id=rid, seed=seed, num_frames=num_frames, price_per_frame=USDC, claim_duration=600,
                                 commit_duration=900, challenge_window_secs=30, occlusion_level=occlusion)
    return rid, net.client.create_request(net.env["keys"]["requester"], params)["request"]


def new_node(net, out: Path, address: str, samples: int = 4, chain=None) -> Node:
    cfg = NodeConfig(keypair=net.env["keys"]["node"], out_dir=out, samples=samples, only_request=address, port=0, announce_file=None)
    return Node(cfg, chain or net.client)


@pytest.fixture(scope="module")
def gate(net, tmp_path_factory):
    """The gate scenario, run once: a 10-frame request picked up, rendered and committed by a real node."""
    out = tmp_path_factory.mktemp("gate_out")
    rid, address = make_request(net, num_frames=10)
    before = net.client.get_request(request=address)
    node = new_node(net, out, address, samples=8)
    actions = node.step()
    server = RevealServer(out, port=0).start()
    yield SimpleNamespace(net=net, out=out, rid=rid, address=address, node=node, actions=actions, server=server, before=before)
    server.stop()


def disk_root(job_dir: Path, n: int) -> bytes:
    """Independent recomputation: read the files on disk, hash them with hashlib, build the tree."""
    leaves = []
    for i in range(n):
        d = job_dir / f"frame_{i}"
        parts = [hashlib.sha256((d / f).read_bytes()).digest() for f in ("scene.json", "annotations.json", "rgb.png", "seg.png")]
        leaves.append(hashlib.sha256(b"\x00" + i.to_bytes(4, "little") + b"".join(parts)).digest())
    return merkle_root(leaves)


@pytest.mark.slow
@pytest.mark.needs_validator
@needs_blender
class TestGate:
    def test_request_is_picked_up_rendered_and_committed(self, gate):
        assert gate.actions == [f"claimed {gate.address}", f"rendered {gate.address}", f"committed {gate.address}"]
        view = gate.net.client.get_request(request=gate.address)
        assert view.request.status == "Committed" and view.request.node == gate.net.env["pubkeys"]["node"]
        assert view.stake_vault_balance == view.request.stake_amount == 2 * USDC   # the node's stake is locked
        sub = gate.net.client.get_submission(request=gate.address)
        assert (sub.node, sub.frame_count, sub.verdict, sub.sample_count) == (gate.net.env["pubkeys"]["node"], 10, "Pending", 0)

    def test_onchain_root_equals_the_root_recomputed_from_the_files_on_disk(self, gate):
        sub = gate.net.client.get_submission(request=gate.address)
        job_dir = gate.out / str(gate.rid)
        for i in range(10):  # exactly the files the renderer promised
            assert sorted(p.name for p in (job_dir / f"frame_{i}").iterdir()) == ["annotations.json", "depth.png", "rgb.png", "scene.json", "seg.png"]
        assert disk_root(job_dir, 10).hex() == sub.merkle_root
        job = Job.load(job_dir)
        assert (job.state, job.data["merkle_root"], job.data["commit_slot"]) == ("committed", sub.merkle_root, sub.commit_slot)
        assert load_leaf_cache(job)["merkle_root"] == sub.merkle_root

    def test_every_frame_proof_verifies_against_the_onchain_root(self, gate):
        from PIL import Image
        import io

        sub = gate.net.client.get_submission(request=gate.address)
        root, req = bytes.fromhex(sub.merkle_root), gate.net.client.get_request(request=gate.address).request
        for i in range(10):
            status, p = get(f"{gate.server.url}/frame/{i}?request={gate.address}")
            assert status == 200
            assert p["merkle_root"] == sub.merkle_root and p["frame_count"] == 10
            assert verify_payload(p, root, 10), f"frame {i}"
            # served content is what an honest node rendered from the on-chain parameters
            assert p["scene"] == generate_scene(req.seed, i, req.occlusion_level, req.width, req.height)
            for key in ("rgb_png", "seg_png", "depth_png"):
                assert Image.open(io.BytesIO(base64.b64decode(p[key]))).size == (req.width, req.height)
            ids = {o["id"] for o in p["scene"]["objects"]}
            assert {o["instance_id"] for o in p["annotations"]["objects"]} == ids
        # the default (no ?request=) is the latest committed job, which is this one
        assert get(f"{gate.server.url}/frame/0")[1]["request"] == gate.address

    def test_out_of_range_indexes_are_404(self, gate):
        for path in ("/frame/10", "/frame/11", "/frame/99999", "/frame/-1", "/frame/abc"):
            assert get(gate.server.url + path)[0] == 404, path

    def test_a_second_step_does_nothing(self, gate, monkeypatch):
        monkeypatch.setattr(node_client, "render_frames", lambda *a, **k: pytest.fail("must not render again"))
        assert gate.node.step() == []


@pytest.mark.slow
@pytest.mark.needs_validator
@needs_blender
class TestRestart:
    def test_restart_after_rendering_skips_straight_to_commit(self, net, tmp_path, monkeypatch):
        rid, address = make_request(net, num_frames=2)
        a = new_node(net, tmp_path, address)
        job = a._claim(address, net.client.get_request(request=address).request)
        a._render(job)                                                           # ... and "crash" before committing
        assert Job.load(tmp_path / str(rid)).state == "rendered"
        assert net.client.get_request(request=address).request.status == "Claimed"

        monkeypatch.setattr(node_client, "render_frames", lambda *a, **k: pytest.fail("restart must not render again"))
        actions = new_node(net, tmp_path, address).step()
        assert actions == [f"committed {address}"]
        assert net.client.get_request(request=address).request.status == "Committed"
        assert net.client.get_submission(request=address).merkle_root == Job.load(tmp_path / str(rid)).data["merkle_root"]

    def test_restart_after_commit_skips_straight_to_serving(self, net, tmp_path, monkeypatch):
        rid, address = make_request(net, num_frames=2)
        assert len(new_node(net, tmp_path, address).step()) == 3
        monkeypatch.setattr(node_client, "render_frames", lambda *a, **k: pytest.fail("must not render"))
        chain = ChainClient(rpc_url=net.env["rpc_url"])
        monkeypatch.setattr(chain, "submit_commitment", lambda *a, **k: pytest.fail("must not commit again"))
        monkeypatch.setattr(chain, "stake_and_claim", lambda *a, **k: pytest.fail("must not claim again"))
        node = new_node(net, tmp_path, address, chain=chain)
        assert node.step() == []
        srv = RevealServer(tmp_path, port=0).start()
        try:
            root = bytes.fromhex(net.client.get_submission(request=address).merkle_root)
            assert all(verify_payload(get(f"{srv.url}/frame/{i}")[1], root, 2) for i in range(2))
        finally:
            srv.stop()

    def test_commit_that_landed_but_was_not_recorded_is_reconciled(self, net, tmp_path, monkeypatch):
        rid, address = make_request(net, num_frames=2)
        new_node(net, tmp_path, address).step()
        job = Job.load(tmp_path / str(rid))
        job.update(state="rendered")                                           # as if the crash came right after the tx
        chain = ChainClient(rpc_url=net.env["rpc_url"])
        monkeypatch.setattr(chain, "submit_commitment", lambda *a, **k: pytest.fail("must not submit twice"))
        assert new_node(net, tmp_path, address, chain=chain).step() == [f"committed {address}"]
        assert Job.load(tmp_path / str(rid)).state == "committed"

    def test_second_commit_attempt_is_reconciled_not_fatal(self, net, tmp_path):
        """Same as above but the node does not know the commit landed, so it really sends the tx and gets 'already in use'."""
        rid, address = make_request(net, num_frames=2)
        new_node(net, tmp_path, address).step()
        Job.load(tmp_path / str(rid)).update(state="rendered")
        node = new_node(net, tmp_path, address)
        # force the "still Claimed" view of the world: the node then really sends submit_commitment
        job = Job.load(tmp_path / str(rid))
        class Stale:
            status = "Claimed"
        node._commit(job, Stale())
        assert Job.load(tmp_path / str(rid)).state == "committed"

    def test_claim_without_a_local_job_is_adopted(self, net, tmp_path):
        rid, address = make_request(net, num_frames=2)
        net.client.stake_and_claim(net.env["keys"]["node"], request=address)    # claimed on chain; the node "crashed" before job.json
        assert not (tmp_path / str(rid)).exists()
        actions = new_node(net, tmp_path, address).step()
        assert actions == [f"adopted {address}", f"rendered {address}", f"committed {address}"]
        assert net.client.get_request(request=address).request.status == "Committed"

    def test_a_request_claimed_by_someone_else_is_not_touched(self, net, tmp_path):
        rid, address = make_request(net, num_frames=2)
        rival_key = "keys/requester.json"                                       # any other funded key claiming it first
        req = net.client.get_request(request=address).request
        net.client.stake_and_claim(rival_key, request=address, node_token=net.env["token_accounts"]["requester"])
        assert new_node(net, tmp_path, address).step() == []
        assert not (tmp_path / str(rid)).exists()
        assert net.client.get_request(request=address).request.node == net.env["pubkeys"]["requester"]


@pytest.mark.slow
@pytest.mark.needs_validator
@needs_blender
def test_node_process_end_to_end(net, tmp_path):
    """The real entry point: `python -m porf.node.client` as a subprocess, announcing its URL, claiming, committing, serving."""
    rid, address = make_request(net, num_frames=2)
    announce = tmp_path / "reveal.json"
    cmd = [sys.executable, "-m", "porf.node.client", "--keypair", net.env["keys"]["node"], "--out", str(tmp_path / "out"), "--poll", "1",
           "--samples", "4", "--port", "0", "--only-request", address, "--announce-file", str(announce), "--cheat", "partial"]
    proc = subprocess.Popen(cmd, env=dict(os.environ, PORF_RPC_URL=net.env["rpc_url"]), cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 180
        while time.time() < deadline and net.client.get_request(request=address).request.status != "Committed":
            assert proc.poll() is None, proc.stdout.read()
            time.sleep(1)
        assert net.client.get_request(request=address).request.status == "Committed"
        info = json.loads(announce.read_text())
        assert info["node"] == net.env["pubkeys"]["node"] and info["url"].startswith("http://127.0.0.1:")
        root = bytes.fromhex(net.client.get_submission(request=address).merkle_root)
        assert all(verify_payload(get(f"{info['url']}/frame/{i}")[1], root, 2) for i in range(2))
        assert get(f"{info['url']}/frame/2")[0] == 404
    finally:
        proc.terminate()
        out = proc.communicate(timeout=20)[0]
    assert "HONESTLY" in out  # --cheat partial is accepted but is a no-op today, and says so
