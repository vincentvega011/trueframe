"""The five node cheat modes (docs/PROTOCOL.md section 8): deterministic frame selection, the fraction rule, no effect without a
mode, and what each mode does to the files. The verifier's reaction to each is covered live in test_verifier_flow.py.
Blender-free: the fixture node writes synthetic frames."""
import json
import math
from itertools import combinations
from pathlib import Path

import pytest

from porf.canon import canonical_bytes
from porf.node import cheats
from porf.node.cheats import CHEAT_MODES, bad_frames, render_plan, wrong_seed_for
from porf.scene.generator import generate_scene
from porf.verifier import checks as C

from verifier_fixtures import H, W, HonestNode

N, SEED = 20, 42


def snapshot(d: Path) -> dict:
    return {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("frame_*/*") if p.is_file()}


# ---- which frames

@pytest.mark.parametrize("mode", CHEAT_MODES)
def test_default_corrupts_at_least_half_of_the_frames(mode):
    assert len(bad_frames(mode, SEED, N)) == 10
    assert len(bad_frames(mode, SEED, 7)) == 4 and len(bad_frames(mode, SEED, 1)) == (0 if mode in ("reuse_frames", "partial") else 1)


@pytest.mark.parametrize("mode", CHEAT_MODES)
def test_selection_is_deterministic_per_request_and_varies_between_requests(mode):
    assert bad_frames(mode, SEED, N) == bad_frames(mode, SEED, N)
    assert bad_frames(mode, SEED, N, 0.5) == bad_frames(mode, SEED, N)
    if mode != "partial":
        assert len({tuple(bad_frames(mode, s, N)) for s in range(1, 8)}) > 1


@pytest.mark.parametrize("mode", CHEAT_MODES)
@pytest.mark.parametrize("fraction,expected", [(0.05, 1), (0.1, 2), (0.5, 10), (0.55, 11), (0.9, 18)])
def test_fraction_counts_round_up(mode, fraction, expected):
    bad = bad_frames(mode, SEED, N, fraction)
    assert len(bad) == expected and bad == sorted(set(bad)) and all(0 <= i < N for i in bad)


def test_bad_frames_hide_in_the_middle_until_the_fraction_forces_the_ends():
    for mode in ("reuse_frames", "fake_boxes", "wrong_seed", "no_occlusion"):
        assert not {0, N - 1} & set(bad_frames(mode, SEED, N, 0.9))      # 18 = all of the middle
        assert {0, N - 1} <= set(bad_frames(mode, SEED, N, 1.0)) if mode != "reuse_frames" else True
    assert bad_frames("fake_boxes", SEED, N, 1.0) == list(range(N))
    assert bad_frames("reuse_frames", SEED, N, 1.0) == bad_frames("reuse_frames", SEED, N, 0.95) and len(bad_frames("reuse_frames", SEED, N, 1.0)) == 19


def test_partial_corrupts_the_tail():
    assert bad_frames("partial", SEED, N) == list(range(10, 20))
    assert bad_frames("partial", SEED, N, 0.25) == list(range(15, 20))
    assert bad_frames("partial", SEED, N, 1.0) == list(range(1, 20))     # frame 0 stays real: a copy needs a source


@pytest.mark.parametrize("bad", [0, -0.1, 1.5, float("nan")])
def test_fraction_is_validated(bad):
    with pytest.raises(ValueError):
        bad_frames("reuse_frames", SEED, N, bad)


def test_render_plans():
    assert render_plan(None, SEED, N) == [(SEED, list(range(N)))]
    for mode in ("reuse_frames", "fake_boxes", "no_occlusion"):
        assert render_plan(mode, SEED, N) == [(SEED, list(range(N)))]    # they render everything honestly, then rewrite
    assert render_plan("partial", SEED, N) == [(SEED, list(range(10)))]  # only the front part is rendered
    plan = render_plan("wrong_seed", SEED, N)
    bad = bad_frames("wrong_seed", SEED, N)
    assert plan == [(SEED, [i for i in range(N) if i not in bad]), (wrong_seed_for(SEED), bad)]
    assert wrong_seed_for(SEED) != SEED and wrong_seed_for(SEED) == wrong_seed_for(SEED) and 0 <= wrong_seed_for(SEED) < 2 ** 64
    assert render_plan("wrong_seed", SEED, N, 1.0) == [(wrong_seed_for(SEED), list(range(N)))]


def test_the_single_bad_frame_and_half_detection_probabilities_in_the_docs():
    """PROTOCOL.md section 8: N=20, k=7 (frames 0 and 19 plus 5 of the 18 middle ones), bad frames hidden in the middle."""
    def p(b):
        return 1 - math.comb(18 - b, 5) / math.comb(18, 5)
    assert round(p(1), 3) == 0.278 and round(p(10), 4) == 0.9935


# ---- no effect without --cheat

def test_without_a_cheat_nothing_changes(tmp_path):
    node = HonestNode(tmp_path, n=6)
    before = snapshot(node.job.dir)
    assert cheats.after_render(None, node.job.dir, 6, seed=SEED) is None
    assert snapshot(node.job.dir) == before


# ---- what each mode does to the files

def frames_changed(before: dict, after: dict, name: str, n: int) -> list[int]:
    return [i for i in range(n) if before[f"frame_{i}/{name}"] != after[f"frame_{i}/{name}"]]


def run(mode, tmp_path, n=10, fraction=None):
    node = HonestNode(tmp_path, n=n, seed=SEED)
    before = snapshot(node.job.dir)
    cheats.after_render(mode, node.job.dir, n, SEED, fraction, 3, W, H)
    node.recommit()                                                      # committed consistently
    return node, before, snapshot(node.job.dir), bad_frames(mode, SEED, n, fraction)


def test_reuse_frames_copies_images_and_annotations_from_a_good_frame(tmp_path):
    node, before, after, bad = run("reuse_frames", tmp_path)
    good = [i for i in range(10) if i not in bad]
    sources = cheats._copy_sources(bad, 10)
    assert set(sources) == set(bad) and set(sources.values()) <= set(good)
    for b, s in sources.items():
        for name in ("rgb.png", "seg.png", "annotations.json"):
            assert after[f"frame_{b}/{name}"] == before[f"frame_{s}/{name}"]
        assert after[f"frame_{b}/scene.json"] == before[f"frame_{b}/scene.json"]    # its own scene stays the honest one
    assert frames_changed(before, after, "rgb.png", 10) == bad                       # every other frame is byte-identical
    assert all(after[k] == before[k] for k in before if int(k.split("/")[0][6:]) in good)
    # the lie is committed consistently: the proof, the scene and the hashes of every frame verify
    for i in range(10):
        res = {r.name: r for r in C.run_checks(node.reveal(i), i, node.ctx())}
        assert res["merkle_proof"].passed and res["scene_match"].passed and res["hash_consistency"].passed, i
        assert (i not in bad) or not (res["annotation_plausibility"].passed and res["segmentation_consistency"].passed and res["seg_rerender"].passed), i


def test_fake_boxes_shifts_boxes_and_keeps_the_seg_honest(tmp_path):
    node, before, after, bad = run("fake_boxes", tmp_path)
    assert frames_changed(before, after, "annotations.json", 10) == bad
    assert frames_changed(before, after, "seg.png", 10) == [] and frames_changed(before, after, "rgb.png", 10) == []
    for i in bad:
        old = json.loads(before[f"frame_{i}/annotations.json"])["objects"]
        new = json.loads(after[f"frame_{i}/annotations.json"])["objects"]
        assert len(old) == len(new)
        assert all(n["bbox"] is not None and 0 <= n["bbox"][0] < n["bbox"][2] <= W and 0 <= n["bbox"][1] < n["bbox"][3] <= H for n in new)
        assert sum(1 for o, n in zip(old, new) if o["bbox"] != n["bbox"]) >= len(old) - 1     # every box is fabricated or moved
        assert [o["instance_id"] for o in old] == [n["instance_id"] for n in new]
    assert not {r.name: r for r in C.run_checks(node.reveal(bad[0]), bad[0], node.ctx())}["segmentation_consistency"].passed   # the boxes no longer match seg.png


def test_partial_copies_a_rendered_frame_over_each_tail_frame(tmp_path):
    node, before, after, bad = run("partial", tmp_path)
    assert bad == list(range(5, 10))
    for b, s in cheats._copy_sources(bad, 10).items():
        assert s < 5 and after[f"frame_{b}/rgb.png"] == before[f"frame_{s}/rgb.png"]
    assert frames_changed(before, after, "rgb.png", 10) == [b for b in bad if before[f"frame_{b}/rgb.png"] != before[f"frame_{cheats._copy_sources(bad, 10)[b]}/rgb.png"]]


def test_no_occlusion_rewrites_only_the_selected_frames(tmp_path):
    node, before, after, bad = run("no_occlusion", tmp_path)
    assert frames_changed(before, after, "seg.png", 10) == bad
    assert frames_changed(before, after, "rgb.png", 10) == []


def test_wrong_seed_frames_come_from_another_scene():
    """render_plan makes the render itself use the other seed; the scene the node then commits is not the requested one."""
    plan = render_plan("wrong_seed", SEED, N)
    seed_bad, bad = plan[1]
    for i in bad:
        honest = canonical_bytes(generate_scene(SEED, i, 3, W, H))
        assert canonical_bytes(generate_scene(seed_bad, i, 3, W, H)) != honest


def test_every_mode_is_deterministic_on_disk(tmp_path):
    for mode in ("reuse_frames", "fake_boxes", "partial", "no_occlusion"):
        _, _, a, _ = run(mode, tmp_path / "a" / mode)
        _, _, b, _ = run(mode, tmp_path / "b" / mode)
        assert a == b, mode
