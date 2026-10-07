"""End to end against a live localnet: commit -> open_challenge -> verdict -> payout or slash, through the real TS CLI and
the real program, with exact token balances. Skips cleanly without a validator. Uses real wall-clock challenge windows."""
import base64
import hashlib
import json
import time
import urllib.request

import pytest

import livenet
from porf.chain.client import ChainError, CreateRequestParams

USDC = 1_000_000
ESCROW, STAKE = 10 * USDC, 2 * USDC     # 10 frames x 1 USDC; stake_bps 2000 from localnet_up.sh
WINDOW = 6                               # seconds; generous, because each CLI call takes ~1 s and more on a busy machine
SLOT_HASHES = "SysvarS1otHashes111111111111111111111111111"
pytestmark = pytest.mark.needs_validator


@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


def slot_hashes(rpc_url: str) -> dict[int, bytes]:
    """The real SlotHashes sysvar account, parsed independently of the program: u64 count, then (u64 slot, 32-byte hash) entries.
    Read at `confirmed` commitment: the default (finalized) view lags ~32 slots and would not contain a freshly used slot."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [SLOT_HASHES, {"encoding": "base64", "commitment": "confirmed"}]}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = base64.b64decode(json.load(r)["result"]["value"]["data"][0])
    count = int.from_bytes(data[:8], "little")
    return {int.from_bytes(data[8 + 40 * i: 16 + 40 * i], "little"): data[16 + 40 * i: 48 + 40 * i] for i in range(count)}


class Flow:
    """One request on the live chain, driven step by step."""

    def __init__(self, net, root_label: str):
        self.net, self.c, self.e = net, net.client, net.env
        self.keys = self.e["keys"]
        self.root = hashlib.sha256(root_label.encode()).digest()
        self.req_tok, self.node_tok = self.e["token_accounts"]["requester"], self.e["token_accounts"]["node"]
        self.before = self.balances()
        rid = next(net.ids)
        params = CreateRequestParams(request_id=rid, seed=42, num_frames=10, price_per_frame=USDC, claim_duration=600,
                                     commit_duration=900, challenge_window_secs=WINDOW, occlusion_level=3)
        self.address = self.c.create_request(self.keys["requester"], params)["request"]
        self.c.stake_and_claim(self.keys["node"], request=self.address)
        self.c.submit_commitment(self.keys["node"], self.root, 10, request=self.address)

    def balances(self) -> dict:
        return {"requester": self.c.token_balance(token_account=self.req_tok), "node": self.c.token_balance(token_account=self.node_tok)}

    def view(self):
        return self.c.get_request(request=self.address)

    def open(self):
        return self.c.open_challenge(self.keys["verifier"], request=self.address, max_attempts=20, delay=0.4)

    def verdict(self, v: str, key: str = "verifier"):
        return self.c.submit_verdict(self.keys[key], v, request=self.address)

    def payout(self):
        return self.c.finalize_payout(self.keys["admin"], request=self.address)   # a stranger signs

    def slash(self):
        return self.c.slash_and_refund(self.keys["admin"], request=self.address)

    def wait_for_window_end(self):
        end = self.view().request.challenge_window_end
        deadline = time.time() + 30
        while time.time() < end + 1 and time.time() < deadline:
            time.sleep(0.25)

    def payout_when_window_closed(self, attempts: int = 15, delay: float = 0.5):
        """The window is judged by the chain's clock, which can trail ours by a moment: retry ONLY ChallengeWindowOpen, boundedly."""
        for n in range(attempts):
            try:
                return self.payout()
            except ChainError as e:
                if e.name != "ChallengeWindowOpen" or n == attempts - 1:
                    raise
                time.sleep(delay)


def err(net, name: str):
    """pytest.raises that also checks the error's code equals the IDL's number for that name."""
    class Ctx:
        def __enter__(self):
            self.cm = pytest.raises(ChainError)
            self.info = self.cm.__enter__()
            return self.info

        def __exit__(self, *a):
            ok = self.cm.__exit__(*a)
            if ok:
                assert self.info.value.name == name, f"expected {name}, got {self.info.value.name}: {self.info.value.message}"
                assert self.info.value.code == net.idl_errors[name], (name, self.info.value.code)
            return ok
    return Ctx()


def test_honest_flow_open_challenge_pass_payout_with_exact_balances(net):
    f = Flow(net, "honest")
    assert f.balances() == {"requester": f.before["requester"] - ESCROW, "node": f.before["node"] - STAKE}   # money is locked

    f.open()
    f.verdict("Pass")                                                     # time-critical steps first: they must land inside the window
    with err(net, "ChallengeWindowOpen"):
        f.payout()                                                        # too early, even with a Pass verdict

    sub = f.c.get_submission(request=f.address)
    assert sub.sample_slot > sub.commit_slot and sub.sample_count == 7 and sub.verdict == "Pass"
    # audit the seed independently: sha256(root || the SlotHashes entry for sample_slot), read straight from the chain
    entries = slot_hashes(f.e["rpc_url"])
    assert sub.sample_slot in entries, "sample_slot should still be in the 512-slot window"
    assert hashlib.sha256(f.root + entries[sub.sample_slot]).hexdigest() == sub.sample_seed
    view = f.view()
    assert view.request.status == "Verified"
    assert abs(view.request.challenge_window_end - time.time()) < WINDOW + 30   # opened moments ago
    assert f.balances() == {"requester": f.before["requester"] - ESCROW, "node": f.before["node"] - STAKE}

    f.wait_for_window_end()
    f.payout_when_window_closed()
    after = f.view()
    assert after.request.status == "Paid"
    assert (after.escrow_balance, after.stake_vault_balance) == (0, 0)
    assert f.balances() == {"requester": f.before["requester"] - ESCROW, "node": f.before["node"] + ESCROW}   # node nets +escrow
    with err(net, "InvalidStatus"):
        f.payout()                                                        # double payout
    with err(net, "VerdictNotFail"):
        f.slash()                                                         # cannot slash a Pass


def test_tampered_flow_fail_verdict_slashes_to_the_requester(net):
    f = Flow(net, "tampered")
    f.open()
    f.verdict("Fail")                                                      # inside the window, before the slower error checks
    assert (f.view().request.status, f.c.get_submission(request=f.address).verdict) == ("Challenged", "Fail")
    with err(net, "UnauthorizedVerifier"):
        f.verdict("Pass", key="node")                                      # the node cannot clear itself
    with err(net, "UnauthorizedVerifier"):
        f.c.open_challenge(f.keys["requester"], request=f.address)
    with err(net, "InvalidVerdict"):
        f.verdict("Pending")
    with err(net, "VerdictAlreadySet"):
        f.verdict("Pass")                                                  # a recorded Fail cannot be overturned
    with err(net, "VerdictIsFail"):
        f.payout()                                                         # payout is refused on Fail, even after the window

    f.slash()                                                              # immediately, inside the window; a stranger signs
    after = f.view()
    assert after.request.status == "Slashed" and (after.escrow_balance, after.stake_vault_balance) == (0, 0)
    assert f.balances() == {"requester": f.before["requester"] + STAKE, "node": f.before["node"] - STAKE}   # requester +stake, node -stake
    with err(net, "InvalidStatus"):
        f.slash()                                                          # double slash
    with err(net, "InvalidStatus"):
        f.payout()                                                         # payout after a slash


def test_no_verdict_means_optimistic_payout_after_the_window(net):
    f = Flow(net, "pending")
    f.open()
    with err(net, "ChallengeWindowOpen"):
        f.payout()
    with err(net, "VerdictNotFail"):
        f.slash()                                                          # nothing to slash while Pending
    f.wait_for_window_end()
    with err(net, "ChallengeWindowClosed"):
        f.verdict("Pass")                                                  # too late for the verifier
    f.payout_when_window_closed()
    assert f.view().request.status == "Paid"
    assert f.c.get_submission(request=f.address).verdict == "Pending"      # no verdict was invented
    assert f.balances() == {"requester": f.before["requester"] - ESCROW, "node": f.before["node"] + ESCROW}


def test_a_challenge_cannot_be_opened_twice_and_wrong_payees_are_rejected(net):
    f = Flow(net, "twice")
    f.open()
    first = f.c.get_submission(request=f.address)
    f.verdict("Pass")                                                      # inside the window, before the slower checks
    with err(net, "InvalidStatus"):
        f.c.open_challenge(f.keys["verifier"], request=f.address)
    again = f.c.get_submission(request=f.address)
    assert (again.sample_seed, again.sample_slot) == (first.sample_seed, first.sample_slot)   # the sample is never re-rolled

    f.wait_for_window_end()
    # a stranger tries to route the node's payout to the requester's account
    with pytest.raises(ChainError) as e:
        f.c.finalize_payout(f.keys["admin"], request=f.address, node_token=f.req_tok)
    assert e.value.name == "ConstraintTokenOwner"
    f.payout_when_window_closed()
    assert f.view().request.status == "Paid"
