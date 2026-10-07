"""Verifier report: per-frame results, the verdict with reasons, and what was moved on chain.

Written to <out>/<request address>/report.json (machine-readable) and report.txt (short human summary).
Schema: docs/PROTOCOL.md section 17.3.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import CheckResult

CHECKED, UNAVAILABLE, NOT_CHECKED = "checked", "reveal_unavailable", "not_checked"


@dataclass
class FrameResult:
    index: int
    status: str
    checks: list[CheckResult] = field(default_factory=list)
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == CHECKED and bool(self.checks) and all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"index": self.index, "status": self.status, "passed": self.passed, "checks": [c.to_dict() for c in self.checks]}
        if self.reason:
            d["reason"] = self.reason
        return d


def decide(frames: list[FrameResult]) -> tuple[str, list[str]]:
    """Pass only if every sampled frame was fetched and passed every check. Returns the verdict and one reason per failure.
    An unservable reveal is reported as "reveal unavailable", never as a check failure."""
    reasons: list[str] = []
    for f in frames:
        if f.status == UNAVAILABLE:
            reasons.append(f"frame {f.index}: reveal unavailable ({f.reason})")
        elif f.status == NOT_CHECKED:
            continue  # only produced after an unavailable reveal, which already decided the verdict
        else:
            reasons += [f"frame {f.index}: {c.name} failed: {c.detail}" for c in f.checks if not c.passed]
    if not reasons and not any(f.status == CHECKED for f in frames):
        reasons.append("no sampled frame was checked")   # an empty sample must never pass
    return ("Fail" if reasons else "Pass"), reasons


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def summarize(report: dict[str, Any]) -> str:
    s = report.get("sample", {})
    lines = [
        f"Request {report['request']}  (request_id {report.get('request_id')}, node {report.get('node')})",
        f"Sample: {s.get('count')} of {report.get('frame_count')} frames {s.get('indices')}  seed {str(s.get('seed'))[:16]}...  slot {s.get('slot')}",
        f"Reveal URL: {report.get('reveal_url')}",
        "",
    ]
    for f in report.get("frames", []):
        mark = "PASS" if f["passed"] else ("UNAVAILABLE" if f["status"] == UNAVAILABLE else "FAIL" if f["status"] == CHECKED else "SKIPPED")
        lines.append(f"frame {f['index']:>4}: {mark}")
        for c in f["checks"]:
            lines.append(f"    [{'ok' if c['passed'] else 'FAIL'}] {c['name']}: {c['detail']}")
        if f.get("reason"):
            lines.append(f"    {f['reason']}")
    lines += ["", f"VERDICT: {report.get('verdict')}"]
    lines += [f"  - {r}" for r in report.get("verdict_reasons", [])]
    t = report.get("transactions", {})
    lines.append("Transactions: " + ", ".join(f"{k} {str(v)[:16]}..." for k, v in t.items() if v) if t else "Transactions: none yet")
    o = report.get("outcome")
    if o:
        lines.append(f"Outcome: {o['action']} -> {o['to']}: escrow {o['escrow_moved']} + stake {o['stake_moved']} "
                     f"(destination balance {o['destination_delta']:+d}); request is now {o['final_status']}")
    if report.get("thresholds", {}).get("calibrated") is False:
        lines.append("Note: annotation thresholds are PLACEHOLDERS (not yet calibrated).")
    return "\n".join(lines) + "\n"


def write_report(directory: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    report["updated_at"] = time.time()
    json_path, txt_path = directory / "report.json", directory / "report.txt"
    _atomic_write(json_path, json.dumps(report, indent=2, sort_keys=True))
    _atomic_write(txt_path, summarize(report))
    return json_path, txt_path


def load_report(directory: Path) -> dict[str, Any] | None:
    path = directory / "report.json"
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (json.JSONDecodeError, OSError):
        return None
