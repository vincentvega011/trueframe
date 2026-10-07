"""Each check against an honest reveal and against hand-tampered ones. The honest node is Blender-free
(verifier_fixtures.HonestNode); tampering either changes what the node COMMITS (a hash-consistent lie, built with `mutate`)
or what it SERVES after the commit (edit the raw reveal)."""
import base64
import copy
import io

import numpy as np
import pytest
from PIL import Image

from porf.records import FrameHashes
from porf.scene.camera import Camera
from porf.scene.generator import generate_scene
from porf.scene.projection import OK, project_box
from porf.verifier import checks as C
from porf.verifier.checks import (CHECK_NAMES, ClassThresholds, FrameFacts, SeenImages, Thresholds, VerifyContext, run_checks)
from porf.verifier.reveal import Reveal
from verifier_fixtures import ADDRESS, H, W, HonestNode, other_root_submission, png_bytes


def run_one(check, reveal, index, ctx):
    return check(reveal, index, ctx, FrameFacts.build(reveal, index, ctx))


def results(reveal, index, ctx):
    return {r.name: r for r in run_checks(reveal, index, ctx)}


def failed(reveal, index, ctx):
    return sorted(name for name, r in results(reveal, index, ctx).items() if not r.passed)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture(scope="module")
def honest(tmp_path_factory):
    return HonestNode(tmp_path_factory.mktemp("honest"), n=8)


def biggest_visible(files):
    """The annotation of the visible object with the largest mask box (a stable target for box tampering)."""
    return max((o for o in files.annotations["objects"] if o["bbox"]), key=lambda o: (o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]))


# ---------------------------------------------------------------- the honest baseline

def test_an_honest_node_passes_all_six_checks_on_every_frame(honest):
    ctx = honest.ctx()
    for i in range(honest.n):
        res = run_checks(honest.reveal(i), i, ctx)
        assert [r.name for r in res] == list(CHECK_NAMES)
        assert all(r.passed for r in res), [r for r in res if not r.passed]
        assert all(set(r.to_dict()) >= {"name", "passed", "detail"} and r.detail for r in res)


def test_honest_frames_pass_across_seeds_and_occlusion_levels(tmp_path):
    for seed, occ in ((7, 3), (99, 1), (3, 0), (1234, 2)):
        node = HonestNode(tmp_path / f"{seed}_{occ}", n=6, seed=seed, occlusion=occ)
        ctx = node.ctx()
        for i in range(6):
            assert failed(node.reveal(i), i, ctx) == [], (seed, occ, i)


def test_a_malformed_reveal_fails_checks_instead_of_crashing(honest):
    for raw in ({}, {"frame_index": 2}, {"frame_index": 2, "scene": "nope", "proof": 7}, []):
        res = run_checks(Reveal.parse(raw), 2, honest.ctx())
        assert [r.name for r in res] == list(CHECK_NAMES)
        assert not any(r.passed for r in res if r.name in ("merkle_proof", "hash_consistency"))
        assert not any("crashed" in r.detail for r in res), [r.detail for r in res]


# ---------------------------------------------------------------- 1. Merkle proof

def test_merkle_proof_rejects_a_flipped_proof_byte(honest):
    raw = honest.raw(3)
    h = raw["proof"][0]["sibling_hash"]
    raw["proof"][0]["sibling_hash"] = ("0" if h[0] != "0" else "1") + h[1:]
    r = run_one(C.check_merkle_proof, Reveal.parse(raw), 3, honest.ctx())
    assert not r.passed and "does not verify" in r.detail


def test_merkle_proof_rejects_flipped_sides_and_wrong_lengths(honest):
    raw = honest.raw(3)
    flipped = copy.deepcopy(raw)
    flipped["proof"][1]["sibling_is_left"] = not flipped["proof"][1]["sibling_is_left"]
    assert not run_one(C.check_merkle_proof, Reveal.parse(flipped), 3, honest.ctx()).passed
    for proof in ([], raw["proof"][:-1], raw["proof"] + raw["proof"][:1]):
        bad = {**raw, "proof": proof}
        assert not run_one(C.check_merkle_proof, Reveal.parse(bad), 3, honest.ctx()).passed


@pytest.mark.parametrize("proof", ["not a list", [{"sibling_hash": "zz" * 32, "sibling_is_left": True}], [{"sibling_hash": "00" * 32}], None])
def test_merkle_proof_rejects_malformed_proofs(honest, proof):
    raw = {**honest.raw(3), "proof": proof}
    r = run_one(C.check_merkle_proof, Reveal.parse(raw), 3, honest.ctx())
    assert not r.passed and ("malformed" in r.detail or "proof" in r.detail)


def test_merkle_proof_rejects_a_reveal_for_the_wrong_index(honest):
    r = run_one(C.check_merkle_proof, honest.reveal(4), 3, honest.ctx())   # asked for frame 3, served frame 4
    assert not r.passed and "frame 4" in r.detail and "frame 3 was requested" in r.detail


def test_merkle_proof_rejects_a_leaf_whose_index_is_not_the_requested_one(honest):
    raw = honest.raw(4)
    raw["frame_index"] = 3                                   # the node CLAIMS frame 3, but the leaf says 4
    r = run_one(C.check_merkle_proof, Reveal.parse(raw), 3, honest.ctx())
    assert not r.passed and "frame_index is 4, expected 3" in r.detail


def test_merkle_proof_rejects_a_relabelled_leaf(honest):
    # patch both indexes to 3 but leave the leaf hash: it no longer matches its own fields
    raw = honest.raw(4)
    raw["frame_index"] = raw["leaf"]["frame_index"] = 3
    r = run_one(C.check_merkle_proof, Reveal.parse(raw), 3, honest.ctx())
    assert not r.passed and "leaf_hash does not equal" in r.detail
    # ... and a node that also recomputes the leaf hash for index 3 still fails, because the proof is for position 4
    raw["leaf"]["leaf_hash"] = FrameHashes.from_hex(raw["leaf"]).leaf(3).hex()
    r = run_one(C.check_merkle_proof, Reveal.parse(raw), 3, honest.ctx())
    assert not r.passed and "does not verify" in r.detail


def test_merkle_proof_uses_the_onchain_root_not_the_one_the_node_reports(honest, tmp_path):
    raw = honest.raw(2)
    raw["merkle_root"] = "00" * 32
    assert "states root" in run_one(C.check_merkle_proof, Reveal.parse(raw), 2, honest.ctx()).detail
    # a perfectly valid proof, but of a different tree (other seed), against this request's on-chain root
    other = HonestNode(tmp_path, n=8, seed=43)
    r = run_one(C.check_merkle_proof, other.reveal(2), 2, honest.ctx())
    assert not r.passed
    # the same reveal is fine against the root it was actually committed under
    assert run_one(C.check_merkle_proof, other.reveal(2), 2, other.ctx()).passed


def test_merkle_proof_uses_the_onchain_frame_count(honest):
    ctx = honest.ctx()
    ctx.submission = other_root_submission(honest, honest.submission.merkle_root)
    object.__setattr__(ctx.submission, "frame_count", 9)   # proof length for 9 leaves differs from the 8-leaf proof
    assert not run_one(C.check_merkle_proof, honest.reveal(3), 3, ctx).passed


# ---------------------------------------------------------------- 2. scene match

def test_scene_match_catches_a_hash_consistent_wrong_seed(tmp_path):
    node = HonestNode(tmp_path, n=6, seed=42, scene_seed=43)   # renders and COMMITS scenes from seed 43; the request says 42
    ctx, rv = node.ctx(), node.reveal(2)
    res = results(rv, 2, ctx)
    assert not res["scene_match"].passed and "first difference" in res["scene_match"].detail
    assert res["merkle_proof"].passed and res["hash_consistency"].passed   # a clean-looking lie: only the regenerated scene exposes it
    assert failed(rv, 2, ctx)[0] == "scene_match" or "scene_match" in failed(rv, 2, ctx)


def test_scene_match_catches_a_scene_swapped_after_the_commit(honest):
    raw = honest.raw(2)
    raw["scene"] = generate_scene(43, 2, 3, W, H)
    res = results(Reveal.parse(raw), 2, honest.ctx())
    assert not res["scene_match"].passed and not res["hash_consistency"].passed


def test_scene_match_catches_a_changed_committed_scene_hash(honest):
    raw = honest.raw(2)
    raw["leaf"]["scene_hash"] = "00" * 32
    r = run_one(C.check_scene_match, Reveal.parse(raw), 2, honest.ctx())
    assert not r.passed and "differs from the scene regenerated" in r.detail


def test_scene_match_is_per_frame(honest):
    assert not run_one(C.check_scene_match, honest.reveal(2), 3, honest.ctx()).passed   # frame 2's scene is not frame 3's


# ---------------------------------------------------------------- 3. hash consistency

def tamper(raw, which):
    if which == "scene.json":
        raw["scene"]["objects"][0]["yaw_deg"] = (raw["scene"]["objects"][0]["yaw_deg"] + 1.0) % 360
    elif which == "annotations.json":
        raw["annotations"]["objects"][0]["visibility"] = 0.5
    elif which == "rgb.png":
        raw["rgb_png"] = b64(base64.b64decode(raw["rgb_png"]) + b"\x00")
    elif which == "seg.png":
        raw["seg_png"] = b64(base64.b64decode(raw["seg_png"]) + b"\x00")


@pytest.mark.parametrize("which", ["scene.json", "annotations.json", "rgb.png", "seg.png"])
def test_hash_consistency_names_exactly_the_file_that_changed(honest, which):
    raw = honest.raw(5)
    tamper(raw, which)
    r = run_one(C.check_hash_consistency, Reveal.parse(raw), 5, honest.ctx())
    assert not r.passed
    for other in ("scene.json", "annotations.json", "rgb.png", "seg.png"):
        assert (other in r.detail) == (other == which), (which, r.detail)


def test_hash_consistency_lists_several_changed_files_and_missing_ones(honest):
    raw = honest.raw(5)
    tamper(raw, "rgb.png")
    tamper(raw, "seg.png")
    detail = run_one(C.check_hash_consistency, Reveal.parse(raw), 5, honest.ctx()).detail
    assert "rgb.png" in detail and "seg.png" in detail and "scene.json" not in detail
    for key in ("scene", "annotations", "rgb_png", "seg_png"):
        raw = honest.raw(5)
        del raw[key]
        r = run_one(C.check_hash_consistency, Reveal.parse(raw), 5, honest.ctx())
        assert not r.passed and "cannot hash" in r.detail


# ---------------------------------------------------------------- 4. annotation plausibility

def shift_box(i, files, dx=12):
    if i != 2:
        return
    a = biggest_visible(files)
    x0, y0, x1, y1 = a["bbox"]
    dx = dx if x1 + dx <= W else -dx
    a["bbox"] = [x0 + dx, y0, x1 + dx, y1]


def test_a_shifted_box_is_caught_even_when_the_hashes_are_consistent(tmp_path):
    node = HonestNode(tmp_path, n=6, mutate=shift_box)
    ctx, rv = node.ctx(), node.reveal(2)
    res = results(rv, 2, ctx)
    assert not res["annotation_plausibility"].passed and "leaves the projected box" in res["annotation_plausibility"].detail
    assert not res["segmentation_consistency"].passed and "seg.png's mask extents" in res["segmentation_consistency"].detail
    assert res["merkle_proof"].passed and res["scene_match"].passed and res["hash_consistency"].passed   # the node committed its lie
    assert failed(node.reveal(1), 1, ctx) == []   # other frames are untouched


def test_an_enlarged_box_is_caught(tmp_path):
    def grow(i, files):
        if i == 2:
            a = biggest_visible(files)
            a["bbox"] = [max(0, a["bbox"][0] - 8), a["bbox"][1], a["bbox"][2], a["bbox"][3]]
    node = HonestNode(tmp_path, n=6, mutate=grow)
    r = run_one(C.check_annotation_plausibility, node.reveal(2), 2, node.ctx())
    assert not r.passed and "leaves the projected box" in r.detail


def test_a_tiny_box_inside_a_big_visible_object_fails_the_iou_term_even_with_a_tiny_claimed_visibility(tmp_path):
    def shrink(i, files):
        if i == 2:
            a = biggest_visible(files)
            cx, cy = (a["bbox"][0] + a["bbox"][2]) // 2, (a["bbox"][1] + a["bbox"][3]) // 2
            a["bbox"] = [cx, cy, cx + 3, cy + 3]
            a["visibility"], a["mask_pixels"] = 0.0001, 3   # claiming to be barely visible must not relax the threshold: it is derived from seg.png
    node = HonestNode(tmp_path, n=6, mutate=shrink)
    r = run_one(C.check_annotation_plausibility, node.reveal(2), 2, node.ctx())
    assert not r.passed and "IoU" in r.detail and "below the required" in r.detail


def test_wrong_class_missing_extra_and_duplicate_objects_are_caught(tmp_path):
    def wrong_class(i, f):
        if i == 2:
            f.annotations["objects"][0]["class"] = "forklift" if f.annotations["objects"][0]["class"] != "forklift" else "rack"

    def missing(i, f):
        if i == 2:
            f.annotations["objects"].pop()

    def extra(i, f):
        if i == 2:
            f.annotations["objects"].append({**f.annotations["objects"][0], "instance_id": 999})

    def duplicate(i, f):
        if i == 2:
            f.annotations["objects"].append(copy.deepcopy(f.annotations["objects"][0]))

    for mutate, needle in ((wrong_class, "annotated as"), (missing, "missing"), (extra, "extra"), (duplicate, "appears twice")):
        node = HonestNode(tmp_path / mutate.__name__, n=4, mutate=mutate)
        r = run_one(C.check_annotation_plausibility, node.reveal(2), 2, node.ctx())
        assert not r.passed and needle in r.detail, (mutate.__name__, r.detail)


def test_a_visible_box_for_an_object_that_cannot_be_on_screen_is_caught(tmp_path):
    chosen = {}

    def fabricate(i, f):
        if i == 2:
            cam = Camera.from_dict(f.scene["camera"])
            for o in f.scene["objects"]:
                if project_box(cam, o).status != OK:
                    chosen["id"] = o["id"]
                    next(a for a in f.annotations["objects"] if a["instance_id"] == o["id"]).update(bbox=[10, 10, 40, 40], mask_pixels=900, visibility=0.5)
                    return

    node = HonestNode(tmp_path, n=6, seed=7, mutate=fabricate)
    if not chosen:
        pytest.skip("no off-screen object in this frame to fabricate a box for")
    r = run_one(C.check_annotation_plausibility, node.reveal(2), 2, node.ctx())
    assert not r.passed and f"object {chosen['id']}" in r.detail and "projection is" in r.detail


def test_thresholds_are_per_class(honest):
    strict_boxes = Thresholds(per_class={"box": ClassThresholds(pixel_tolerance=0.0, min_iou=0.3)}, default=ClassThresholds(2.0, 0.3), visibility_tolerance=1e-3)
    bad = [i for i in range(honest.n) if "(box)" in run_one(C.check_annotation_plausibility, honest.reveal(i), i, honest.ctx(strict_boxes)).detail]
    assert bad, "a zero-pixel tolerance for boxes must reject at least one honest (sub-pixel overshoot) box"
    # the same strictness applied to a class that has no such objects in the frame changes nothing
    lax = Thresholds(per_class={"forklift": ClassThresholds(0.0, 1.0)}, default=ClassThresholds(2.0, 0.3), visibility_tolerance=1e-3)
    assert all(run_one(C.check_annotation_plausibility, honest.reveal(i), i, honest.ctx(lax)).passed or "(forklift)" in
               run_one(C.check_annotation_plausibility, honest.reveal(i), i, honest.ctx(lax)).detail for i in range(honest.n))


def test_the_iou_requirement_is_met_by_honest_data_at_any_slack_up_to_one(honest):
    """min_iou <= 1 is the slack factor: honest data satisfies the requirement by construction (see Thresholds.required_iou)."""
    full = Thresholds(per_class={}, default=ClassThresholds(pixel_tolerance=2.0, min_iou=1.0), visibility_tolerance=1e-3)
    for i in range(honest.n):
        assert run_one(C.check_annotation_plausibility, honest.reveal(i), i, honest.ctx(full)).passed, i


def test_the_default_thresholds_are_the_approved_calibrated_values(honest):
    th = C.DEFAULT_THRESHOLDS
    assert th.calibrated is True
    for cls in ("rack", "pallet", "box", "forklift"):
        assert th.for_class(cls) == ClassThresholds(pixel_tolerance=1.0, min_iou=0.30)
    assert th.default == ClassThresholds(pixel_tolerance=1.0, min_iou=0.30)
    assert th.seg_rerender == SegRerenderThresholds(min_global_agreement=0.999, min_object_iou=0.90, min_object_pixels=50)
    detail = run_one(C.check_annotation_plausibility, honest.reveal(1), 1, honest.ctx()).detail
    assert "PLACEHOLDER" not in detail and "UNCALIBRATED" not in detail


def test_uncalibrated_thresholds_are_labelled_in_the_details(honest):
    provisional = Thresholds(per_class={}, default=ClassThresholds(2.0, 0.3), visibility_tolerance=1e-3,
                             seg_rerender=SegRerenderThresholds(0.99, 0.5, 50))      # calibrated defaults to False
    assert provisional.calibrated is False
    assert "UNCALIBRATED" in run_one(C.check_annotation_plausibility, honest.reveal(1), 1, honest.ctx(provisional)).detail
    assert "UNCALIBRATED" in run_one(C.check_seg_rerender, honest.reveal(1), 1, honest.ctx(provisional)).detail


def test_the_calibrated_tolerance_is_tighter_than_the_old_placeholder_and_still_passes_honest_boxes(honest):
    # honest overshoot is at most half a pixel (pixel-centre sampling), so 1.0 px has 2x headroom
    worst = 0.0
    for i in range(honest.n):
        r = run_one(C.check_annotation_plausibility, honest.reveal(i), i, honest.ctx())
        assert r.passed
        worst = max(worst, r.metrics.get("max_overshoot_px") or 0.0)
    assert worst <= 0.5 + 1e-6
    # a box 1.5 px outside the projection (inside the old 2.0 px placeholder tolerance) is now rejected
    node = HonestNode(honest.job.dir.parent / "grown", n=4, mutate=lambda i, f: i == 2 and biggest_visible(f).update(
        bbox=[max(0, biggest_visible(f)["bbox"][0] - 2), *biggest_visible(f)["bbox"][1:]]))
    assert not run_one(C.check_annotation_plausibility, node.reveal(2), 2, node.ctx()).passed


def test_calibration_metrics_are_reported(honest):
    m = run_one(C.check_annotation_plausibility, honest.reveal(1), 1, honest.ctx()).metrics
    assert m["boxes_checked"] > 0 and 0 <= m["max_overshoot_px"] < 1.0 and m["min_iou_margin"] >= 0
    assert {"id", "class", "overshoot_px", "iou", "required_iou", "visibility", "visible_fraction"} <= set(m["objects"][0])


# ---------------------------------------------------------------- 5. image sanity

def test_image_sanity_rejects_wrong_resolution_garbage_wrong_format_and_flat_images(honest):
    def with_rgb(data: bytes):
        raw = honest.raw(1)
        raw["rgb_png"] = b64(data)
        return run_one(C.check_image_sanity, Reveal.parse(raw), 1, honest.ctx())

    r = with_rgb(png_bytes(np.random.RandomState(0).randint(0, 255, (100, 100, 3)).astype(np.uint8), "RGB"))
    assert not r.passed and "100x100" in r.detail and f"{W}x{H}" in r.detail
    assert "not a valid PNG" in with_rgb(b"this is not an image").detail
    jpeg = io.BytesIO()
    Image.fromarray(np.random.RandomState(1).randint(0, 255, (H, W, 3)).astype(np.uint8), "RGB").save(jpeg, format="JPEG")
    assert "JPEG, not PNG" in with_rgb(jpeg.getvalue()).detail
    assert "flat colour" in with_rgb(png_bytes(np.zeros((H, W, 3), np.uint8), "RGB")).detail
    assert "flat colour" in with_rgb(png_bytes(np.full((H, W, 3), 200, np.uint8), "RGB")).detail
    assert with_rgb(honest.reveal(1).rgb_png).passed


def test_image_sanity_checks_the_seg_png_too(honest):
    raw = honest.raw(1)
    raw["seg_png"] = b64(png_bytes(np.zeros((10, 10), np.uint16), "I;16"))
    assert "seg.png is 10x10" in run_one(C.check_image_sanity, Reveal.parse(raw), 1, honest.ctx()).detail
    raw["seg_png"] = b64(b"junk")
    assert "seg.png is not a valid PNG" in run_one(C.check_image_sanity, Reveal.parse(raw), 1, honest.ctx()).detail


def test_a_duplicate_image_within_the_sample_is_caught(tmp_path):
    def reuse(i, f):
        if i == 5:
            f.rgb = png_bytes(np.random.RandomState(42 * 1000 + 2).randint(0, 256, (H, W, 3)).astype(np.uint8), "RGB")   # frame 2's image

    node = HonestNode(tmp_path, n=8, mutate=reuse)
    seen = SeenImages()
    ctx = node.ctx(seen=seen)
    assert run_one(C.check_image_sanity, node.reveal(2), 2, ctx).passed          # the original is fine
    dup = run_one(C.check_image_sanity, node.reveal(5), 5, ctx)
    assert not dup.passed and f"{ADDRESS}:2" in dup.detail and "duplicated image" in dup.detail
    assert run_one(C.check_image_sanity, node.reveal(2), 2, ctx).passed          # re-verifying the SAME frame is not a duplicate


def test_an_image_seen_earlier_in_the_run_is_a_duplicate(honest):
    seen = SeenImages()
    seen.check_and_add(__import__("hashlib").sha256(honest.reveal(3).rgb_png).hexdigest(), "someOtherRequest:0")
    r = run_one(C.check_image_sanity, honest.reveal(3), 3, honest.ctx(seen=seen))
    assert not r.passed and "someOtherRequest:0" in r.detail


# ---------------------------------------------------------------- 6. segmentation consistency

def test_a_wrong_segmentation_id_is_caught_when_committed(tmp_path):
    def stray(i, f):
        if i == 2:
            seg = np.array(Image.open(io.BytesIO(f.seg))).astype(np.uint16)
            seg[5:9, 5:9] = len(f.scene["objects"]) + 40       # an id that is not a scene object
            f.seg = png_bytes(seg, "I;16")

    node = HonestNode(tmp_path, n=6, mutate=stray)
    res = results(node.reveal(2), 2, node.ctx())
    assert not res["segmentation_consistency"].passed and "not scene objects" in res["segmentation_consistency"].detail
    assert res["hash_consistency"].passed and res["merkle_proof"].passed      # committed, so only this check sees it


def test_annotations_that_disagree_with_seg_png_are_caught(tmp_path):
    def edit(field, value):
        def mutate(i, f):
            if i == 2:
                a = biggest_visible(f)
                a[field] = value(a) if callable(value) else value
        return mutate

    cases = {
        "bbox": edit("bbox", lambda a: [a["bbox"][0], a["bbox"][1], a["bbox"][2] - 1, a["bbox"][3]]),
        "mask_pixels": edit("mask_pixels", lambda a: a["mask_pixels"] + 5),
        "visibility": edit("visibility", lambda a: max(0.0, a["visibility"] - 0.2)),
    }
    for field, mutate in cases.items():
        node = HonestNode(tmp_path / field, n=4, mutate=mutate)
        r = run_one(C.check_segmentation_consistency, node.reveal(2), 2, node.ctx())
        assert not r.passed and field in r.detail, (field, r.detail)


def test_claiming_hidden_when_visible_and_visible_when_hidden_are_both_caught(tmp_path):
    def hide(i, f):          # a visible object annotated as fully hidden
        if i == 2:
            biggest_visible(f).update(bbox=None, mask_pixels=0, visibility=0.0)

    node = HonestNode(tmp_path / "hide", n=4, mutate=hide)
    r = run_one(C.check_segmentation_consistency, node.reveal(2), 2, node.ctx())
    assert not r.passed and "seg.png's mask extents" in r.detail

    conjured = {}

    def conjure(i, f):       # a hidden object annotated with a visible box
        hidden = next((a for a in f.annotations["objects"] if a["bbox"] is None), None)
        if hidden is not None and not conjured:
            conjured["frame"], conjured["id"] = i, hidden["instance_id"]
            hidden.update(bbox=[1, 1, 20, 20], mask_pixels=50, visibility=0.3)

    node = HonestNode(tmp_path / "conjure", n=8, occlusion=3, mutate=conjure)
    assert conjured, "the fixture must contain a fully hidden object to conjure a box for"
    r = run_one(C.check_segmentation_consistency, node.reveal(conjured["frame"]), conjured["frame"], node.ctx())
    assert not r.passed and f"object {conjured['id']}" in r.detail


def test_an_undecodable_seg_png_fails_gracefully_everywhere(honest):
    raw = honest.raw(2)
    raw["seg_png"] = b64(b"garbage")
    res = results(Reveal.parse(raw), 2, honest.ctx())
    assert [r for r in res] == list(CHECK_NAMES) and not res["segmentation_consistency"].passed
    assert not res["annotation_plausibility"].passed and not res["image_sanity"].passed and not res["hash_consistency"].passed
    assert not any("crashed" in r.detail for r in res.values())


def test_a_fully_hidden_object_with_no_box_is_consistent(honest):
    # honest frames contain hidden objects; they pass check 6 with bbox null / 0 pixels / visibility 0
    ctx = honest.ctx()
    hidden_seen = 0
    for i in range(honest.n):
        rv = honest.reveal(i)
        hidden_seen += sum(1 for a in rv.annotations["objects"] if a["bbox"] is None)
        assert run_one(C.check_segmentation_consistency, rv, i, ctx).passed
    assert hidden_seen > 0, "the fixture should include fully hidden objects"


# ---------------------------------------------------------------- 7. seg re-render

from porf.node import cheats                                              # noqa: E402
from porf.seg import decode_seg                                           # noqa: E402
from porf.verifier.checks import SegRerenderThresholds                    # noqa: E402
from porf.verifier.segcompare import compare_seg                          # noqa: E402


def seg_of(raw) -> np.ndarray:
    return decode_seg(base64.b64decode(raw["seg_png"]))


def with_seg(raw, seg: np.ndarray):
    raw["seg_png"] = b64(png_bytes(seg.astype(np.uint16), "I;16"))
    return raw


def test_an_honest_seg_matches_its_rerender_exactly(honest):
    for i in range(honest.n):
        r = run_one(C.check_seg_rerender, honest.reveal(i), i, honest.ctx())
        assert r.passed and r.metrics["global_agreement"] == 1.0 and r.metrics["mismatched_pixels"] == 0
        assert r.metrics["worst_object"]["iou"] == 1.0 and r.metrics["objects_compared"] > 0
        assert "PLACEHOLDER" not in r.detail and "UNCALIBRATED" not in r.detail


def test_check_7_fails_loudly_without_a_reference_and_on_size_or_decode_problems(honest):
    ctx = honest.ctx()
    ctx.reference_seg = None
    assert "no re-rendered reference" in run_one(C.check_seg_rerender, honest.reveal(1), 1, ctx).detail
    ctx.reference_seg = {2: honest.reference[2]}                       # a reference for a different frame only
    assert "no re-rendered reference" in run_one(C.check_seg_rerender, honest.reveal(1), 1, ctx).detail
    ctx.reference_seg = {1: np.zeros((10, 10), np.int64)}
    assert "the re-render" in run_one(C.check_seg_rerender, honest.reveal(1), 1, ctx).detail
    raw = honest.raw(1)
    raw["seg_png"] = b64(b"garbage")
    assert "cannot compare" in run_one(C.check_seg_rerender, Reveal.parse(raw), 1, honest.ctx()).detail


def test_a_few_flipped_pixels_are_tolerated_but_real_differences_are_not(honest):
    i = 1
    seg = seg_of(honest.raw(i))
    ref = honest.reference[i]
    # 20 pixels flipped on object boundaries: below every placeholder floor
    flipped = seg.copy()
    ys, xs = np.nonzero((seg != 0) & (np.roll(seg, 1, axis=1) == 0))
    flipped[ys[:20], xs[:20]] = 0
    assert (flipped != ref).sum() == 20
    assert run_one(C.check_seg_rerender, Reveal.parse(with_seg(honest.raw(i), flipped)), i, honest.ctx()).passed

    # an object shifted by 12 pixels
    big = max(np.unique(seg)[1:], key=lambda k: (seg == k).sum())
    shifted_seg = seg.copy()
    shifted_seg[seg == big] = 0
    shifted_seg[np.roll(seg == big, 12, axis=1)] = big
    r = run_one(C.check_seg_rerender, Reveal.parse(with_seg(honest.raw(i), shifted_seg)), i, honest.ctx())
    assert not r.passed and f"object {big}" in r.detail and "mask IoU" in r.detail

    # an object erased entirely
    erased = seg.copy()
    erased[seg == big] = 0
    r = run_one(C.check_seg_rerender, Reveal.parse(with_seg(honest.raw(i), erased)), i, honest.ctx())
    assert not r.passed and f"object {big}" in r.detail and "mask IoU 0.000" in r.detail

    # scattered noise over 5% of the image
    noisy = seg.copy()
    rng = np.random.RandomState(0)
    noisy[rng.rand(*seg.shape) < 0.05] = 3
    r = run_one(C.check_seg_rerender, Reveal.parse(with_seg(honest.raw(i), noisy)), i, honest.ctx())
    assert not r.passed and "global pixel agreement" in r.detail


def test_objects_below_the_pixel_floor_are_not_judged(honest):
    th = honest.ctx().thresholds.seg_rerender
    for i in range(honest.n):          # find a frame that has a tiny object (a sliver of something mostly hidden)
        ref = honest.reference[i]
        ids, counts = np.unique(ref[ref != 0], return_counts=True)
        small = [int(k) for k, c in zip(ids, counts) if c < th.min_object_pixels]
        large = [int(k) for k, c in zip(ids, counts) if c >= th.min_object_pixels]
        if small and large:
            break
    else:
        pytest.fail("the honest fixture should contain a frame with a tiny object")
    seg = seg_of(honest.raw(i))
    gone = seg.copy()
    gone[np.isin(seg, small)] = 0                                      # a tiny object vanishes: not judged, so still a pass
    assert run_one(C.check_seg_rerender, Reveal.parse(with_seg(honest.raw(i), gone)), i, honest.ctx()).passed
    gone = seg.copy()
    gone[seg == large[0]] = 0                                          # a real object vanishes: judged, so a fail
    assert not run_one(C.check_seg_rerender, Reveal.parse(with_seg(honest.raw(i), gone)), i, honest.ctx()).passed


def test_thresholds_for_check_7_are_configurable(honest):
    seg = seg_of(honest.raw(1))
    seg[:5, :] = 7                                                     # 5 rows of 240: ~2% of the image differs
    strict = Thresholds(per_class={}, default=C.ClassThresholds(2.0, 0.3), visibility_tolerance=1e-3,
                        seg_rerender=SegRerenderThresholds(0.999, 0.5, 50))
    loose = Thresholds(per_class={}, default=C.ClassThresholds(2.0, 0.3), visibility_tolerance=1e-3,
                       seg_rerender=SegRerenderThresholds(0.90, 0.0, 10**9))
    rv = Reveal.parse(with_seg(honest.raw(1), seg))
    assert not run_one(C.check_seg_rerender, rv, 1, honest.ctx(strict)).passed
    assert run_one(C.check_seg_rerender, rv, 1, honest.ctx(loose)).passed


def test_compare_seg_measures_what_it_says():
    a = np.zeros((10, 10), np.int64)
    b = a.copy()
    a[:5, :5] = 1                      # 25 px
    b[:5, :3] = 1                      # 15 px, all inside a's mask
    c = compare_seg(a, b)
    assert c.mismatched_pixels == 10 and c.global_agreement == 0.9
    o = c.objects[1]
    assert (o.revealed_pixels, o.reference_pixels, o.intersection) == (25, 15, 15) and o.iou == pytest.approx(15 / 25)
    only_a = np.zeros((10, 10), np.int64)
    only_a[0, 0] = 2
    assert compare_seg(only_a, np.zeros((10, 10), np.int64)).objects[2].iou == 0.0
    assert compare_seg(a, a).global_agreement == 1.0
    with pytest.raises(ValueError):
        compare_seg(a, np.zeros((3, 3), np.int64))


# ---- the no_occlusion cheat: consistent through checks 1-6, caught by check 7

def test_the_no_occlusion_cheat_passes_checks_1_to_6_and_fails_check_7(tmp_path):
    node = HonestNode(tmp_path, n=8, seed=42, occlusion=3)
    cheats.after_render("no_occlusion", node.job.dir, node.n, fraction=1.0)          # the real cheat code, rewriting seg.png and annotations.json
    node.recommit()                                                    # ... and committing it consistently
    ctx = node.ctx()
    caught = 0
    for i in range(node.n):
        res = results(node.reveal(i), i, ctx)
        assert all(res[name].passed for name in CHECK_NAMES[:6]), (i, [(n, r.detail) for n, r in res.items() if not r.passed])
        if not res["seg_rerender"].passed:
            caught += 1
            assert "global pixel agreement" in res["seg_rerender"].detail or "mask IoU" in res["seg_rerender"].detail
    assert caught >= 6, f"check 7 should catch the cheat in nearly every occluded frame, caught {caught} of {node.n}"


def test_the_cheat_really_changes_the_segmentation_and_keeps_boxes_consistent(tmp_path):
    node = HonestNode(tmp_path, n=4, seed=42, occlusion=3)
    before = {i: seg_of(node.raw(i)).copy() for i in range(4)}
    cheats.after_render("no_occlusion", node.job.dir, 4, fraction=1.0)
    node.recommit()
    for i in range(4):
        after = seg_of(node.raw(i))
        assert (after != before[i]).any()
        assert run_one(C.check_segmentation_consistency, node.reveal(i), i, node.ctx()).passed   # boxes derived from the forged seg
        # every object's full silhouette is painted, so no object is fully hidden unless it is off screen
        ann = node.raw(i)["annotations"]["objects"]
        assert sum(1 for a in ann if a["bbox"] is None) <= sum(1 for a in node.reveal(i).annotations["objects"] if a["bbox"] is None)


def test_cheat_announcements_are_loud(caplog):
    with caplog.at_level("WARNING"):
        cheats.announce("no_occlusion")
    assert "COMMIT FRAUDULENT DATA" in caplog.text and cheats.IMPLEMENTED == cheats.CHEAT_MODES
    assert cheats.after_render(None, Path("/nonexistent"), 3) is None   # no --cheat: nothing is touched


from pathlib import Path   # noqa: E402
