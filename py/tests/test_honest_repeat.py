"""Reliability: ten honest 20-frame requests, different seeds across occlusion levels 0 to 3, production render settings, a real
localnet and the real verifier with Blender. Zero false positives is required. About 25 minutes, so it only runs when asked:

    PORF_HONEST_REPEAT=1 py/.venv/bin/pytest -q py/tests/test_honest_repeat.py -s

Writes out/honest_repeat.json (verdict, failing checks, timing per run)."""
import json
import os
from pathlib import Path

import pytest

import livenet
from porf.render.runner import find_blender
from test_verifier_flow import USDC, Scenario

RUNS = [(42, 0), (7, 1), (99, 2), (2024, 3), (31337, 0), (11, 1), (23, 2), (59, 3), (101, 0), (777, 1)]
pytestmark = [pytest.mark.slow, pytest.mark.needs_validator, pytest.mark.skipif(find_blender() is None, reason="Blender not installed"),
              pytest.mark.skipif(os.environ.get("PORF_HONEST_REPEAT") != "1", reason="set PORF_HONEST_REPEAT=1 (about 25 minutes)")]
RESULTS = Path(__file__).resolve().parents[2] / "out" / "honest_repeat.json"


@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


@pytest.fixture(scope="module")
def results():
    out = []
    yield out
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(out, indent=2))


@pytest.mark.parametrize("seed,level", RUNS, ids=[f"seed{s}-occ{o}" for s, o in RUNS])
def test_an_honest_node_is_never_flagged(net, tmp_path, results, seed, level):
    s = Scenario(net, tmp_path, f"honest-{seed}-{level}", 20, seed=seed, occlusion=level)
    try:
        s.honest_commit()
        s.verifier().run_until_settled(timeout=400)
        rep = s.report()
        record = {"seed": seed, "occlusion": level, "verdict": rep["verdict"], "failing_checks": sorted(s.failing_checks(rep)), "reasons": rep["verdict_reasons"],
                  "sampled": rep["sample"]["indices"], "timing": rep["timing"], "outcome": rep["outcome"]["action"]}
        results.append(record)
        print("\nHONEST_RUN " + json.dumps(record, sort_keys=True))
        assert rep["verdict"] == "Pass" and rep["verdict_reasons"] == [], record
        assert rep["outcome"]["action"] == "paid"
        assert s.balances() == {"requester": s.before["requester"] - s.escrow, "node": s.before["node"] + s.escrow}
    finally:
        s.close()
