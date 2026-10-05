"""Renders scene graphs. Runs INSIDE Blender 4.5 (bpy + stdlib only).

    blender -b --factory-startup --python-exit-code 1 -P blender_job.py -- --spec render_spec.json

Blender is a renderer of a scene graph, not a generator: this file makes no random choices.
Camera parameters arrive pre-computed in the spec (porf.scene.camera.blender_camera_spec),
so no camera convention is re-derived here. Spec: docs/PROTOCOL.md sections 13 and 14.

Per frame it writes rgb.png (8-bit), depth.png (16-bit) and seg.png (16-bit).
Two Cycles renders per frame:
  * main:  N samples, normal pixel filter, OpenImageDenoise -> rgb.png
  * aux:   1 sample, filter width 0.01     -> depth + object index (exact per-pixel values)
"""
import array
import glob
import json
import math
import os
import struct
import sys
import time
import zlib

import bpy
from mathutils import Matrix, Vector

BOUNDS_EPS = 1e-4

CLASS_COLORS = {
    "rack": (0.80, 0.35, 0.05, 1.0),
    "pallet": (0.55, 0.38, 0.18, 1.0),
    "box": (0.70, 0.55, 0.35, 1.0),
    "forklift": (0.90, 0.75, 0.05, 1.0),
    "floor": (0.35, 0.35, 0.37, 1.0),
}

# unit cube, vertex i has x = bit0, y = bit1, z = bit2 of i (each +-0.5)
_CUBE_VERTS = [((i & 1) - 0.5, ((i >> 1) & 1) - 0.5, ((i >> 2) & 1) - 0.5) for i in range(8)]
_CUBE_FACES = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]


# ------------------------------------------------------------------ args / io

def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) != 2 or argv[0] != "--spec":
        raise SystemExit("usage: blender ... -- --spec render_spec.json")
    with open(argv[1], "r", encoding="utf-8") as fh:
        return json.load(fh)


def log(tag, **fields):
    print("PORF_" + tag + " " + json.dumps(fields, sort_keys=True), flush=True)


def write_png16(path, width, height, rows):
    """16-bit grayscale PNG. `rows` are big-endian byte strings, top row first."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ geometry

def parts_for(cls, dims):
    """[(centre offset, size)] in the object's local frame. Every part lies inside the box."""
    sx, sy, sz = dims
    parts = []
    if cls == "rack":
        post, panel, plank = 0.08, 0.03, 0.05
        for ix in (-1, 1):
            for iy in (-1, 1):
                parts.append(((ix * (sx / 2 - post / 2), iy * (sy / 2 - post / 2), 0.0), (post, post, sz)))
        for i in range(4):  # shelf boards, bottom to top
            z = -sz / 2 + plank / 2 + i * (sz - plank) / 3
            parts.append(((0.0, 0.0, z), (sx, sy, plank)))
        parts.append(((0.0, sy / 2 - panel / 2, 0.0), (sx, panel, sz)))  # back panel on local +y
    elif cls == "pallet":
        deck = min(0.03, 0.2 * sz)
        parts.append(((0.0, 0.0, sz / 2 - deck / 2), (sx, sy, deck)))
        for iy in (-1, 0, 1):
            parts.append(((0.0, iy * (sy / 2 - 0.05), -deck / 2), (sx, 0.1, sz - deck)))
    elif cls == "box":
        parts.append(((0.0, 0.0, 0.0), (sx, sy, sz)))
    elif cls == "forklift":  # forks point toward local +y
        parts.append(((0.0, -sy / 2 + 0.29 * sy, -sz / 2 + 0.215 * sz), (sx, 0.58 * sy, 0.43 * sz)))        # chassis
        parts.append(((0.0, -sy / 2 + 0.085 * sy, -sz / 2 + 0.165 * sz), (sx, 0.17 * sy, 0.33 * sz)))       # counterweight
        for ix in (-1, 1):
            parts.append(((ix * 0.36 * sx, 0.15 * sy, -sz / 2 + 0.4 * sz), (0.09 * sx, 0.05 * sy, 0.8 * sz)))  # mast
            parts.append(((ix * 0.27 * sx, 0.29 * sy, -sz / 2 + 0.0125 * sz), (0.11 * sx, 0.42 * sy, 0.025 * sz)))  # forks
            for y in (-0.32, -0.02):
                parts.append(((ix * 0.4 * sx, y * sy, -sz / 2 + 0.7 * sz), (0.055 * sx, 0.03 * sy, 0.54 * sz)))  # guard posts
        parts.append(((0.0, -0.17 * sy, sz / 2 - 0.0125 * sz), (0.95 * sx, 0.45 * sy, 0.025 * sz)))          # roof
    else:
        raise ValueError("unknown class " + repr(cls))
    return parts


def make_material(name, color):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Roughness"].default_value = 0.8
    return mat


def make_cube_mesh(name, material):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(_CUBE_VERTS, [], _CUBE_FACES)
    mesh.update()
    mesh.validate()
    mesh.materials.append(material)
    return mesh


class Stage:
    """Scene, camera, light, materials and shared meshes, built once per Blender process."""

    def __init__(self, spec):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.scene = bpy.context.scene
        sc = self.scene
        sc.render.engine = "CYCLES"
        sc.render.resolution_x, sc.render.resolution_y = spec["width"], spec["height"]
        sc.render.resolution_percentage = 100
        sc.render.pixel_aspect_x = sc.render.pixel_aspect_y = 1.0
        sc.render.film_transparent = False
        sc.view_settings.view_transform = "Standard"
        sc.view_settings.look = "None"
        sc.frame_current = 1
        cy = sc.cycles
        cy.seed = 0
        cy.use_animated_seed = False
        cy.use_adaptive_sampling = False
        cy.use_denoising = False
        cy.max_bounces = 4
        cy.device = "CPU"
        if spec.get("gpu"):
            self._enable_gpu()
        self.samples = spec["samples"]
        sc.view_layers[0].use_pass_z = True
        sc.view_layers[0].use_pass_object_index = True

        self.meshes = {k: make_cube_mesh("cube_" + k, make_material("mat_" + k, c)) for k, c in CLASS_COLORS.items()}
        world = bpy.data.worlds.new("world")
        world.use_nodes = True
        sc.world = world
        self.world_bg = world.node_tree.nodes["Background"]
        self.world_bg.inputs["Color"].default_value = (0.55, 0.6, 0.7, 1.0)

        cam_data = bpy.data.cameras.new("camera")
        self.camera = bpy.data.objects.new("camera", cam_data)
        sc.collection.objects.link(self.camera)
        sc.camera = self.camera

        sun_data = bpy.data.lights.new("sun", "SUN")
        sun_data.angle = math.radians(1.0)
        self.sun = bpy.data.objects.new("sun", sun_data)
        sc.collection.objects.link(self.sun)

    @staticmethod
    def _enable_gpu():
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        for dev in prefs.devices:
            dev.use = dev.type != "CPU"
        bpy.context.scene.cycles.device = "GPU"

    def clear_objects(self):
        keep = {self.camera, self.sun}
        for obj in list(bpy.data.objects):
            if obj not in keep:
                bpy.data.objects.remove(obj, do_unlink=True)

    def _add_part(self, mesh_key, name, parent, offset, size, pass_index):
        obj = bpy.data.objects.new(name, self.meshes[mesh_key])
        obj.parent = parent
        obj.location = offset
        obj.scale = size
        obj.pass_index = pass_index
        self.scene.collection.objects.link(obj)
        return obj

    def build(self, graph, camera_spec):
        """Create geometry/camera/light for one scene graph. Raises if a part leaves its box."""
        self.clear_objects()
        sc = self.scene

        w, h = graph["floor"]["size"]
        self._add_part("floor", "floor", None, (0.0, 0.0, -0.01), (w, h, 0.02), 0)  # top surface at z = 0

        built = []
        for obj in graph["objects"]:
            oid = obj["id"]
            root = bpy.data.objects.new("obj%d" % oid, None)
            root.location = obj["location"]
            root.rotation_euler = (0.0, 0.0, math.radians(obj["yaw_deg"]))
            sc.collection.objects.link(root)
            parts = [
                self._add_part(obj["class"], "obj%d_p%d" % (oid, i), root, off, size, oid)
                for i, (off, size) in enumerate(parts_for(obj["class"], obj["dimensions"]))
            ]
            built.append((obj, root, parts))

        cam = self.camera
        cam.location = camera_spec["location"]
        cam.rotation_mode = camera_spec["rotation_mode"]
        cam.rotation_euler = camera_spec["rotation_euler_rad"]
        cd = cam.data
        cd.sensor_fit = camera_spec["sensor_fit"]
        cd.lens_unit = camera_spec["lens_unit"]
        cd.angle = camera_spec["angle_rad"]
        cd.shift_x, cd.shift_y = camera_spec["shift"]
        cd.clip_start, cd.clip_end = camera_spec["clip_start"], camera_spec["clip_end"]
        sc.render.resolution_x, sc.render.resolution_y = camera_spec["resolution"]

        sun = graph["lighting"]["sun"]  # rotated like a camera: the light travels along that heading
        self.sun.rotation_mode = "XYZ"
        self.sun.rotation_euler = (math.radians(90.0 - sun["elevation_deg"]), 0.0, math.radians(sun["yaw_deg"]))
        self.sun.data.energy = sun["energy"]
        self.world_bg.inputs["Strength"].default_value = graph["lighting"]["ambient"]

        bpy.context.view_layer.update()
        for obj, root, parts in built:
            assert_inside_box(obj, root, parts)

    # ------------------------------------------------------------ rendering

    def render_rgb(self, path):
        sc = self.scene
        sc.use_nodes = False
        sc.cycles.samples = self.samples
        sc.cycles.filter_width = 1.5
        sc.cycles.max_bounces = 4
        sc.cycles.use_denoising = True  # RGB only; the aux render must stay unfiltered
        sc.cycles.denoiser = "OPENIMAGEDENOISE"
        sc.render.filepath = path
        sc.render.image_settings.file_format = "PNG"
        sc.render.image_settings.color_mode = "RGB"
        sc.render.image_settings.color_depth = "8"
        sc.render.image_settings.compression = 15
        bpy.ops.render.render(write_still=True)

    def render_aux(self, tmp_dir):
        """1 sample at the pixel centre (filter width 0.01): exact depth and object index."""
        sc = self.scene
        sc.cycles.samples = 1
        sc.cycles.filter_width = 0.01
        sc.cycles.max_bounces = 0
        sc.cycles.use_denoising = False
        sc.use_nodes = True
        tree = sc.node_tree
        tree.nodes.clear()
        rl = tree.nodes.new("CompositorNodeRLayers")
        fo = tree.nodes.new("CompositorNodeOutputFile")
        fo.base_path = tmp_dir
        fo.format.file_format = "OPEN_EXR"
        fo.format.color_depth = "32"
        fo.format.color_mode = "RGB"
        fo.format.exr_codec = "NONE"
        fo.file_slots[0].path = "depth"
        fo.file_slots.new("idx")
        tree.links.new(rl.outputs["Depth"], fo.inputs[0])
        tree.links.new(rl.outputs["IndexOB"], fo.inputs[1])
        for old in glob.glob(os.path.join(tmp_dir, "*.exr")):
            os.remove(old)
        bpy.ops.render.render(write_still=False)
        sc.use_nodes = False
        return (
            read_exr_channel(glob.glob(os.path.join(tmp_dir, "depth*.exr"))[0], sc),
            read_exr_channel(glob.glob(os.path.join(tmp_dir, "idx*.exr"))[0], sc),
        )


def read_exr_channel(path, scene):
    """Red channel of a float EXR as (array('f'), width, height), rows bottom-up."""
    img = bpy.data.images.load(path, check_existing=False)
    img.colorspace_settings.name = "Non-Color"
    w, h = img.size
    buf = array.array("f", bytes(4 * w * h * 4))
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    return buf, w, h


def assert_inside_box(obj, root, parts):
    """Every part's world-space bounds must lie inside the scene-graph box of its object."""
    inv = root.matrix_world.inverted()
    half = [d / 2.0 + BOUNDS_EPS for d in obj["dimensions"]]
    for part in parts:
        for corner in part.bound_box:
            local = inv @ (part.matrix_world @ Vector(corner))
            for axis in range(3):
                assert abs(local[axis]) <= half[axis], (
                    "geometry of object %d (%s) leaves its box: axis %d, local %.5f, limit %.5f"
                    % (obj["id"], obj["class"], axis, local[axis], half[axis])
                )


# ------------------------------------------------------------------ encoders

def encode_depth_png(path, buf, w, h, near, far):
    """depth.png: 16-bit, v = round_half_up(65535 * clamp((z - near)/(far - near), 0, 1)); z = distance
    along the camera axis; rays that hit nothing (z huge) saturate at 65535."""
    scale = 65535.0 / (far - near)
    rows = []
    for y in range(h - 1, -1, -1):  # Blender rows are bottom-up; PNG is top-down
        row = buf[y * w * 4: (y + 1) * w * 4: 4]
        vals = [min(65535, max(0, int((z - near) * scale + 0.5))) for z in row]
        rows.append(struct.pack(">%dH" % w, *vals))
    write_png16(path, w, h, rows)


def encode_seg_png(path, buf, w, h, valid_ids):
    """seg.png: 16-bit, pixel = object id, 0 = background. Asserts every value is an exact
    integer in {0} + valid_ids (no anti-aliasing, no filtering)."""
    rows, seen = [], set()
    for y in range(h - 1, -1, -1):
        row = buf[y * w * 4: (y + 1) * w * 4: 4]
        for v in set(row):
            iv = int(v)
            assert v == iv and iv in valid_ids, "seg value %r is not 0 or a real object id" % (v,)
            seen.add(iv)
        rows.append(struct.pack(">%dH" % w, *[int(v) for v in row]))
    write_png16(path, w, h, rows)
    return sorted(seen)


# ------------------------------------------------------------------ main

def main():
    spec = parse_args()
    t0 = time.time()
    stage = Stage(spec)
    log("TIMING", phase="setup", seconds=round(time.time() - t0, 3))
    for fr in spec["frames"]:
        out_dir = fr["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        with open(fr["scene"], "r", encoding="utf-8") as fh:
            graph = json.load(fh)
        near, far = graph["camera"]["near"], graph["camera"]["far"]
        valid_ids = {0} | {o["id"] for o in graph["objects"]}

        t = time.time()
        stage.build(graph, fr["camera"])
        t_build = time.time() - t
        t = time.time()
        stage.render_rgb(os.path.join(out_dir, "rgb.png"))
        t_rgb = time.time() - t
        t = time.time()
        tmp = os.path.join(out_dir, ".tmp")
        os.makedirs(tmp, exist_ok=True)
        (depth, w, h), (idx, w2, h2) = stage.render_aux(tmp)
        t_aux = time.time() - t
        assert (w, h) == (w2, h2) == tuple(fr["camera"]["resolution"])
        t = time.time()
        encode_depth_png(os.path.join(out_dir, "depth.png"), depth, w, h, near, far)
        ids_seen = encode_seg_png(os.path.join(out_dir, "seg.png"), idx, w, h, valid_ids)
        for f in glob.glob(os.path.join(tmp, "*.exr")):
            os.remove(f)
        os.rmdir(tmp)
        t_enc = time.time() - t
        log("SEG_CHECK", frame=fr["frame_index"], ids_seen=ids_seen, ok=True)
        log("TIMING", phase="frame", frame=fr["frame_index"], build=round(t_build, 3), rgb=round(t_rgb, 3),
            aux=round(t_aux, 3), encode=round(t_enc, 3))
    log("TIMING", phase="total", seconds=round(time.time() - t0, 3))


main()
