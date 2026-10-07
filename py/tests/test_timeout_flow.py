"""timeout_refund against a live localnet, through the real TS CLI and the real program, with exact token balances. Short real-time
deadlines. Skips cleanly without a validator."""
import time

import pytest

import livenet
from porf.chain.client import ChainError, CreateRequestParams

USDC = 1_000_000
ESCROW, STAKE = 10 * USDC, 2 * USDC     # 10 frames x 1 USDC; stake_bps 2000 from localnet_up.sh
SHORT = 5                                # seconds
pytestmark = pytest.mark.needs_validator


@pytest.fixture(scope="module")
def net():
    return livenet.make_net()


def make(net, claim_duration, commit_duration):
    c, e = net.client, net.env
    req_tok, node_tok = e["token_accounts"]["requester"], e["token_accounts"]["node"]
    before = {"requester": c.token_balance(token_account=req_tok), "node": c.token_balance(token_account=node_tok)}
    params = CreateRequestParams(request_id=next(net.ids), seed=42, num_frames=10, price_per_frame=USDC, claim_duration=claim_duration,
                                 commit_duration=commit_duration, challenge_window_secs=30, occlusion_level=3)
    address = c.create_request(e["keys"]["requester"], params)["request"]

    def balances():
        return {"requester": c.token_balance(token_account=req_tok), "node": c.token_balance(token_account=node_tok)}
    return address, before, balances


def refund_when_due(net, address, attempts=40):
    for _ in range(attempts):
        try:
            return net.client.timeout_refund(net.env["keys"]["admin"], request=address)   # a stranger signs
        except ChainError as e:
            if e.name != "DeadlineNotReached":
                raise
            time.sleep(0.5)
    raise AssertionError("the deadline never passed")


def test_an_unclaimed_request_is_refunded_after_the_claim_deadline(net):
    c = net.client
    address, before, balances = make(net, SHORT, 600)
    assert balances()["requester"] == before["requester"] - ESCROW
    with pytest.raises(ChainError) as err:
        c.timeout_refund(net.env["keys"]["admin"], request=address)
    assert err.value.name == "DeadlineNotReached" and err.value.code == net.idl_errors["DeadlineNotReached"]
    refund_when_due(net, address)
    assert balances() == before                                          # the requester is made whole; the node never staked
    assert c.get_request(request=address).request.status == "Expired"
    with pytest.raises(ChainError) as err:                               # a second refund
        c.timeout_refund(net.env["keys"]["admin"], request=address)
    assert (err.value.name, err.value.code) == ("InvalidStatus", net.idl_errors["InvalidStatus"])
    assert balances() == before


def test_a_claim_without_a_commit_pays_the_node_stake_to_the_requester(net):
    c = net.client
    address, before, balances = make(net, 600, SHORT)
    c.stake_and_claim(net.env["keys"]["node"], request=address)
    assert balances() == {"requester": before["requester"] - ESCROW, "node": before["node"] - STAKE}
    with pytest.raises(ChainError) as err:
        c.timeout_refund(net.env["keys"]["admin"], request=address)
    assert err.value.name == "DeadlineNotReached"
    refund_when_due(net, address)
    assert balances() == {"requester": before["requester"] + STAKE, "node": before["node"] - STAKE}   # escrow back, stake forfeited
    assert c.get_request(request=address).request.status == "Expired"
    with pytest.raises(ChainError) as err:
        c.timeout_refund(net.env["keys"]["admin"], request=address)
    assert err.value.name == "InvalidStatus"


def test_a_committed_request_cannot_be_refunded(net):
    c = net.client
    address, before, balances = make(net, 600, SHORT)
    c.stake_and_claim(net.env["keys"]["node"], request=address)
    c.submit_commitment(net.env["keys"]["node"], b"\x07" * 32, 10, request=address)
    time.sleep(SHORT + 2)                                                # past the commit deadline: still not refundable
    with pytest.raises(ChainError) as err:
        c.timeout_refund(net.env["keys"]["admin"], request=address)
    assert err.value.name == "InvalidStatus"
    assert c.get_request(request=address).request.status == "Committed"
