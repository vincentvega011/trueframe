"""Cheat modes for the demo. Isolated here so it is obviously test tooling.

NOT IMPLEMENTED YET: the node calls these two hooks at the right points, and today both do nothing, so
`--cheat <mode>` runs an honest node (and says so loudly). Day 5 fills them in:
  before_render(mode, seed)                 -> the seed the node will render from      (wrong_seed)
  after_render(mode, job_dir, num_frames)   -> may rewrite files before they are hashed (reuse_frames, fake_boxes, partial)
The leaves are built after after_render, so the committed root covers whatever the cheat left on disk.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("porf.node.cheats")

CHEAT_MODES = ("reuse_frames", "fake_boxes", "wrong_seed", "partial")


def announce(mode: str | None) -> None:
    if mode:
        log.warning("--cheat %s requested, but cheat modes are not implemented yet: this node behaves HONESTLY", mode)


def before_render(mode: str | None, seed: int) -> int:
    return seed  # no-op


def after_render(mode: str | None, job_dir: Path, num_frames: int) -> None:
    return None  # no-op
