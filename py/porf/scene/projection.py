"""Full-object 2D boxes from a scene-graph object's 3D box, via the pinhole camera.

The box is the pixel bounding rectangle of the object's oriented 3D box (8 corners), with
edges clipped to the camera near plane first, then clipped to the image. It is the *full*
object extent, ignoring occlusion; node-reported boxes (from visible mask extents) must
lie inside it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .camera import Camera

# Projection.status values
OK = "ok"
BEHIND_CAMERA = "behind_camera"      # entire box is nearer than the near plane
OUTSIDE_IMAGE = "outside_image"      # in front of the camera but entirely off-screen

_EDGES = (
    (0, 1), (2, 3), (4, 5), (6, 7),  # along local x
    (0, 2), (1, 3), (4, 6), (5, 7),  # along local y
    (0, 4), (1, 5), (2, 6), (3, 7),  # along local z
)


@dataclass(frozen=True)
class Projection:
    status: str
    box: tuple[float, float, float, float] | None  # (x0, y0, x1, y1) in pixels, clipped to image
    truncated: bool  # box was cut by the near plane or the image border


def object_corners(obj: dict) -> list[tuple[float, float, float]]:
    """World-space corners of an object's box: dimensions centred at location, yawed about Z."""
    sx, sy, sz = obj["dimensions"]
    cx, cy, cz = obj["location"]
    yaw = math.radians(obj["yaw_deg"])
    c, s = math.cos(yaw), math.sin(yaw)
    out = []
    for i in range(8):
        lx = (sx / 2.0) * (1 if i & 1 else -1)
        ly = (sy / 2.0) * (1 if i & 2 else -1)
        lz = (sz / 2.0) * (1 if i & 4 else -1)
        out.append((cx + c * lx - s * ly, cy + s * lx + c * ly, cz + lz))
    return out


def project_box(camera: Camera, obj: dict) -> Projection:
    corners = object_corners(obj)
    cam_pts = [camera.world_to_camera(p) for p in corners]
    near = camera.near
    f = camera.focal_px
    cx0, cy0 = camera.width / 2.0, camera.height / 2.0

    def to_pixel(pt):
        depth = -pt[2]
        return (cx0 + f * pt[0] / depth, cy0 - f * pt[1] / depth)

    pixels = []
    for a, b in _EDGES:
        pa, pb = cam_pts[a], cam_pts[b]
        da, db = -pa[2], -pb[2]
        if da < near and db < near:
            continue
        if da < near or db < near:
            t = (near - da) / (db - da)
            mid = tuple(pa[k] + t * (pb[k] - pa[k]) for k in range(3))
            if da < near:
                pa = mid
            else:
                pb = mid
        pixels.append(to_pixel(pa))
        pixels.append(to_pixel(pb))

    if not pixels:
        return Projection(BEHIND_CAMERA, None, True)

    truncated = any(-p[2] < near for p in cam_pts)
    x0 = min(p[0] for p in pixels)
    x1 = max(p[0] for p in pixels)
    y0 = min(p[1] for p in pixels)
    y1 = max(p[1] for p in pixels)
    cx0_, cy0_ = max(0.0, x0), max(0.0, y0)
    cx1_, cy1_ = min(float(camera.width), x1), min(float(camera.height), y1)
    if (cx0_, cy0_, cx1_, cy1_) != (x0, y0, x1, y1):
        truncated = True
    if cx1_ <= cx0_ or cy1_ <= cy0_:
        return Projection(OUTSIDE_IMAGE, None, truncated)
    return Projection(OK, (cx0_, cy0_, cx1_, cy1_), truncated)
