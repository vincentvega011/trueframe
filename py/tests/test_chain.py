"""Chain client tests. The first group needs nothing running. Tests marked `needs_validator` run against a
live localnet started with scripts/localnet_up.sh and skip cleanly when it is not reachable."""
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from porf.chain import client as chain_mod
import livenet
from porf.chain.client import REPO_ROOT, ChainClient, ChainError, Config, CreateRequestParams, _build, _prep

IDL = REPO_ROOT / "target" / "idl" / "proof_of_render.json"
ENV_FILE = REPO_ROOT / ".localnet" / "env.json"
USDC = 1_000_000


# ============================================================ no validator needed

def test_prep_makes_ints_json_safe_without_losing_u64():
    out = _prep({"seed": 2**63 + 5, "flag": True, "none": None, "nested": [1, {"x": 2}], "path": Path("keys/a.json"), "root": b"\x01\xff"})
    assert out == {"seed": "9223372036854775813", "flag": True, "none": None, "nested": ["1", {"x": "2"}], "path": "keys/a.json", "root": "01ff"}
    params = CreateRequestParams(request_id=1, seed=2**64 - 1, num_frames=10, price_per_frame=5, claim_duration=1, commit_duration=2, challenge_window_secs=3)
    assert _prep(params)["seed"] == "18446744073709551615"


def test_views_are_strict_about_fields():
    good = {"admin": "a", "verifier": "v", "mint": "m", "stake_bps": "2000", "bump": 254}
    assert _build(Config, good) == Config("a", "v", "m", 2000, 254)
    with pytest.raises(ChainError) as e:
        _build(Config, {**good, "surprise": 1})
    assert e.value.name == "SchemaMismatch"
    with pytest.raises(ChainError):
        _build(Config, {k: v for k, v in good.items() if k != "bump"})


def _fake_run(stdout: str, returncode: int, monkeypatch):
    monkeypatch.setattr(chain_mod, "_find_node", lambda: "node")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout, ""))


@pytest.mark.skipif(not chain_mod._TSX.exists(), reason="CLI dependencies not installed (npm ci in py/porf/chain/cli)")
def test_cli_error_json_becomes_chainerror(monkeypatch):
    err = {"ok": False, "error": {"name": "InvalidStatus", "code": 6012, "message": "wrong status", "logs": ["a", "b"], "kind": "program"}}
    _fake_run(json.dumps(err) + "\n", 1, monkeypatch)
    with pytest.raises(ChainError) as e:
        ChainClient().call("get-config")
    assert (e.value.name, e.value.code, e.value.logs, e.value.kind) == ("InvalidStatus", 6012, ["a", "b"], "program")


@pytest.mark.skipif(not chain_mod._TSX.exists(), reason="CLI dependencies not installed")
def test_cli_crash_without_json_is_reported(monkeypatch):
    _fake_run("not json at all", 1, monkeypatch)
    with pytest.raises(ChainError) as e:
        ChainClient().call("get-config")
    assert e.value.name == "CliCrash"


def _scripted_call(monkeypatch, failures: int, error: ChainError | None = None):
    c, calls, sleeps = ChainClient(), [], []

    def fake_call(command, args=None):
        calls.append((command, args))
        if len(calls) <= failures:
            raise error or ChainError("SlotHashNotNewer", "slot hashes are stale", code=7, logs=["log"], kind="program")
        return {"signature": "sig"}

    monkeypatch.setattr(c, "call", fake_call)
    monkeypatch.setattr(chain_mod.time, "sleep", sleeps.append)
    return c, calls, sleeps


def test_open_challenge_retries_slot_hash_not_newer_and_then_succeeds(monkeypatch):
    c, calls, sleeps = _scripted_call(monkeypatch, failures=2)
    assert c.open_challenge("keys/verifier.json", request="Req", max_attempts=20, delay=0.4) == {"signature": "sig"}
    assert len(calls) == 3 and sleeps == [0.4, 0.4]
    assert calls[0][0] == "open-challenge" and calls[0][1]["request"] == "Req"


def test_open_challenge_retry_is_bounded_and_fails_with_a_clear_message(monkeypatch):
    c, calls, sleeps = _scripted_call(monkeypatch, failures=10**6)
    with pytest.raises(ChainError) as e:
        c.open_challenge("keys/verifier.json", request="Req", max_attempts=20, delay=0.4)
    assert len(calls) == 20 and len(sleeps) == 19 and set(sleeps) == {0.4}   # bounded: 20 tries, 19 waits
    assert e.value.name == "SlotHashNotNewer" and e.value.code == 7 and e.value.logs == ["log"]
    assert "after 20 attempts" in e.value.message and "0.4s apart" in e.value.message and "validator" in e.value.message


def test_open_challenge_does_not_retry_other_errors_or_by_default(monkeypatch):
    c, calls, sleeps = _scripted_call(monkeypatch, failures=5, error=ChainError("UnauthorizedVerifier", "nope", kind="program"))
    with pytest.raises(ChainError) as e:
        c.open_challenge("keys/node.json", request="Req", max_attempts=20)
    assert (e.value.name, len(calls), sleeps) == ("UnauthorizedVerifier", 1, [])

    c, calls, sleeps = _scripted_call(monkeypatch, failures=5)   # default: a single attempt, original error, no sleeping
    with pytest.raises(ChainError) as e:
        c.open_challenge("keys/verifier.json", request="Req")
    assert (e.value.name, e.value.message, len(calls), sleeps) == ("SlotHashNotNewer", "slot hashes are stale", 1, [])


def test_no_error_numbers_are_hardcoded_in_the_chain_code():
    """Error names must come from the IDL / Anchor logs at runtime, never from a literal number."""
    files = list((chain_mod.CLI_DIR / "src").glob("*.ts")) + [Path(chain_mod.__file__)]
    offenders = []
    for f in files:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\b6[0-9]{3}\b", line) and not line.lstrip().startswith(("#", "//")):
                offenders.append(f"{f.name}:{n}: {line.strip()}")
    assert not offenders, offenders


@pytest.mark.skipif(not chain_mod._TSX.exists() or not IDL.exists(), reason="CLI dependencies or IDL missing")
def test_error_names_are_read_from_the_idl_at_runtime(tmp_path):
    """No validator needed: a bare `custom program error: 0x..` is resolved through whatever IDL is loaded,
    so renaming an error in the IDL renames it in the output; the name is not baked into the CLI."""
    idl = json.loads(IDL.read_text())
    target = next(e for e in idl["errors"] if e["name"] == "WrongMint")
    helper = chain_mod.CLI_DIR / "test" / "map_error.ts"

    def run(idl_path: Path, message: str) -> dict:
        env = dict(__import__("os").environ, PORF_IDL=str(idl_path))
        out = subprocess.run([chain_mod._find_node(), str(chain_mod._TSX), str(helper), message], capture_output=True, text=True, env=env, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    message = f"failed: custom program error: {hex(target['code'])}"
    real = run(IDL, message)
    assert (real["name"], real["code"], real["kind"]) == ("WrongMint", target["code"], "program")
    assert real["message"] == target["msg"]

    renamed = json.loads(IDL.read_text())
    next(e for e in renamed["errors"] if e["code"] == target["code"]).update(name="RenamedForTest", msg="renamed message")
    alt = tmp_path / "idl.json"
    alt.write_text(json.dumps(renamed))
    after = run(alt, message)
    assert (after["name"], after["message"]) == ("RenamedForTest", "renamed message")

    # a code that is in neither the IDL nor Anchor's built-in table is reported, not guessed
    unknown = run(IDL, "custom program error: 0xfffff")
    assert unknown["name"] == "UnknownProgramError"
    # an Anchor built-in (not in the program's IDL) is named from Anchor's own table
    builtin_code = int(subprocess.run([chain_mod._find_node(), "-e", "console.log(require('@anchor-lang/core').LangErrorCode.ConstraintTokenOwner)"],
                                      capture_output=True, text=True, cwd=chain_mod.CLI_DIR, check=True).stdout)
    from_lib = run(IDL, f"custom program error: {hex(builtin_code)}")
    assert from_lib["name"] == "ConstraintTokenOwner"


# ============================================================ against a live localnet

@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


def _params(request_id: int, **over) -> CreateRequestParams:
    base = dict(request_id=request_id, seed=42, num_frames=10, price_per_frame=USDC, claim_duration=300, commit_duration=600,
                challenge_window_secs=30, occlusion_level=3)
    return CreateRequestParams(**{**base, **over})


def _claimed(net) -> tuple[int, str]:
    """A fresh request created by the requester and claimed by the node. Returns (request_id, request address)."""
    c, e = net.client, net.env
    rid = next(net.ids)
    out = c.create_request(e["keys"]["requester"], _params(rid))
    c.stake_and_claim(e["keys"]["node"], request=out["request"])
    return rid, out["request"]


@pytest.mark.needs_validator
def test_config_matches_the_localnet_setup(net):
    cfg = net.client.get_config()
    assert cfg.admin == net.env["pubkeys"]["admin"]
    assert cfg.verifier == net.env["pubkeys"]["verifier"]
    assert cfg.mint == net.env["mint"]
    assert cfg.stake_bps == net.env["stake_bps"]
    info = net.client.info()
    assert info["program_id"] == net.env["program_id"]
    assert "submit_commitment" in info["instructions"]


@pytest.mark.needs_validator
def test_create_fetch_claim_commit_flow(net):
    c, e = net.client, net.env
    requester, node = e["pubkeys"]["requester"], e["pubkeys"]["node"]
    req_tok, node_tok = e["token_accounts"]["requester"], e["token_accounts"]["node"]
    rid = next(net.ids)
    escrow, stake = 10 * USDC, 2 * USDC
    req_before, node_before = c.token_balance(token_account=req_tok), c.token_balance(token_account=node_tok)

    # ---- create, then fetch by (requester, request_id)
    t0 = int(time.time())
    created = c.create_request(e["keys"]["requester"], _params(rid))
    assert created["signature"] and created["request"]
    view = c.get_request(requester=requester, request_id=rid)
    r = view.request
    assert view.address == created["request"] == c.get_request(request=created["request"]).address
    assert (view.escrow, view.stake_vault) == (created["escrow"], created["stake_vault"])
    assert (r.requester, r.request_id, r.mint) == (requester, rid, e["mint"])
    assert (r.scene_type, r.seed, r.num_frames, r.width, r.height, r.occlusion_level) == (0, 42, 10, 640, 480, 3)
    assert (r.price_per_frame, r.escrow_amount, r.stake_amount) == (USDC, escrow, stake)
    assert (r.claim_duration, r.commit_duration, r.challenge_window_secs) == (300, 600, 30)
    assert r.status == "Open" and r.node == "11111111111111111111111111111111"
    assert t0 - 60 <= r.created_at <= int(time.time()) + 60
    assert r.claim_deadline == r.created_at + 300
    assert (r.claimed_at, r.commit_deadline, r.challenge_window_end) == (0, 0, 0)
    assert (view.escrow_balance, view.stake_vault_balance) == (escrow, 0)
    assert c.token_balance(token_account=req_tok) == req_before - escrow

    open_now = [addr for addr, _ in c.list_requests(status="Open", requester=requester)]
    assert view.address in open_now
    assert view.address not in [addr for addr, _ in c.list_requests(status="Claimed")]

    # ---- claim, then fetch again
    claim = c.stake_and_claim(e["keys"]["node"], requester=requester, request_id=rid)
    assert claim["signature"]
    view = c.get_request(requester=requester, request_id=rid)
    r2 = view.request
    assert r2.status == "Claimed" and r2.node == node
    assert r2.claimed_at >= r.created_at and r2.commit_deadline == r2.claimed_at + 600
    assert (r2.escrow_amount, r2.stake_amount, r2.claim_deadline) == (escrow, stake, r.claim_deadline)  # untouched by the claim
    assert (view.escrow_balance, view.stake_vault_balance) == (escrow, stake)
    assert c.token_balance(token_account=node_tok) == node_before - stake
    assert c.token_balance(token_account=req_tok) == req_before - escrow
    assert view.address in [addr for addr, _ in c.list_requests(status="Claimed")]
    assert view.address not in [addr for addr, _ in c.list_requests(status="Open")]

    # ---- no submission yet, then commit and read it back
    with pytest.raises(ChainError) as no_sub:
        c.get_submission(request=view.address)
    assert no_sub.value.name == "AccountNotFound"

    root = hashlib.sha256(b"proof-of-render test root").digest()
    commit = c.submit_commitment(e["keys"]["node"], root, 10, request=view.address)
    assert commit["submission"] == view.submission
    sub = c.get_submission(request=view.address)
    assert (sub.request, sub.node) == (view.address, node)
    assert (sub.merkle_root, sub.frame_count) == (root.hex(), 10)
    assert sub.commit_slot > 0
    assert (sub.sample_seed, sub.sample_slot, sub.sample_count, sub.verdict) == ("00" * 32, 0, 0, "Pending")
    assert c.get_request(request=view.address).request.status == "Committed"


@pytest.mark.needs_validator
def test_program_errors_carry_idl_names_and_codes(net):
    c, e = net.client, net.env
    node_key = e["keys"]["node"]
    _, request = _claimed(net)

    # double claim -> InvalidStatus; the code must equal the IDL's number for that name
    with pytest.raises(ChainError) as err:
        c.stake_and_claim(node_key, request=request)
    assert err.value.name == "InvalidStatus" and err.value.kind == "program"
    assert err.value.code == net.idl_errors["InvalidStatus"]
    assert err.value.logs, "program logs are attached"

    # commit with the wrong frame count, then with a zero root
    with pytest.raises(ChainError) as err:
        c.submit_commitment(node_key, hashlib.sha256(b"x").digest(), 9, request=request)
    assert (err.value.name, err.value.code) == ("FrameCountMismatch", net.idl_errors["FrameCountMismatch"])
    with pytest.raises(ChainError) as err:
        c.submit_commitment(node_key, b"\x00" * 32, 10, request=request)
    assert (err.value.name, err.value.code) == ("ZeroMerkleRoot", net.idl_errors["ZeroMerkleRoot"])

    # a commit by someone who did not claim
    with pytest.raises(ChainError) as err:
        c.submit_commitment(e["keys"]["requester"], hashlib.sha256(b"x").digest(), 10, request=request)
    assert err.value.name == "UnauthorizedNode"

    # double commit: Anchor's `init` fails at account creation, so this is a system-level failure, not InvalidStatus
    c.submit_commitment(node_key, hashlib.sha256(b"first").digest(), 10, request=request)
    with pytest.raises(ChainError) as err:
        c.submit_commitment(node_key, hashlib.sha256(b"second").digest(), 10, request=request)
    assert "already in use" in " ".join(err.value.logs)
    assert c.get_submission(request=request).merkle_root == hashlib.sha256(b"first").hexdigest()

    # wrong mint account -> WrongMint (custom); someone else's token account -> ConstraintTokenOwner (Anchor built-in)
    other_mint = c.create_mint(e["keys"]["admin"])
    other_tok = c.create_token_account(e["keys"]["requester"], other_mint)
    with pytest.raises(ChainError) as err:
        c.create_request(e["keys"]["requester"], _params(next(net.ids)), mint=other_mint, requester_token=other_tok)
    assert (err.value.name, err.value.code) == ("WrongMint", net.idl_errors["WrongMint"])
    with pytest.raises(ChainError) as err:
        c.create_request(e["keys"]["requester"], _params(next(net.ids)), requester_token=e["token_accounts"]["node"])
    assert err.value.name == "ConstraintTokenOwner"

    # parameter validation names
    for over, name in ((dict(num_frames=0), "ZeroFrames"), (dict(num_frames=1025), "TooManyFrames"), (dict(width=4097), "ResolutionTooLarge"),
                       (dict(price_per_frame=2**64 - 1, num_frames=2), "MathOverflow"), (dict(occlusion_level=4), "InvalidOcclusionLevel")):
        with pytest.raises(ChainError) as err:
            c.create_request(e["keys"]["requester"], _params(next(net.ids), **over))
        assert err.value.name == name and err.value.code == net.idl_errors[name], over


@pytest.mark.needs_validator
def test_non_program_failures_are_structured(net):
    c = net.client
    with pytest.raises(ChainError) as err:
        c.get_request(requester=net.env["pubkeys"]["requester"], request_id=1)  # never created
    assert err.value.name == "AccountNotFound"

    with pytest.raises(ChainError) as err:
        c.get_request()  # neither a request address nor requester+id
    assert err.value.kind == "input"

    with pytest.raises(ChainError) as err:
        c.call("create-request", {"signer": net.env["keys"]["requester"], "params": {"request_id": 1}})  # missing fields
    assert (err.value.name, err.value.kind) == ("InvalidInput", "input")

    with pytest.raises(ChainError) as err:
        c.call("no-such-command")
    assert err.value.kind == "input"

    with pytest.raises(ChainError) as err:
        c.call("get-request", {"request": "not-a-pubkey"})
    assert err.value.name == "InvalidInput"

    with pytest.raises(ChainError) as err:
        c.call("stake-and-claim", {"signer": "keys/does_not_exist.json", "request": net.env["pubkeys"]["admin"]})
    assert err.value.name == "KeypairNotFound"

    with pytest.raises(ChainError) as err:
        ChainClient(rpc_url="http://127.0.0.1:1").get_config()  # nothing listening
    assert err.value.name == "RpcError" and err.value.kind == "rpc"


def test_the_demo_challenge_window_defaults_to_60_seconds():
    """3 x the worst measured loaded verification (16.9 s) is 51 s; rounded up to 60 s for screen-recording load (PROTOCOL.md 17.6)."""
    from porf.chain.client import DEFAULT_CHALLENGE_WINDOW_SECS
    p = CreateRequestParams(request_id=1, seed=1, num_frames=1, price_per_frame=1, claim_duration=1, commit_duration=1)
    assert DEFAULT_CHALLENGE_WINDOW_SECS == 60 and p.challenge_window_secs == 60
    assert CreateRequestParams(request_id=1, seed=1, num_frames=1, price_per_frame=1, claim_duration=1, commit_duration=1,
                               challenge_window_secs=6).challenge_window_secs == 6
