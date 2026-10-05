import json
import math
import os
import subprocess
import sys

import pytest

from porf.canon import canonical_bytes, hash_json, quantize
from porf.scene.camera import Camera, camera_dict
from porf.scene.generator import CLASS_IDS, FLOOR_SIZE, generate_scene
from porf.scene.projection import BEHIND_CAMERA, OK, OUTSIDE_IMAGE, project_box

# Regression vectors: if the generator's output changes in any way, these break.
# DELIBERATELY UPDATED when the camera-pose guard and view-cone placement of loose boxes / forklift were
# added (previous level-0 value: 8b6bfed0102d43c9f6d77085ad7077945e4093b83e737568504716336dcd27c7).
# The level-0 hash moved because loose boxes now number 4-6 (was 2-4) and are placed in the view cone,
# not because level 0's camera changed. Any further change must update these on purpose.
SEED42_FRAME0_L0_HASH = "77f7f25c1e75ee2ea0a5f66ebb2c9936162a841dd3949ca9db56a61a5396ac4c"
SEED42_FRAME0_L3_HASH = "1704f1b7be84042dbb9c53669afeda33938abcd47b81e66e56ea5b738ed153f5"


# ---------------- quantize ----------------

def test_quantize_rules():
    assert quantize(1.23456) == 1.2346
    assert quantize(2.00004) == 2.0
    assert repr(quantize(-0.0)) == "0.0"
    assert repr(quantize(-0.00001)) == "0.0"  # rounds to -0.0, normalized
    assert quantize(3) == 3.0 and isinstance(quantize(3), float)
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            quantize(bad)


# ---------------- determinism ----------------

_SNIPPET = (
    "import sys; from porf.scene.generator import generate_scene; from porf.canon import canonical_bytes;"
    "sys.stdout.write(canonical_bytes(generate_scene(42, 3, 2)).decode())"
)


def _run_subprocess(hashseed: str) -> bytes:
    env = dict(os.environ, PYTHONHASHSEED=hashseed)
    out = subprocess.run([sys.executable, "-c", _SNIPPET], env=env, capture_output=True, check=True)
    return out.stdout


def test_byte_identical_across_processes_and_hash_seeds():
    a, b, c = _run_subprocess("0"), _run_subprocess("1"), _run_subprocess("12345")
    assert a == b == c
    assert a == canonical_bytes(generate_scene(42, 3, 2))


def test_regression_hash_seed42_frame0():
    assert hash_json(generate_scene(42, 0)).hex() == SEED42_FRAME0_L0_HASH
    assert hash_json(generate_scene(42, 0, 3)).hex() == SEED42_FRAME0_L3_HASH


def test_same_inputs_same_output_in_process():
    assert generate_scene(7, 5, 1) == generate_scene(7, 5, 1)


def test_frames_differ_and_seeds_differ():
    hashes = {hash_json(generate_scene(42, f)) for f in range(10)}
    assert len(hashes) == 10
    assert hash_json(generate_scene(42, 0)) != hash_json(generate_scene(43, 0))
    assert hash_json(generate_scene(42, 0, 0)) != hash_json(generate_scene(42, 0, 3))


def test_input_validation():
    with pytest.raises(ValueError):
        generate_scene(-1, 0)
    with pytest.raises(ValueError):
        generate_scene(2**64, 0)
    with pytest.raises(ValueError):
        generate_scene(1, 2**32)
    with pytest.raises(ValueError):
        generate_scene(1, 0, occlusion_level=4)


# ---------------- graph hygiene ----------------

def _walk(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            assert isinstance(k, str), path
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_no_negative_zero_nan_or_extra_decimals(level):
    for frame in range(15):
        graph = generate_scene(42, frame, level)
        for path, v in _walk(graph):
            assert not isinstance(v, tuple), path
            if isinstance(v, float):
                assert math.isfinite(v), path
                assert math.copysign(1.0, v) > 0 or v != 0.0, f"-0.0 at {path}"
                assert not (v == 0.0 and str(v).startswith("-")), path
                assert round(v, 4) == v, f"{path}={v!r}"
        # survives a canonical JSON round trip unchanged
        assert json.loads(canonical_bytes(graph)) == graph


@pytest.mark.parametrize("level", [0, 3])
def test_structure_and_invariants(level):
    for frame in range(15):
        g = generate_scene(42, frame, level)
        objs = g["objects"]
        assert [o["id"] for o in objs] == list(range(1, len(objs) + 1))
        assert {o["class"] for o in objs} <= set(CLASS_IDS)
        assert sum(o["class"] == "rack" for o in objs) >= 1
        hx, hy = FLOOR_SIZE[0] / 2, FLOOR_SIZE[1] / 2
        for o in objs:
            assert abs(o["location"][0]) < hx and abs(o["location"][1]) < hy
            assert o["location"][2] > 0
            assert all(d > 0 for d in o["dimensions"])
        cam = g["camera"]
        assert cam["resolution"] == [640, 480] and cam["near"] == 0.1 and cam["far"] == 40.0
        assert cam["fov_axis"] == "horizontal"
        # racks and pallets never overlap in plan view
        fps = []
        for o in objs:
            if o["class"] in ("rack", "pallet"):
                dx, dy = o["dimensions"][0] / 2, o["dimensions"][1] / 2
                if o["yaw_deg"] == 90.0:
                    dx, dy = dy, dx
                else:
                    assert o["yaw_deg"] == 0.0
                fps.append((o["id"], o["location"][0], o["location"][1], dx, dy))
        for i, a in enumerate(fps):
            for b in fps[i + 1:]:
                assert abs(a[1] - b[1]) >= a[3] + b[3] or abs(a[2] - b[2]) >= a[4] + b[4], (a[0], b[0])


def test_resolution_is_parameter():
    g = generate_scene(42, 0, 0, width=320, height=240)
    assert g["camera"]["resolution"] == [320, 240]


# ---------------- occlusion levels ----------------

def _stats(level):
    behind, heights, pitches, dists = [], [], [], []
    for f in range(20):
        g = generate_scene(42, f, level)
        behind.append(sum(1 for o in g["objects"] if o["class"] == "pallet" and o["occlusion"]["placed_behind"] is not None))
        heights.append(g["camera"]["location"][2])
        pitches.append(g["camera"]["pitch_deg"])
    n = len(behind)
    return sum(behind) / n, sum(heights) / n, sum(pitches) / n


def test_occlusion_level_3_differs_from_0_as_documented():
    b0, h0, p0 = _stats(0)
    b3, h3, p3 = _stats(3)
    assert b0 == 0.0
    assert b3 >= 4.0                    # most of the 6 pallets are placed behind a rack/pallet
    assert h0 > 3.4 and h3 < 1.7        # high vantage vs low vantage
    assert p0 > 24.0 and p3 <= 8.0      # steep vs near-horizontal
    # monotone in between
    b1, h1, _ = _stats(1)
    b2, h2, _ = _stats(2)
    assert b0 < b1 < b2 < b3
    assert h0 > h1 > h2 > h3


def test_placed_behind_references_a_real_earlier_object():
    for f in range(20):
        g = generate_scene(42, f, 3)
        ids = {o["id"] for o in g["objects"]}
        for o in g["objects"]:
            pb = o["occlusion"]["placed_behind"]
            if pb is not None:
                assert pb in ids and pb < o["id"] and o["occlusion"]["kind"] in ("rack", "pallet")
            else:
                assert o["occlusion"]["kind"] == "none"


# ---------------- camera / projection ----------------

def _cam(location=(0.0, 0.0, 1.0), yaw=0.0, pitch=0.0, fov=90.0, w=640, h=480):
    return Camera.from_dict(camera_dict(location, yaw, pitch, fov, w, h))


def _cube(center, size=2.0, yaw=0.0):
    return {"dimensions": [size] * 3, "location": list(center), "yaw_deg": yaw}


# Hand computation: fov 90, 640x480 -> f = 320/tan(45deg) = 320 px, principal point (320, 240).
# Cube 2 m, centre (0, 10, 1), camera (0, 0, 1) looking along +Y. Front face at depth 9:
# u = 320 +/- 320*1/9 = 284.4444 .. 355.5556; v = 240 -/+ 320*1/9 = 204.4444 .. 275.5556
# (the back face at depth 11 projects strictly inside the front face).
HAND_BOX = (320 - 320 / 9, 240 - 320 / 9, 320 + 320 / 9, 240 + 320 / 9)


def _assert_box(box, expected, tol=1e-6):
    assert box is not None
    for got, want in zip(box, expected):
        assert got == pytest.approx(want, abs=tol)


def test_projection_hand_computed_cube():
    p = project_box(_cam(), _cube((0.0, 10.0, 1.0)))
    assert p.status == OK and not p.truncated
    _assert_box(p.box, HAND_BOX)
    assert HAND_BOX[0] == pytest.approx(284.4444, abs=1e-3) and HAND_BOX[3] == pytest.approx(275.5556, abs=1e-3)


def test_camera_intrinsics():
    cam = _cam()
    assert cam.focal_px == pytest.approx(320.0)
    assert cam.vertical_fov_deg == pytest.approx(2 * math.degrees(math.atan(240 / 320)))
    u, v, d = cam.project((0.0, 10.0, 1.0))  # on the optical axis
    assert (u, v) == (pytest.approx(320.0), pytest.approx(240.0)) and d == pytest.approx(10.0)
    u, v, _ = cam.project((1.0, 10.0, 2.0))  # right of and above the axis: u grows, v shrinks
    assert u == pytest.approx(352.0) and v == pytest.approx(208.0)


def test_yaw_convention_positive_yaw_looks_toward_minus_x():
    # same cube, same box, but seen with yaw=90 (looks along -X)
    p = project_box(_cam(yaw=90.0), _cube((-10.0, 0.0, 1.0)))
    _assert_box(p.box, HAND_BOX)
    # and yaw=270 looks along +X
    _assert_box(project_box(_cam(yaw=270.0), _cube((10.0, 0.0, 1.0))).box, HAND_BOX)
    # yaw=180 looks along -Y
    _assert_box(project_box(_cam(yaw=180.0), _cube((0.0, -10.0, 1.0))).box, HAND_BOX)


def test_pitch_convention_positive_pitch_looks_down():
    # camera 11 m above the cube looking straight down; image "up" is world +Y
    cam = _cam(location=(0.0, 0.0, 11.0), pitch=90.0)
    _assert_box(project_box(cam, _cube((0.0, 0.0, 1.0))).box, HAND_BOX)
    right, up, fwd = cam.basis()
    assert fwd == pytest.approx((0.0, 0.0, -1.0), abs=1e-9) and up == pytest.approx((0.0, 1.0, 0.0), abs=1e-9)
    # tilting down 45 degrees at yaw 0: forward has equal +Y and -Z parts
    f45 = _cam(pitch=45.0).basis()[2]
    assert f45 == pytest.approx((0.0, math.sqrt(0.5), -math.sqrt(0.5)))


def test_object_yaw_rotates_the_box_footprint():
    # a 2 x 4 x 2 box turned 90 degrees is 4 wide as seen from the camera: wider than unrotated
    wide = {"dimensions": [2.0, 4.0, 2.0], "location": [0.0, 10.0, 1.0], "yaw_deg": 90.0}
    narrow = dict(wide, yaw_deg=0.0)
    pw, pn = project_box(_cam(), wide), project_box(_cam(), narrow)
    assert (pw.box[2] - pw.box[0]) > (pn.box[2] - pn.box[0])
    # rotated box: x extent +/-2, nearest face at y = 10 - 1 = 9 (depth 9): u = 320 +/- 320*2/9
    _assert_box(pw.box, (320 - 640 / 9, 240 - 320 / 9, 320 + 640 / 9, 240 + 320 / 9))


def test_object_behind_camera_is_flagged():
    p = project_box(_cam(), _cube((0.0, -10.0, 1.0)))
    assert p.status == BEHIND_CAMERA and p.box is None


def test_object_outside_image_is_flagged():
    p = project_box(_cam(), _cube((200.0, 10.0, 1.0)))  # far off to the right, in front
    assert p.status == OUTSIDE_IMAGE and p.box is None


def test_box_clipped_to_image_and_flagged_truncated():
    p = project_box(_cam(), _cube((10.0, 10.0, 1.0)))  # right face projects to u = 672 > 640
    assert p.status == OK and p.truncated
    assert p.box[2] == 640.0 and 0.0 <= p.box[0] < 640.0


def test_box_straddling_near_plane_is_clipped_not_dropped():
    p = project_box(_cam(), _cube((0.0, 0.0, 1.0), size=2.0))  # camera inside the box
    assert p.status == OK and p.truncated
    assert p.box == (0.0, 0.0, 640.0, 480.0)


def test_projection_of_generated_scene_is_sane():
    g = generate_scene(42, 0, 0)
    cam = Camera.from_dict(g["camera"])
    statuses = [project_box(cam, o).status for o in g["objects"]]
    assert OK in statuses
    for o in g["objects"]:
        p = project_box(cam, o)
        if p.box:
            assert 0.0 <= p.box[0] < p.box[2] <= 640.0 and 0.0 <= p.box[1] < p.box[3] <= 480.0


# ---------------- camera pose guard ----------------

def _plan_distance_to_racks(g):
    cx, cy = g["camera"]["location"][0], g["camera"]["location"][1]
    best = float("inf")
    for o in g["objects"]:
        if o["class"] != "rack":
            continue
        hx, hy = o["dimensions"][0] / 2, o["dimensions"][1] / 2
        if o["yaw_deg"] == 90.0:
            hx, hy = hy, hx
        dx = max(abs(cx - o["location"][0]) - hx, 0.0)
        dy = max(abs(cy - o["location"][1]) - hy, 0.0)
        best = min(best, math.hypot(dx, dy))
    return best


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_camera_pose_guard_holds(level):
    from porf.scene.generator import MIN_CAM_HEIGHT, MIN_RACK_DIST

    for frame in range(40):
        g = generate_scene(42, frame, level)
        x, y, z = g["camera"]["location"]
        assert _plan_distance_to_racks(g) >= MIN_RACK_DIST - 1e-3, (level, frame)
        assert z >= MIN_CAM_HEIGHT[level], (level, frame)
        assert abs(x) <= 9.5 and abs(y) <= 7.5


def test_level3_places_nearly_all_hidden_pallets():
    """The guard keeps >= MIN_ROOM_BEHIND of floor behind the focus rack, so the hidden pallets fit.
    Chains of pallets can still run out of room occasionally: never fewer than 4 of 5, and 5 in >= 90% of frames."""
    from porf.scene.generator import BEHIND_PALLETS

    counts = []
    for frame in range(60):
        g = generate_scene(42, frame, 3)
        counts.append(sum(1 for o in g["objects"] if o["class"] == "pallet" and o["occlusion"]["placed_behind"] is not None))
    assert min(counts) >= BEHIND_PALLETS[3] - 1
    assert counts.count(BEHIND_PALLETS[3]) >= 54
    for level in (1, 2):  # lower levels always place all of theirs
        for frame in range(30):
            g = generate_scene(42, frame, level)
            assert sum(1 for o in g["objects"] if o["class"] == "pallet" and o["occlusion"]["placed_behind"] is not None) == BEHIND_PALLETS[level]


def test_deterministic_fallback_pose(monkeypatch):
    """With zero redraws allowed the fallback pose is used: still deterministic and still guarded."""
    import porf.scene.generator as gen

    monkeypatch.setattr(gen, "MAX_POSE_TRIES", 0)
    for level in (0, 3):
        for frame in range(10):
            a, b = generate_scene(42, frame, level), generate_scene(42, frame, level)
            assert a == b
            assert _plan_distance_to_racks(a) >= gen.MIN_RACK_DIST - 1e-3
            assert a["camera"]["location"][2] >= gen.MIN_CAM_HEIGHT[level]
    # the fallback pose is a different pose from the drawn one
    fallback = [generate_scene(42, f, 3)["camera"]["location"] for f in range(10)]
    monkeypatch.setattr(gen, "MAX_POSE_TRIES", 12)
    drawn = [generate_scene(42, f, 3)["camera"]["location"] for f in range(10)]
    assert drawn != fallback
