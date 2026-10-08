"""The report server: stateless, read-only, CORS, and nothing outside the report files (docs/PROTOCOL.md section 17.6)."""
import json
import urllib.error
import urllib.request

import numpy as np
import pytest
from PIL import Image

from porf.verifier.artifacts import diff_image, false_colour
from porf.verifier.serve import ReportServer

ADDR = "8HM2AfdakwfLYWBVnwC3siuWGfisjdMD3T3hVe36vcZS"


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.fixture
def out(tmp_path):
    d = tmp_path / ADDR
    (d / "frames" / "3").mkdir(parents=True)
    (d / "report.json").write_text(json.dumps({"verdict": "Fail", "updated_at": 1.0, "outcome": {"action": "slashed"}}))
    Image.new("RGB", (4, 4)).save(d / "frames" / "3" / "rgb.png")
    (tmp_path / "secret.txt").write_text("nope")
    (d / "notes.txt").write_text("not served")
    return tmp_path


def test_serves_reports_images_and_an_index_with_cors(out):
    s = ReportServer(out, port=0).start()
    try:
        st, h, body = get(f"{s.url}/{ADDR}/report.json")
        assert st == 200 and json.loads(body)["verdict"] == "Fail" and h["Access-Control-Allow-Origin"] == "*"
        st, h, body = get(f"{s.url}/{ADDR}/frames/3/rgb.png")
        assert st == 200 and h["Content-Type"] == "image/png" and body[:4] == b"\x89PNG"
        st, _, body = get(s.url + "/")
        assert json.loads(body)["reports"] == [{"request": ADDR, "verdict": "Fail", "updated_at": 1.0, "outcome": "slashed"}]
    finally:
        s.stop()


def test_a_restarted_server_serves_the_same_old_report(out):
    for _ in range(2):
        s = ReportServer(out, port=0).start()
        try:
            assert get(f"{s.url}/{ADDR}/report.json")[0] == 200
        finally:
            s.stop()


@pytest.mark.parametrize("path", ["/../secret.txt", f"/{ADDR}/../secret.txt", f"/{ADDR}/notes.txt", f"/{ADDR}/frames/3/../../notes.txt",
                                  f"/{ADDR}/frames/9/rgb.png", "/%2e%2e/secret.txt", f"/{ADDR}/report.json/x"])
def test_nothing_else_is_served(out, path):
    s = ReportServer(out, port=0).start()
    try:
        assert get(s.url + path)[0] == 404
    finally:
        s.stop()


def test_false_colour_is_stable_and_diff_marks_exactly_the_differing_pixels():
    a = np.zeros((4, 6), dtype=np.int64)
    a[1:3, 1:3] = 5
    b = a.copy()
    b[1, 1] = 0
    assert np.array_equal(np.array(false_colour(a)), np.array(false_colour(a)))
    d = np.array(diff_image(a, b))
    red = (d == [255, 77, 77]).all(axis=2)
    assert red.sum() == 1 and red[1, 1]


def test_overlay_uses_red_only_for_a_box_that_fails_and_marks_hidden_objects():
    """Colour semantics of overlay.png: solid green = a node box that passes, solid red = a node box that fails a check,
    dashed grey = what the scene predicts, "hidden" = projected but unlabelled."""
    import io

    from porf.node import cheats
    from porf.render.runner import build_annotations
    from porf.scene.generator import generate_scene
    from porf.verifier.artifacts import GREEN, RED, _overlay, failing_objects
    from porf.verifier.checks import DEFAULT_THRESHOLDS
    from porf.verifier.reveal import Reveal

    scene = generate_scene(5150, 3, 3, 640, 480)
    seg = cheats.paint_silhouettes(scene)                       # a seg.png and boxes that agree with each other
    ann = build_annotations(scene, seg)
    buf = io.BytesIO()
    Image.fromarray(seg.astype(np.uint16)).save(buf, format="PNG")
    reveal = Reveal(raw={}, annotations=ann, seg_png=buf.getvalue())
    assert failing_objects(reveal, scene, None, DEFAULT_THRESHOLDS) == set()      # consistent boxes: nothing fails without a re-render
    victim = next(o for o in ann["objects"] if o["bbox"])
    victim["bbox"] = [victim["bbox"][0] + 40, victim["bbox"][1], victim["bbox"][2] + 40, victim["bbox"][3]]
    assert victim["instance_id"] in failing_objects(reveal, scene, None, DEFAULT_THRESHOLDS)

    rgb = io.BytesIO()
    Image.new("RGB", (640, 480), (30, 30, 30)).save(rgb, format="PNG")
    img = np.array(_overlay(rgb.getvalue(), ann, scene, {victim["instance_id"]}))
    assert (img == list(RED)).all(axis=2).any() and (img == list(GREEN)).all(axis=2).any()
    plain = np.array(_overlay(rgb.getvalue(), ann, scene, set()))
    assert not (plain == list(RED)).all(axis=2).any()                            # no failure, no red


def test_a_label_is_skipped_when_it_would_overlap_one_already_drawn_but_the_outline_stays():
    from PIL import ImageDraw

    from porf.verifier.artifacts import GREY, _label
    img = Image.new("RGB", (200, 100))
    draw = ImageDraw.Draw(img)
    taken: list = []
    assert _label(draw, (10, 10), "hidden", GREY, taken) is True
    assert _label(draw, (20, 14), "hidden", GREY, taken) is False        # overlaps the first
    assert _label(draw, (10, 60), "hidden", GREY, taken) is True         # clear of it
    assert len(taken) == 2
