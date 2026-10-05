"""Deterministic scene-graph generator for warehouse_v1. Spec: docs/PROTOCOL.md section 13.

Pure Python, stdlib only. All randomness comes from one per-frame RNG; Blender never
makes random choices. No trigonometry is used here: object yaws that other placements
depend on are restricted to exact multiples of 90 degrees, so every value that enters
the graph comes from IEEE +,-,*,/,sqrt and the RNG, and is identical across machines.
Every float passes through canon.quantize before entering the graph.
"""
from __future__ import annotations

import math
import random

from ..canon import quantize, sha256
from .camera import DEFAULT_HEIGHT, DEFAULT_WIDTH, camera_dict

SCENE_TYPE = "warehouse_v1"

CLASS_IDS = {"rack": 1, "pallet": 2, "box": 3, "forklift": 4}

FLOOR_SIZE = (20.0, 16.0)  # x extent, y extent; floor spans [-10, 10] x [-8, 8], top at z = 0
WALL_MARGIN = 0.5
PALLET_DIMS = (1.2, 0.8, 0.15)
N_PALLETS = 6

# Per-occlusion-level rules (index = occlusion_level). See PROTOCOL.md section 13.4.
BEHIND_PALLETS = (0, 1, 3, 5)                                      # pallets placed behind a rack/pallet
CAM_HEIGHT = ((3.5, 5.0), (2.5, 3.5), (1.6, 2.5), (0.9, 1.6))      # camera z range
CAM_PITCH = ((25.0, 35.0), (18.0, 28.0), (8.0, 18.0), (0.0, 8.0))  # degrees looking down
CAM_DIST = ((7.0, 10.0), (6.0, 8.5), (4.5, 7.0), (3.5, 5.5))       # camera to focus rack
YAW_JITTER = (12.0, 10.0, 7.0, 5.0)                                # degrees around the aim direction

# Camera pose guard (PROTOCOL.md 13.4): checked on the final, wall-clamped position.
MIN_RACK_DIST = 3.5                                                # m, camera to the nearest rack surface (plan view)
MIN_CAM_HEIGHT = (3.5, 2.5, 1.6, 1.0)                              # m; equals the draw range's floor except at level 3
MIN_ROOM_BEHIND = 3.0                                              # m of floor behind the focus rack (when pallets hide behind it)
MAX_POSE_TRIES = 12                                                # redraws before the deterministic fallback

# (dx, dy) forward direction, lateral axis (lx, ly), base yaw in degrees
_DIRS = (
    ((0, 1), (1, 0), 0.0),
    ((-1, 0), (0, 1), 90.0),
    ((0, -1), (1, 0), 180.0),
    ((1, 0), (0, 1), 270.0),
)


def frame_rng(seed: int, frame_index: int) -> random.Random:
    digest = sha256(seed.to_bytes(8, "little") + frame_index.to_bytes(4, "little"))
    return random.Random(int.from_bytes(digest[:8], "little"))


def _dist_to_footprints(x: float, y: float, footprints) -> float:
    """Plan-view distance from (x, y) to the nearest footprint rectangle (0 if inside one)."""
    best = float("inf")
    for ox, oy, hx, hy in footprints:
        ddx = max(abs(x - ox) - hx, 0.0)
        ddy = max(abs(y - oy) - hy, 0.0)
        best = min(best, math.sqrt(ddx * ddx + ddy * ddy))
    return best


def _norm_yaw(deg: float) -> float:
    return quantize(deg % 360.0)


class _Builder:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.objects: list[dict] = []
        self.footprints: list[tuple[float, float, float, float]] = []  # cx, cy, hx, hy

    # -- random helpers (every draw is quantized at the source) --
    def u(self, lo: float, hi: float) -> float:
        return quantize(self.rng.uniform(lo, hi))

    # -- geometry helpers --
    @staticmethod
    def half_extents(dims: tuple[float, float, float], yaw: float) -> tuple[float, float]:
        if yaw == 0.0 or yaw == 180.0:
            return dims[0] / 2.0, dims[1] / 2.0
        if yaw == 90.0 or yaw == 270.0:
            return dims[1] / 2.0, dims[0] / 2.0
        r = math.sqrt(dims[0] * dims[0] + dims[1] * dims[1]) / 2.0  # circumscribed square
        return r, r

    @staticmethod
    def in_floor(cx: float, cy: float, hx: float, hy: float) -> bool:
        return (
            abs(cx) + hx <= FLOOR_SIZE[0] / 2.0 - WALL_MARGIN
            and abs(cy) + hy <= FLOOR_SIZE[1] / 2.0 - WALL_MARGIN
        )

    def free(self, cx: float, cy: float, hx: float, hy: float, margin: float) -> bool:
        if not self.in_floor(cx, cy, hx, hy):
            return False
        for ox, oy, ohx, ohy in self.footprints:
            if abs(cx - ox) < hx + ohx + margin and abs(cy - oy) < hy + ohy + margin:
                return False
        return True

    def add(self, cls: str, dims, loc, yaw, behind: int | None = None, kind: str = "none", footprint=True) -> dict:
        obj = {
            "id": len(self.objects) + 1,
            "class": cls,
            "dimensions": [quantize(v) for v in dims],
            "location": [quantize(v) for v in loc],
            "yaw_deg": _norm_yaw(yaw),
            "occlusion": {"placed_behind": behind, "kind": kind},
        }
        self.objects.append(obj)
        if footprint:
            hx, hy = self.half_extents(tuple(dims), obj["yaw_deg"])
            self.footprints.append((loc[0], loc[1], hx, hy))
        return obj

    def place_open(self, hx: float, hy: float, margin: float, tries: int = 40):
        for _ in range(tries):
            cx = self.u(-(FLOOR_SIZE[0] / 2 - WALL_MARGIN - hx), FLOOR_SIZE[0] / 2 - WALL_MARGIN - hx)
            cy = self.u(-(FLOOR_SIZE[1] / 2 - WALL_MARGIN - hy), FLOOR_SIZE[1] / 2 - WALL_MARGIN - hy)
            if self.free(cx, cy, hx, hy, margin):
                return cx, cy
        return None


def generate_scene(
    seed: int,
    frame_index: int,
    occlusion_level: int = 0,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> dict:
    """Return the scene graph (plain JSON-compatible dict) for (seed, frame_index)."""
    if not 0 <= seed < 2**64:
        raise ValueError("seed must fit in u64")
    if not 0 <= frame_index < 2**32:
        raise ValueError("frame_index must fit in u32")
    if occlusion_level not in (0, 1, 2, 3):
        raise ValueError("occlusion_level must be 0..3")

    L = occlusion_level
    b = _Builder(frame_rng(seed, frame_index))
    rng = b.rng

    # ---- racks ----
    racks: list[dict] = []
    for _ in range(rng.randint(3, 5)):
        length = rng.choice((2.5, 3.0, 3.5))
        height_r = rng.choice((3.0, 3.5, 4.0))
        yaw = rng.choice((0.0, 90.0))
        dims = (length, 1.0, height_r)
        hx, hy = b.half_extents(dims, yaw)
        pos = b.place_open(hx, hy, margin=1.8, tries=60)
        if pos is None:
            continue
        racks.append(b.add("rack", dims, (pos[0], pos[1], height_r / 2.0), yaw))
    if not racks:  # practically unreachable; keeps the graph valid and deterministic
        racks.append(b.add("rack", (3.0, 1.0, 3.0), (0.0, 0.0, 1.5), 0.0))

    # ---- aim direction, focus rack, camera (with pose guard) ----
    def pose_from(dir_idx, rack, dist_raw, lat, z):
        (dx_, dy_), (lx_, ly_), base = _DIRS[dir_idx]
        r_hx, r_hy = b.half_extents(tuple(rack["dimensions"]), rack["yaw_deg"])
        dist_ = max(dist_raw, (r_hx if dx_ != 0 else r_hy) + 2.0)
        x = max(-9.5, min(9.5, rack["location"][0] - dx_ * dist_ + lx_ * lat))  # wall clamp
        y = max(-7.5, min(7.5, rack["location"][1] - dy_ * dist_ + ly_ * lat))
        return {"dir": dir_idx, "rack": rack, "dist": dist_, "x": x, "y": y, "z": z}

    def room_behind(pose):
        (dx_, dy_), _, _ = _DIRS[pose["dir"]]
        rack = pose["rack"]
        r_hx, r_hy = b.half_extents(tuple(rack["dimensions"]), rack["yaw_deg"])
        if dx_ != 0:
            return (9.5 - (rack["location"][0] + r_hx)) if dx_ > 0 else ((rack["location"][0] - r_hx) + 9.5)
        return (7.5 - (rack["location"][1] + r_hy)) if dy_ > 0 else ((rack["location"][1] - r_hy) + 7.5)

    def pose_ok(pose):
        # evaluated AFTER the wall clamp, on the final camera position
        if pose["z"] < MIN_CAM_HEIGHT[L]:
            return False
        if _dist_to_footprints(pose["x"], pose["y"], b.footprints) < MIN_RACK_DIST:
            return False
        return BEHIND_PALLETS[L] == 0 or room_behind(pose) >= MIN_ROOM_BEHIND

    pose = None
    for _ in range(MAX_POSE_TRIES):  # bounded redraws from the same per-frame RNG stream
        pose = pose_from(rng.randrange(4), racks[rng.randrange(len(racks))], b.u(*CAM_DIST[L]), b.u(-1.5, 1.5), b.u(*CAM_HEIGHT[L]))
        if pose_ok(pose):
            break
    else:
        # deterministic fallback, no randomness: first (rack, direction) in generation/table order whose
        # pose, at the far end of the level's distance and height range, centred, passes the guard
        fallback = None
        for rack in racks:
            for dir_idx in range(4):
                cand = pose_from(dir_idx, rack, CAM_DIST[L][1], 0.0, CAM_HEIGHT[L][1])
                if pose_ok(cand):
                    fallback = cand
                    break
            if fallback:
                break
        pose = fallback or pose  # unreachable in practice; otherwise keep the last drawn pose
    (dx, dy), (lx, ly), base_yaw = _DIRS[pose["dir"]]
    focus, dist, cam_x, cam_y, cam_z = pose["rack"], pose["dist"], pose["x"], pose["y"], pose["z"]
    along_half = b.half_extents(tuple(focus["dimensions"]), focus["yaw_deg"])[0 if dx != 0 else 1]
    pitch = b.u(*CAM_PITCH[L])
    jitter = YAW_JITTER[L]
    yaw = base_yaw + b.u(-jitter, jitter)
    fov = b.u(62.0, 78.0)
    camera = camera_dict((cam_x, cam_y, cam_z), _norm_yaw(yaw), pitch, fov, width, height)

    # ---- pallets: open ones in the corridor in front of the focus rack, then occluded ones ----
    pallets: list[dict] = []
    n_behind = BEHIND_PALLETS[L]
    n_open = N_PALLETS - n_behind

    def pallet_yaw_dims():
        yw = rng.choice((0.0, 90.0))
        hx, hy = b.half_extents(PALLET_DIMS, yw)
        return yw, hx, hy

    def place_in_view(hx, hy, margin, tries):
        """A free spot in the corridor between the camera and the focus rack, inside the view cone."""
        for _try in range(tries):
            t = b.u(2.0, max(2.5, dist - along_half - 1.2))
            lat_lim = min(2.5, 0.6 * t)
            lat = b.u(-lat_lim, lat_lim)
            cx = cam_x + dx * t + lx * lat
            cy = cam_y + dy * t + ly * lat
            if b.free(cx, cy, hx, hy, margin=margin):
                return cx, cy
        return None

    # Loose boxes and the forklift are "visible context": anywhere inside the view cone that is not in the
    # focus rack's perspective shadow. Stage 1 is the corridor above, stage 2 the whole cone.
    rack_lat = (focus["location"][0] - cam_x) * lx + (focus["location"][1] - cam_y) * ly  # lateral offset of focus rack
    rack_lat_half = b.half_extents(tuple(focus["dimensions"]), focus["yaw_deg"])[0 if lx != 0 else 1]
    rack_near = max(dist - along_half, 1.0)

    def place_in_cone(hx, hy, margin, tries):
        for _try in range(tries):
            t = b.u(1.8, 9.0)
            lat = b.u(-0.55 * t, 0.55 * t)
            if t > rack_near and abs(lat - rack_lat) < (rack_lat_half + 0.3) * t / rack_near + 0.3:
                continue  # hidden behind the focus rack
            cx = cam_x + dx * t + lx * lat
            cy = cam_y + dy * t + ly * lat
            if b.free(cx, cy, hx, hy, margin=margin):
                return cx, cy
        return None

    for _ in range(n_open):
        yw, hx, hy = pallet_yaw_dims()
        placed = place_in_view(hx, hy, 0.3, 30)
        if placed is None:
            placed = b.place_open(hx, hy, margin=0.3)
        if placed is None:
            continue
        pallets.append(b.add("pallet", PALLET_DIMS, (placed[0], placed[1], PALLET_DIMS[2] / 2.0), yw))

    for _ in range(n_behind):
        yw, hx, hy = pallet_yaw_dims()
        placed = None
        for _try in range(30):
            # half of the time hide behind another pallet (if any), otherwise behind the focus rack
            if pallets and rng.random() < 0.5:
                anchor = pallets[rng.randrange(len(pallets))]
                kind = "pallet"
            else:
                anchor = focus
                kind = "rack"
            a_hx, a_hy = b.half_extents(tuple(anchor["dimensions"]), anchor["yaw_deg"])
            ax, ay = anchor["location"][0], anchor["location"][1]
            gap = b.u(0.15, 0.6)
            if dx != 0:
                cx = ax + dx * (a_hx + gap + hx)
                cy = ay + b.u(-a_hy, a_hy)
            else:
                cx = ax + b.u(-a_hx, a_hx)
                cy = ay + dy * (a_hy + gap + hy)
            if b.free(cx, cy, hx, hy, margin=0.1):
                placed = (cx, cy, anchor["id"], kind)
                break
        if placed is None:
            continue
        pallets.append(
            b.add("pallet", PALLET_DIMS, (placed[0], placed[1], PALLET_DIMS[2] / 2.0), yw, behind=placed[2], kind=placed[3])
        )

    # ---- boxes stacked on pallets (axis-aligned pallets only, so slot rotation is exact) ----
    slots = ((-0.3, 0.0, None), (0.3, 0.0, None), (-0.3, 0.0, 0))  # (local x, local y, stacked-on slot)
    for pallet in list(pallets):
        n_boxes = rng.randint(0, 3)
        px, py = pallet["location"][0], pallet["location"][1]
        pyaw = pallet["yaw_deg"]
        tops: list[float] = []
        for k in range(n_boxes):
            sx, sy, sz = b.u(0.35, 0.55), b.u(0.35, 0.55), b.u(0.3, 0.6)
            lx_, ly_, under = slots[k]
            z = PALLET_DIMS[2] if under is None else tops[under]
            tops.append(z + sz)
            wx, wy = (lx_, ly_) if pyaw == 0.0 else (-ly_, lx_)
            b.add(
                "box",
                (sx, sy, sz),
                (px + wx, py + wy, z + sz / 2.0),
                pyaw + b.u(-8.0, 8.0),
                behind=pallet["occlusion"]["placed_behind"],
                kind=pallet["occlusion"]["kind"],
                footprint=False,
            )

    # ---- loose boxes on the floor ----
    for _ in range(rng.randint(4, 6)):
        sx, sy, sz = b.u(0.4, 0.8), b.u(0.4, 0.8), b.u(0.4, 0.8)
        yw = b.u(0.0, 360.0)
        hx, hy = b.half_extents((sx, sy, sz), yw)
        pos = place_in_view(hx, hy, 0.3, 10) or place_in_cone(hx, hy, 0.3, 30) or b.place_open(hx, hy, margin=0.3)
        if pos is not None:
            b.add("box", (sx, sy, sz), (pos[0], pos[1], sz / 2.0), yw)

    # ---- optional forklift ----
    has_forklift = rng.random() < 0.5
    fdims = (1.1, 2.4, 2.1)
    fyaw = b.u(0.0, 360.0)
    if has_forklift:
        hx, hy = b.half_extents(fdims, fyaw)
        pos = place_in_view(hx, hy, 0.5, 10) or place_in_cone(hx, hy, 0.5, 30) or b.place_open(hx, hy, margin=0.5)
        if pos is not None:
            b.add("forklift", fdims, (pos[0], pos[1], fdims[2] / 2.0), fyaw)

    sun_yaw = b.u(0.0, 360.0)
    sun_elev = b.u(35.0, 75.0)
    sun_energy = b.u(2.0, 4.0)

    return {
        "scene_type": SCENE_TYPE,
        "seed": seed,
        "frame_index": frame_index,
        "occlusion_level": L,
        "camera": camera,
        "lighting": {
            "sun": {"yaw_deg": sun_yaw, "elevation_deg": sun_elev, "energy": sun_energy},
            "ambient": 0.3,
        },
        "floor": {"size": [FLOOR_SIZE[0], FLOOR_SIZE[1]]},
        "objects": b.objects,
    }
