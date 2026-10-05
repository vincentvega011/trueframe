"""Renderer tests. The first group needs no Blender. Tests marked `slow` render with real
Blender (4.5) and are skipped if it is not installed; deselect with `pytest -m "not slow"`."""
import json
import math

import numpy as np
import pytest

from porf.canon import canonical_bytes, hash_json
from porf.render.runner import (
    build_annotations,
    check_seg_integrity,
    find_blender,
    frame_dir,
    read_seg,
    render_frames,
)
from porf.scene.camera import Camera, blender_camera_spec, camera_dict
from porf.scene.generator import CLASS_IDS, generate_scene
from porf.scene.projection import OK, project_box, silhouette_area


# ---------------- no Blender needed ----------------

def _euler_xyz(rx, ry, rz):
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx  # Blender 'XYZ' Euler: X applied first


@pytest.mark.parametrize("yaw,pitch", [(0, 0), (90, 20), (176.9467, 28.0007), (270, 90), (33, 5)])
def test_blender_camera_spec_matches_camera_basis(yaw, pitch):
    cam = Camera.from_dict(camera_dict((1.0, 2.0, 3.0), yaw, pitch, 70.0, 640, 480))
    spec = blender_camera_spec(cam)
    R = _euler_xyz(*spec["rotation_euler_rad"])
    right, up, fwd = cam.basis()
    # Blender camera: +X right, +Y up, looks down local -Z
    assert R @ np.array([1, 0, 0]) == pytest.approx(right, abs=1e-9)
    assert R @ np.array([0, 1, 0]) == pytest.approx(up, abs=1e-9)
    assert R @ np.array([0, 0, -1]) == pytest.approx(fwd, abs=1e-9)
    assert spec["location"] == [1.0, 2.0, 3.0]
    assert spec["sensor_fit"] == "HORIZONTAL" and spec["angle_rad"] == pytest.approx(math.radians(70.0))
    assert spec["resolution"] == [640, 480] and spec["clip_start"] == 0.1 and spec["clip_end"] == 40.0


def _cam():
    return Camera.from_dict(camera_dict((0.0, 0.0, 1.0), 0.0, 0.0, 90.0, 640, 480))


def _cube(center, size=2.0):
    return {"dimensions": [size] * 3, "location": list(center), "yaw_deg": 0.0}


def test_silhouette_area_hand_computed():
    # front face 2 m square at depth 9, f = 320 px -> (640/9)^2 px^2; the back face lies inside it
    assert silhouette_area(_cam(), _cube((0.0, 10.0, 1.0))) == pytest.approx((640 / 9) ** 2)
    # cube centred at x = 10: its front face (u = 640..711) is off-screen, so only the visible left face
    # remains: a trapezoid from u = 320 + 320*9/11 = 581.82 to the image edge u = 640 (width 640/11 px),
    # with vertical sides 640/11 px (far edge, depth 11) and 640/9 px (near edge, depth 9)
    w = 640 / 11
    assert silhouette_area(_cam(), _cube((10.0, 10.0, 1.0))) == pytest.approx((w + 640 / 9) / 2 * w)
    assert silhouette_area(_cam(), _cube((0.0, -10.0, 1.0))) == 0.0     # behind the camera
    assert silhouette_area(_cam(), _cube((200.0, 10.0, 1.0))) == 0.0    # off-screen


def test_annotations_from_synthetic_seg():
    scene = generate_scene(42, 0, 0)
    seg = np.zeros((480, 640), dtype=np.int64)
    seg[100:140, 200:300] = 1        # object 1 visible: 40 x 100 px
    seg[10, 10] = 2                  # object 2 visible: a single pixel
    ann = build_annotations(scene, seg)
    objs = ann["objects"]
    assert [o["instance_id"] for o in objs] == [o["id"] for o in scene["objects"]]
    o1, o2, o3 = objs[0], objs[1], objs[2]
    assert o1["bbox"] == [200, 100, 300, 140] and o1["mask_pixels"] == 4000
    assert o2["bbox"] == [10, 10, 11, 11] and o2["mask_pixels"] == 1
    assert o3["bbox"] is None and o3["mask_pixels"] == 0 and o3["visibility"] == 0.0
    assert o1["class_id"] == CLASS_IDS[o1["class"]]
    for o in objs:
        assert 0.0 <= o["visibility"] <= 1.0 and round(o["visibility"], 4) == o["visibility"]
    assert ann["image"] == {"width": 640, "height": 480}
    assert json.loads(canonical_bytes(ann)) == ann


def test_seg_integrity_rejects_stray_values():
    scene = generate_scene(42, 0, 0)
    n = len(scene["objects"])
    ok = np.zeros((4, 4), dtype=np.int64)
    ok[0, 0], ok[1, 1] = 1, n
    assert check_seg_integrity(ok, scene) == [0, 1, n]
    for bad_value in (n + 1, 65535, 999):
        bad = ok.copy()
        bad[2, 2] = bad_value
        with pytest.raises(AssertionError):
            check_seg_integrity(bad, scene)


# ---------------- real Blender ----------------

needs_blender = pytest.mark.skipif(find_blender() is None, reason="Blender not installed")
FRAMES = 5


@pytest.fixture(scope="module", params=[0, 3], ids=["occlusion0", "occlusion3"])
def rendered(request, tmp_path_factory):
    level = request.param
    out = tmp_path_factory.mktemp(f"render_L{level}")
    res = render_frames(42, list(range(FRAMES)), out, occlusion_level=level, samples=8)
    return level, out, res


@pytest.mark.slow
@needs_blender
def test_seg_ids_match_scene_object_ids_exactly(rendered):
    level, out, _ = rendered
    from PIL import Image

    for i in range(FRAMES):
        fdir = frame_dir(out, i)
        scene = json.loads((fdir / "scene.json").read_text())
        ann = json.loads((fdir / "annotations.json").read_text())
        ids = [o["id"] for o in scene["objects"]]
        seg_img = Image.open(fdir / "seg.png")
        assert seg_img.mode == "I;16" and seg_img.size == (640, 480)
        present = set(int(v) for v in np.unique(read_seg(fdir / "seg.png")))
        assert present <= {0} | set(ids)                      # nothing but background and real ids
        assert 0 in present                                   # sky/background exists in every frame
        # annotations list every scene object, same ids, same classes, same order
        assert [o["instance_id"] for o in ann["objects"]] == ids
        assert [o["class"] for o in ann["objects"]] == [o["class"] for o in scene["objects"]]
        for o in ann["objects"]:
            assert (o["bbox"] is None) == (o["instance_id"] not in present)
            assert (o["mask_pixels"] > 0) == (o["instance_id"] in present)
            if o["bbox"] is None:
                assert o["visibility"] == 0.0 and o["mask_pixels"] == 0
            else:
                assert 0.0 < o["visibility"] <= 1.0


@pytest.mark.slow
@needs_blender
def test_mask_boxes_inside_projected_boxes_within_2px(rendered):
    level, out, _ = rendered
    tol = 2.0
    checked = 0
    for i in range(FRAMES):
        fdir = frame_dir(out, i)
        scene = json.loads((fdir / "scene.json").read_text())
        ann = json.loads((fdir / "annotations.json").read_text())
        cam = Camera.from_dict(scene["camera"])
        by_id = {o["id"]: o for o in scene["objects"]}
        for a in ann["objects"]:
            if a["bbox"] is None:
                continue
            proj = project_box(cam, by_id[a["instance_id"]])
            assert proj.status == OK, (i, a["instance_id"], proj.status)
            mx0, my0, mx1, my1 = a["bbox"]
            px0, py0, px1, py1 = proj.box
            assert mx0 >= px0 - tol and my0 >= py0 - tol and mx1 <= px1 + tol and my1 <= py1 + tol, (
                f"frame {i} obj {a['instance_id']} ({a['class']}): mask {a['bbox']} vs projected {proj.box}"
            )
            checked += 1
    assert checked >= 20


@pytest.mark.slow
@needs_blender
def test_hash_conventions_and_file_set(rendered):
    import hashlib

    level, out, res = rendered
    for i in range(FRAMES):
        fdir = frame_dir(out, i)
        assert sorted(p.name for p in fdir.iterdir()) == ["annotations.json", "depth.png", "rgb.png", "scene.json", "seg.png"]
        scene = json.loads((fdir / "scene.json").read_text())
        assert hashlib.sha256((fdir / "scene.json").read_bytes()).digest() == hash_json(scene)
        assert (fdir / "annotations.json").read_bytes() == canonical_bytes(json.loads((fdir / "annotations.json").read_text()))
        assert scene == generate_scene(42, i, level)          # runner rendered exactly the generator's graph


@pytest.mark.slow
@needs_blender
def test_level3_hides_more_objects_than_level0(tmp_path_factory):
    def hidden(level):
        out = tmp_path_factory.mktemp(f"cmp_L{level}")
        res = render_frames(42, list(range(FRAMES)), out, occlusion_level=level, samples=1)
        return sum(1 for r in res["frames"].values() for o in r["annotations"]["objects"] if o["bbox"] is None)

    assert hidden(3) > hidden(0) + 10


@pytest.mark.slow
@needs_blender
def test_level3_frames_keep_enough_visible_objects(tmp_path):
    """The pose guard and view-cone placement exist so level 3 is occluded but never empty."""
    res = render_frames(42, list(range(10)), tmp_path, occlusion_level=3, samples=1)
    visible = [sum(1 for o in r["annotations"]["objects"] if o["bbox"] is not None) for r in res["frames"].values()]
    assert min(visible) >= 6, visible


@pytest.mark.slow
@needs_blender
def test_containment_assertion_fires_and_blender_exits_nonzero(tmp_path):
    """Test-only hook inflates one part of object 1 past its box: Blender must fail loudly."""
    with pytest.raises(RuntimeError) as exc:
        render_frames(42, [0], tmp_path, samples=1, test_inflate={"object_id": 1, "part": 0, "extra": 0.3})
    msg = str(exc.value)
    assert "Blender failed (exit 1)" in msg and "leaves its box" in msg and "object 1" in msg
    assert not (frame_dir(tmp_path, 0) / "rgb.png").exists()  # failed before rendering anything
    # control: the identical job without the hook passes the same assertion
    render_frames(42, [0], tmp_path / "ok", samples=1)
