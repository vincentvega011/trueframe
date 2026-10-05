"""Full-object 2D boxes from a scene-graph object's 3D box, via the pinhole camera.

The box is the pixel bounding rectangle of the object's oriented 3D box (8 corners), with
edges clipped to the camera near plane first, then clipped to the image. It is the *full*
object extent, ignoring occlusion; node-reported boxes (from visible mask extents) must
lie inside it. `silhouette_area` gives the visible-if-unoccluded area used for visibility.
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


def _screen_points(camera: Camera, obj: dict) -> tuple[list[tuple[float, float]], bool]:
    """Pixel positions of the near-plane-clipped box edges, and whether the near plane cut it."""
    cam_pts = [camera.world_to_camera(p) for p in object_corners(obj)]
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
    return pixels, any(-p[2] < near for p in cam_pts)


def project_box(camera: Camera, obj: dict) -> Projection:
    pixels, truncated = _screen_points(camera, obj)
    if not pixels:
        return Projection(BEHIND_CAMERA, None, True)

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


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _clip_polygon_to_rect(poly, w: float, h: float):
    def clip(points, inside, intersect):
        out = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(intersect(prev, cur))
        return out

    def x_at(a, b, x):
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + t * (b[1] - a[1]))

    def y_at(a, b, y):
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), y)

    poly = clip(poly, lambda p: p[0] >= 0.0, lambda a, b: x_at(a, b, 0.0)) if poly else poly
    poly = clip(poly, lambda p: p[0] <= w, lambda a, b: x_at(a, b, w)) if poly else poly
    poly = clip(poly, lambda p: p[1] >= 0.0, lambda a, b: y_at(a, b, 0.0)) if poly else poly
    poly = clip(poly, lambda p: p[1] <= h, lambda a, b: y_at(a, b, h)) if poly else poly
    return poly


def silhouette_area(camera: Camera, obj: dict) -> float:
    """Pixel area, inside the image, of the convex silhouette of the object's box.

    This is the most the object could cover if nothing hid it. Returns 0.0 if the box is
    behind the camera or off-screen.
    """
    pixels, _ = _screen_points(camera, obj)
    if len(pixels) < 3:
        return 0.0
    hull = _convex_hull(pixels)
    if len(hull) < 3:
        return 0.0
    poly = _clip_polygon_to_rect(hull, float(camera.width), float(camera.height))
    if len(poly) < 3:
        return 0.0
    area = 0.0
    for i, (x1, y1) in enumerate(poly):
        x2, y2 = poly[(i + 1) % len(poly)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0
