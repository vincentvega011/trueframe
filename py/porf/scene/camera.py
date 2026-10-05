"""Pinhole camera. Conventions are specified in docs/PROTOCOL.md section 13 and must
match what blender_job.py configures.

World: right-handed, +Z up, metres, floor at z = 0.
Camera pose = location + yaw_deg + pitch_deg (no roll). Blender equivalent:
  rotation_euler = (radians(90 - pitch_deg), 0, radians(yaw_deg)), mode 'XYZ'.
Blender cameras look down their local -Z with +Y up and +X right.
yaw 0 looks along +Y; positive yaw turns counter-clockwise seen from above (toward -X).
Positive pitch looks down.
fov_deg is the HORIZONTAL field of view (Blender sensor_fit = 'HORIZONTAL').
Image: origin at the top-left corner of the top-left pixel, u right, v DOWN, pixel (i, j)
covers [i, i+1) x [j, j+1); square pixels; principal point at the image centre.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..canon import quantize

NEAR = 0.1
FAR = 40.0
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

Vec3 = tuple[float, float, float]


def camera_dict(
    location: Vec3,
    yaw_deg: float,
    pitch_deg: float,
    fov_deg: float,
    width: int,
    height: int,
    near: float = NEAR,
    far: float = FAR,
) -> dict:
    """The camera block of a scene graph (all floats quantized)."""
    return {
        "location": [quantize(v) for v in location],
        "yaw_deg": quantize(yaw_deg),
        "pitch_deg": quantize(pitch_deg),
        "fov_deg": quantize(fov_deg),
        "fov_axis": "horizontal",
        "resolution": [int(width), int(height)],
        "near": quantize(near),
        "far": quantize(far),
    }


@dataclass(frozen=True)
class Camera:
    location: Vec3
    yaw_deg: float
    pitch_deg: float
    fov_deg: float
    width: int
    height: int
    near: float = NEAR
    far: float = FAR

    @classmethod
    def from_dict(cls, d: dict) -> "Camera":
        w, h = d["resolution"]
        return cls(
            location=tuple(d["location"]),  # type: ignore[arg-type]
            yaw_deg=d["yaw_deg"],
            pitch_deg=d["pitch_deg"],
            fov_deg=d["fov_deg"],
            width=w,
            height=h,
            near=d["near"],
            far=d["far"],
        )

    @property
    def focal_px(self) -> float:
        """Focal length in pixels (same for x and y: square pixels)."""
        return (self.width / 2.0) / math.tan(math.radians(self.fov_deg) / 2.0)

    @property
    def vertical_fov_deg(self) -> float:
        return math.degrees(2.0 * math.atan(math.tan(math.radians(self.fov_deg) / 2.0) * self.height / self.width))

    def basis(self) -> tuple[Vec3, Vec3, Vec3]:
        """World-space (right, up, forward) unit vectors of the camera."""
        y, p = math.radians(self.yaw_deg), math.radians(self.pitch_deg)
        sy, cy, sp, cp = math.sin(y), math.cos(y), math.sin(p), math.cos(p)
        right = (cy, sy, 0.0)
        up = (-sp * sy, sp * cy, cp)
        forward = (-cp * sy, cp * cy, -sp)
        return right, up, forward

    def world_to_camera(self, point: Vec3) -> Vec3:
        """Blender camera space: x right, y up, camera looks down -z (depth = -z)."""
        right, up, fwd = self.basis()
        d = tuple(point[i] - self.location[i] for i in range(3))
        dot = lambda a: a[0] * d[0] + a[1] * d[1] + a[2] * d[2]  # noqa: E731
        return (dot(right), dot(up), -dot(fwd))

    def project(self, point: Vec3) -> tuple[float, float, float]:
        """(u, v, depth) in pixels / metres. Only meaningful when depth > 0."""
        xc, yc, zc = self.world_to_camera(point)
        depth = -zc
        f = self.focal_px
        return (self.width / 2.0 + f * xc / depth, self.height / 2.0 - f * yc / depth, depth)
