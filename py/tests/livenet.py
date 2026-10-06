"""Shared helper for tests that talk to a running localnet (scripts/localnet_up.sh)."""
import json
import time
import urllib.request
from types import SimpleNamespace

import pytest

from porf.chain import client as chain_mod
from porf.chain.client import REPO_ROOT, ChainClient

IDL = REPO_ROOT / "target" / "idl" / "proof_of_render.json"
ENV_FILE = REPO_ROOT / ".localnet" / "env.json"
USDC = 1_000_000


def rpc_healthy(url: str) -> bool:
    try:
        req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getHealth"}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.load(r).get("result") == "ok"
    except Exception:
        return False


def make_net() -> SimpleNamespace:
    """Skips the calling test/fixture with a reason unless a localnet is up and the CLI is installed."""
    if not ENV_FILE.exists():
        pytest.skip("no localnet: run scripts/localnet_up.sh")
    env = json.loads(ENV_FILE.read_text())
    if not rpc_healthy(env["rpc_url"]):
        pytest.skip(f"validator not reachable at {env['rpc_url']}: run scripts/localnet_up.sh")
    if not chain_mod._TSX.exists():
        pytest.skip("CLI dependencies not installed (npm ci in py/porf/chain/cli)")
    if not IDL.exists():
        pytest.skip("IDL missing: run anchor build")
    idl_errors = {e["name"]: e["code"] for e in json.loads(IDL.read_text())["errors"]}
    return SimpleNamespace(client=ChainClient(rpc_url=env["rpc_url"]), env=env, idl_errors=idl_errors, ids=iter(range(int(time.time() * 1000), 2**62)))
