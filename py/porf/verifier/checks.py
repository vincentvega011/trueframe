"""The six per-frame checks (docs/PROTOCOL.md section 7). Each returns a CheckResult {name, passed, detail}.

Every check runs on every sampled frame, independently (no short-circuit), so a report shows all that failed. Checks
never raise on a malformed reveal: they fail with a reason. The inputs are only (a) the reveal, (b) the on-chain
Request and Submission, and (c) things the verifier derives itself (the regenerated scene, statistics from the committed
seg.png). The node's own numbers (annotation visibility, box coordinates) are checked against those, never trusted.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from ..canon import hash_json, sha256
from ..merkle import proof_from_json, verify_frame
from ..records import FrameHashes, hash_frame
from ..scene.camera import Camera
from ..scene.generator import generate_scene
from ..scene.projection import OK, project_box, silhouette_area, visibility_fraction
from ..seg import SegError, SegObject, analyze, decode_seg
from .reveal import Reveal

MERKLE_PROOF = "merkle_proof"
SCENE_MATCH = "scene_match"
HASH_CONSISTENCY = "hash_consistency"
ANNOTATION_PLAUSIBILITY = "annotation_plausibility"
IMAGE_SANITY = "image_sanity"
SEGMENTATION_CONSISTENCY = "segmentation_consistency"
IOU_EPSILON = 1e-9
CHECK_NAMES = (MERKLE_PROOF, SCENE_MATCH, HASH_CONSISTENCY, ANNOTATION_PLAUSIBILITY, IMAGE_SANITY, SEGMENTATION_CONSISTENCY)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)  # measurements for threshold calibration; not part of the verdict

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "passed": self.passed, "detail": self.detail}
        if self.metrics:
            d["metrics"] = self.metrics
        return d


def _ok(name: str, detail: str, **metrics: Any) -> CheckResult:
    return CheckResult(name, True, detail, metrics)


def _bad(name: str, detail: str, **metrics: Any) -> CheckResult:
    return CheckResult(name, False, detail, metrics)


# ---------------------------------------------------------------- thresholds (annotation plausibility)

@dataclass(frozen=True)
class ClassThresholds:
    pixel_tolerance: float  # how far (px) the mask box may stick out of the projected full-object box
    min_iou: float          # slack factor in (0, 1]: IoU must reach min_iou * (visible pixels / tolerance-grown box area)


@dataclass(frozen=True)
class Thresholds:
    """PLACEHOLDER VALUES. Chosen to be permissive so an honest node is never rejected; they are NOT calibrated and a
    cheater with small box errors could pass. Day 4c measures honest runs per class and replaces them
    (docs/PROTOCOL.md section 7). Per-class entries exist because racks and forklifts are not solid objects."""

    per_class: dict[str, ClassThresholds]
    default: ClassThresholds
    visibility_tolerance: float  # claimed visibility must match the one derived from seg.png within this (quantization is 1e-4)
    calibrated: bool = False     # flipped to True only by the calibration work

    def for_class(self, cls: str) -> ClassThresholds:
        return self.per_class.get(cls, self.default)

    def required_iou(self, cls: str, visible_fraction: float) -> float:
        """IoU(mask box, projected box grown by the pixel tolerance) must be at least min_iou * visible_fraction, where
        visible_fraction = mask pixels (counted from seg.png) / area of that grown box, i.e. how much of the box is actually
        visible. Honest data satisfies this by construction for any min_iou <= 1: once the mask box lies inside the grown box,
        IoU = mask_box_area / grown_area >= mask_pixels / grown_area. The grown box matters: the visible part of a mostly
        hidden object can be a one-pixel sliver that sticks out of the exact projected box by under a pixel, and its IoU with
        the exact box is then tiny. There is deliberately no visibility floor either: a barely visible object has a tiny mask
        box, so the requirement must shrink with it."""
        return self.for_class(cls).min_iou * visible_fraction


_PLACEHOLDER = ClassThresholds(pixel_tolerance=2.0, min_iou=0.30)  # PLACEHOLDER
DEFAULT_THRESHOLDS = Thresholds(
    per_class={"rack": _PLACEHOLDER, "pallet": _PLACEHOLDER, "box": _PLACEHOLDER, "forklift": _PLACEHOLDER},  # PLACEHOLDER
    default=_PLACEHOLDER,
    visibility_tolerance=1e-3,
)


# ---------------------------------------------------------------- context

class SeenImages:
    """rgb hashes seen so far in this verifier run, with the frame each came from. Re-verifying the SAME frame is not a
    duplicate; the same image in a different frame or request is."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def check_and_add(self, image_hash: str, label: str) -> str | None:
        """Returns the label of an earlier, different frame with this image, else None (and remembers this one)."""
        prior = self._seen.setdefault(image_hash, label)
        return prior if prior != label else None


@dataclass
class VerifyContext:
    """What the verifier knows from the chain. `request` and `submission` are porf.chain.client views."""

    request_address: str
    request: Any
    submission: Any
    thresholds: Thresholds = DEFAULT_THRESHOLDS
    seen: SeenImages = field(default_factory=SeenImages)

    @property
    def onchain_root(self) -> bytes:
        return bytes.fromhex(self.submission.merkle_root)


@dataclass
class FrameFacts:
    """Things derived once per frame from trusted inputs, shared by the checks."""

    expected_scene: dict[str, Any]
    seg: np.ndarray | None
    seg_error: str | None
    seg_stats: dict[int, SegObject]
    sample_label: str

    @classmethod
    def build(cls, reveal: Reveal, index: int, ctx: VerifyContext) -> "FrameFacts":
        req = ctx.request
        expected = generate_scene(req.seed, index, req.occlusion_level, req.width, req.height)
        seg, err, stats = None, None, {}
        if reveal.seg_png is None:
            err = "seg.png missing"
        else:
            try:
                seg = decode_seg(reveal.seg_png)
                stats = analyze(seg)
            except SegError as e:
                err = str(e)
        return cls(expected, seg, err, stats, f"{ctx.request_address}:{index}")


# ---------------------------------------------------------------- helpers

def first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    """Path and values of the first difference between two JSON-like structures, or None if equal."""
    if type(a) is not type(b):
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: present in only one of them"
            d = first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _leaf_fields(reveal: Reveal) -> FrameHashes | None:
    try:
        return FrameHashes.from_hex(reveal.leaf or {})
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 1. Merkle proof

def check_merkle_proof(reveal: Reveal, index: int, ctx: VerifyContext, facts: FrameFacts) -> CheckResult:
    if reveal.frame_index != index:
        return _bad(MERKLE_PROOF, f"the reveal is for frame {reveal.frame_index!r} but frame {index} was requested")
    if reveal.leaf is None or reveal.leaf.get("frame_index") != index:
        got = None if reveal.leaf is None else reveal.leaf.get("frame_index")
        return _bad(MERKLE_PROOF, f"the leaf's frame_index is {got!r}, expected {index}")
    fields = _leaf_fields(reveal)
    if fields is None:
        return _bad(MERKLE_PROOF, "leaf hashes are missing or not 32-byte hex")
    try:
        proof = proof_from_json(reveal.proof)
    except ValueError as e:
        return _bad(MERKLE_PROOF, f"malformed proof: {e}")
    if reveal.leaf.get("leaf_hash") != fields.leaf(index).hex():
        return _bad(MERKLE_PROOF, "leaf_hash does not equal the hash of the leaf fields")
    if reveal.merkle_root is not None and reveal.merkle_root != ctx.submission.merkle_root:
        return _bad(MERKLE_PROOF, f"the reveal states root {reveal.merkle_root[:16]}..., the chain has {ctx.submission.merkle_root[:16]}...")
    # always the ON-CHAIN root and frame count, never the ones the node reports
    if not verify_frame(index, fields.scene_hash, fields.annotation_hash, fields.image_hash, fields.seg_hash, proof,
                        ctx.onchain_root, ctx.submission.frame_count):
        return _bad(MERKLE_PROOF, "the proof does not verify against the on-chain root for this frame index")
    return _ok(MERKLE_PROOF, f"proof of {len(proof)} steps verifies against the on-chain root at index {index}")


# ---------------------------------------------------------------- 2. scene match

def check_scene_match(reveal: Reveal, index: int, ctx: VerifyContext, facts: FrameFacts) -> CheckResult:
    expected_hash = hash_json(facts.expected_scene).hex()
    committed = (reveal.leaf or {}).get("scene_hash")
    if committed != expected_hash:
        diff = first_difference(reveal.scene, facts.expected_scene) if reveal.scene is not None else "scene not revealed"
        return _bad(SCENE_MATCH, f"committed scene_hash {str(committed)[:16]}... differs from the scene regenerated from the on-chain "
                    f"seed/parameters ({expected_hash[:16]}...); first difference in the revealed scene: {diff}")
    if reveal.scene is None:
        return _bad(SCENE_MATCH, "scene.json was not revealed")
    if hash_json(reveal.scene).hex() != expected_hash:
        return _bad(SCENE_MATCH, f"the revealed scene differs from the regenerated one: {first_difference(reveal.scene, facts.expected_scene)}")
    return _ok(SCENE_MATCH, f"scene_hash {expected_hash[:16]}... equals the scene regenerated from seed {ctx.request.seed}, frame {index}")


# ---------------------------------------------------------------- 3. hash consistency

def check_hash_consistency(reveal: Reveal, index: int, ctx: VerifyContext, facts: FrameFacts) -> CheckResult:
    fields = _leaf_fields(reveal)
    if fields is None:
        return _bad(HASH_CONSISTENCY, "leaf hashes are missing or not 32-byte hex")
    if reveal.scene is None or reveal.annotations is None or reveal.rgb_png is None or reveal.seg_png is None:
        return _bad(HASH_CONSISTENCY, "cannot hash: " + "; ".join(reveal.errors or ["a file is missing"]))
    actual = hash_frame(reveal.scene, reveal.annotations, reveal.rgb_png, reveal.seg_png)
    bad = [f for f, a, c in (("scene.json", actual.scene_hash, fields.scene_hash), ("annotations.json", actual.annotation_hash, fields.annotation_hash),
                             ("rgb.png", actual.image_hash, fields.image_hash), ("seg.png", actual.seg_hash, fields.seg_hash)) if a != c]
    if bad:
        return _bad(HASH_CONSISTENCY, "the revealed " + ", ".join(bad) + " does not hash to the committed leaf field")
    return _ok(HASH_CONSISTENCY, "scene.json, annotations.json, rgb.png and seg.png all hash to the committed leaf fields")


# ---------------------------------------------------------------- 4. annotation plausibility

def check_annotation_plausibility(reveal: Reveal, index: int, ctx: VerifyContext, facts: FrameFacts) -> CheckResult:
    name = ANNOTATION_PLAUSIBILITY
    if reveal.annotations is None:
        return _bad(name, "annotations.json was not revealed")
    objects = reveal.annotations.get("objects")
    if not isinstance(objects, list) or not all(isinstance(o, dict) for o in objects):
        return _bad(name, "annotations.json has no list of objects")
    scene = facts.expected_scene
    by_id: dict[Any, dict] = {}
    for o in objects:
        if o.get("instance_id") in by_id:
            return _bad(name, f"instance_id {o.get('instance_id')!r} appears twice")
        by_id[o.get("instance_id")] = o
    want = [o["id"] for o in scene["objects"]]
    if sorted(by_id, key=str) != sorted(want, key=str):
        extra, missing = sorted(set(by_id) - set(want), key=str), sorted(set(want) - set(by_id), key=str)
        return _bad(name, f"annotated instance ids differ from the scene's: extra {extra}, missing {missing}")
    if facts.seg is None:
        return _bad(name, f"cannot derive visibility: {facts.seg_error}")

    cam = Camera.from_dict(scene["camera"])
    width, height = cam.width, cam.height
    th = ctx.thresholds
    checked, worst_overshoot, worst_iou_margin = 0, 0.0, float("inf")
    per_object: list[dict[str, Any]] = []
    for obj in scene["objects"]:
        a = by_id[obj["id"]]
        cls = obj["class"]
        if a.get("class") != cls:
            return _bad(name, f"object {obj['id']} is annotated as {a.get('class')!r} but is a {cls!r} in the scene")
        bbox = a.get("bbox")
        if bbox is None:
            continue  # a fully hidden object has no box to compare; consistency with seg.png is check 6
        if not (isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bbox)):
            return _bad(name, f"object {obj['id']} ({cls}): malformed bbox {bbox!r}")
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            return _bad(name, f"object {obj['id']} ({cls}): bbox {bbox} is empty or outside the {width}x{height} image")
        proj = project_box(cam, obj)
        if proj.status != OK:
            return _bad(name, f"object {obj['id']} ({cls}) claims a visible box {bbox} but its projection is {proj.status}")
        px0, py0, px1, py1 = proj.box
        tol = th.for_class(cls).pixel_tolerance
        overshoot = max(px0 - x0, py0 - y0, x1 - px1, y1 - py1)
        found = facts.seg_stats.get(obj["id"])
        pixels = found.pixels if found else 0   # counted from the committed seg.png, never taken from the annotation
        visibility = visibility_fraction(pixels, silhouette_area(cam, obj))
        grown = (max(0.0, px0 - tol), max(0.0, py0 - tol), min(float(width), px1 + tol), min(float(height), py1 + tol))
        grown_area = (grown[2] - grown[0]) * (grown[3] - grown[1])
        value = iou((x0, y0, x1, y1), grown)
        visible_fraction = pixels / grown_area if grown_area > 0 else 0.0
        needed = th.required_iou(cls, visible_fraction)
        worst_overshoot = max(worst_overshoot, overshoot)
        worst_iou_margin = min(worst_iou_margin, value - needed)
        per_object.append({"id": obj["id"], "class": cls, "overshoot_px": round(overshoot, 3), "iou": round(value, 4),
                           "required_iou": round(needed, 4), "visibility": round(visibility, 4), "visible_fraction": round(visible_fraction, 4)})
        if overshoot > tol:
            return _bad(name, f"object {obj['id']} ({cls}): mask box {bbox} leaves the projected box "
                        f"[{px0:.1f}, {py0:.1f}, {px1:.1f}, {py1:.1f}] by {overshoot:.1f}px (tolerance {tol}px)", objects=per_object)
        if value < needed - IOU_EPSILON:   # exact-arithmetic equality is the honest boundary at slack 1.0; allow float rounding
            return _bad(name, f"object {obj['id']} ({cls}): IoU with the tolerance-grown projected box is {value:.3f}, below the required "
                        f"{needed:.3f} (min {th.for_class(cls).min_iou} x visible fraction {visible_fraction:.3f})", objects=per_object)
        checked += 1
    detail = f"{checked} visible object boxes lie inside their projected boxes and meet the IoU threshold" if checked else "no visible object boxes to compare"
    return _ok(name, detail + (" (PLACEHOLDER thresholds)" if not th.calibrated else ""),
               boxes_checked=checked, max_overshoot_px=round(worst_overshoot, 3) if checked else None,
               min_iou_margin=round(worst_iou_margin, 4) if checked else None, objects=per_object)


# ---------------------------------------------------------------- 5. image sanity

def _open_png(data: bytes | None, label: str) -> tuple[Image.Image | None, str | None]:
    if data is None:
        return None, f"{label} missing"
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        return None, f"{label} is not a valid PNG ({type(e).__name__})"
    if img.format != "PNG":
        return None, f"{label} is {img.format}, not PNG"
    return img, None


def check_image_sanity(reveal: Reveal, index: int, ctx: VerifyContext, facts: FrameFacts) -> CheckResult:
    name = IMAGE_SANITY
    want = (ctx.request.width, ctx.request.height)
    rgb, err = _open_png(reveal.rgb_png, "rgb.png")
    if rgb is None:
        return _bad(name, err or "rgb.png unreadable")
    if rgb.size != want:
        return _bad(name, f"rgb.png is {rgb.size[0]}x{rgb.size[1]}, the request asked for {want[0]}x{want[1]}")
    if rgb.mode not in ("RGB", "RGBA"):
        return _bad(name, f"rgb.png has mode {rgb.mode}, expected RGB")
    seg, err = _open_png(reveal.seg_png, "seg.png")
    if seg is None:
        return _bad(name, err or "seg.png unreadable")
    if seg.size != want:
        return _bad(name, f"seg.png is {seg.size[0]}x{seg.size[1]}, the request asked for {want[0]}x{want[1]}")
    pixels = np.asarray(rgb.convert("RGB"))
    if int(pixels.max()) == int(pixels.min()):
        return _bad(name, f"rgb.png is a single flat colour ({int(pixels.max())}); nothing was rendered")
    digest = sha256(reveal.rgb_png).hex()
    earlier = ctx.seen.check_and_add(digest, facts.sample_label)
    if earlier:
        return _bad(name, f"rgb.png is byte-identical to {earlier} (a duplicated image)")
    return _ok(name, f"valid {want[0]}x{want[1]} PNGs, not flat, not a duplicate of any image seen in this run")


# ---------------------------------------------------------------- 6. segmentation consistency

def check_segmentation_consistency(reveal: Reveal, index: int, ctx: VerifyContext, facts: FrameFacts) -> CheckResult:
    name = SEGMENTATION_CONSISTENCY
    if facts.seg is None:
        return _bad(name, str(facts.seg_error))
    scene_ids = {o["id"] for o in facts.expected_scene["objects"]}
    stray = sorted(set(facts.seg_stats) - scene_ids)
    if stray:
        return _bad(name, f"seg.png contains ids that are not scene objects: {stray[:10]}")
    if reveal.annotations is None or not isinstance(reveal.annotations.get("objects"), list):
        return _bad(name, "annotations.json was not revealed")
    # annotations must be exactly what seg.png says (both are committed): box, pixel count, visibility
    cam = Camera.from_dict(facts.expected_scene["camera"])
    annotated = {o.get("instance_id"): o for o in reveal.annotations["objects"] if isinstance(o, dict)}
    for obj in facts.expected_scene["objects"]:
        a = annotated.get(obj["id"])
        if a is None:
            return _bad(name, f"object {obj['id']} has no annotation")
        found = facts.seg_stats.get(obj["id"])
        derived_bbox = list(found.bbox) if found else None
        derived_px = found.pixels if found else 0
        if a.get("bbox") != derived_bbox:
            return _bad(name, f"object {obj['id']}: annotated bbox {a.get('bbox')} but seg.png's mask extents are {derived_bbox}")
        if a.get("mask_pixels") != derived_px:
            return _bad(name, f"object {obj['id']}: annotated mask_pixels {a.get('mask_pixels')} but seg.png has {derived_px}")
        claimed = a.get("visibility")
        derived_vis = visibility_fraction(derived_px, silhouette_area(cam, obj))
        if not isinstance(claimed, (int, float)) or abs(claimed - derived_vis) > ctx.thresholds.visibility_tolerance:
            return _bad(name, f"object {obj['id']}: annotated visibility {claimed!r} but seg.png gives {derived_vis:.4f}")
    present = len(facts.seg_stats)
    return _ok(name, f"all {present} ids in seg.png are scene objects, and every annotation (box, pixel count, visibility) "
               f"equals what seg.png gives; {len(scene_ids) - present} objects fully hidden", ids_present=present)


ALL_CHECKS = (check_merkle_proof, check_scene_match, check_hash_consistency, check_annotation_plausibility,
              check_image_sanity, check_segmentation_consistency)


def run_checks(reveal: Reveal, index: int, ctx: VerifyContext) -> list[CheckResult]:
    """All six checks, in order, on one revealed frame. Never raises: a check that crashes counts as failed."""
    facts = FrameFacts.build(reveal, index, ctx)
    results = []
    for check in ALL_CHECKS:
        try:
            results.append(check(reveal, index, ctx, facts))
        except Exception as e:  # a bug or a very odd reveal must not turn into a Pass
            results.append(_bad(check.__name__.removeprefix("check_"), f"check crashed: {type(e).__name__}: {e}"))
    return results
