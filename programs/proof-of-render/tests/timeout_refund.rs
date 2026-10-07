mod common;
use anchor_lang::error::ErrorCode as AnchorError;
use common::*;
use proof_of_render::{errors::PorError, state::RequestStatus};
use solana_signer::Signer;

const ESCROW: u64 = 10 * USDC;
const STAKE: u64 = 2 * USDC;

// default_params: claim_duration 120 s (created at BASE_TIME), commit_duration 600 s (counted from the claim at BASE_TIME + 10)
const CLAIM_DEADLINE: i64 = BASE_TIME + 120;
const COMMIT_DEADLINE: i64 = BASE_TIME + 10 + 600;

/// Nobody claimed: the requester gets the escrow back; no stake ever moved.
const REFUNDED_OPEN: Balances = Balances { requester: START_TOKENS, node: START_TOKENS, escrow: 0, stake: 0 };
const LOCKED_OPEN: Balances = Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS, escrow: ESCROW, stake: 0 };
/// Claimed, never committed: the requester gets the escrow AND the node's stake.
const REFUNDED_CLAIMED: Balances = Balances { requester: START_TOKENS + STAKE, node: START_TOKENS - STAKE, escrow: 0, stake: 0 };
const LOCKED_CLAIMED: Balances = Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS - STAKE, escrow: ESCROW, stake: STAKE };

fn open() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.open_request(1);
    (env, req)
}

fn claimed() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.claimed_request(1);
    (env, req)
}

#[test]
fn an_unclaimed_request_refunds_the_escrow_to_the_requester() {
    let (mut env, req) = open();
    assert_eq!(env.snapshot(&req), LOCKED_OPEN);
    env.warp_to(CLAIM_DEADLINE + 1);
    env.timeout_refund(&req).unwrap(); // a stranger signs
    assert_eq!(env.snapshot(&req), REFUNDED_OPEN);
    let r = env.request(&req);
    assert_eq!((r.status, r.node), (RequestStatus::Expired, anchor_lang::prelude::Pubkey::default()));
}

#[test]
fn a_claim_with_no_commit_refunds_the_escrow_and_the_stake_to_the_requester() {
    let (mut env, req) = claimed();
    assert_eq!(env.snapshot(&req), LOCKED_CLAIMED);
    env.warp_to(COMMIT_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    assert_eq!(env.snapshot(&req), REFUNDED_CLAIMED);
    let r = env.request(&req);
    assert_eq!((r.status, r.node), (RequestStatus::Expired, env.node.pubkey()));
    // conservation
    let b = env.snapshot(&req);
    assert_eq!(b.requester + b.node + b.escrow + b.stake, 2 * START_TOKENS);
}

#[test]
fn the_deadline_boundary_open_request() {
    let (mut env, req) = open();
    for t in [BASE_TIME, CLAIM_DEADLINE - 1, CLAIM_DEADLINE] {
        env.warp_to(t);
        assert_por_error(&env.timeout_refund(&req), PorError::DeadlineNotReached);
        assert_eq!((env.snapshot(&req), env.request(&req).status), (LOCKED_OPEN, RequestStatus::Open), "t = {t}");
    }
    // at the deadline the claim is still allowed (the two never both succeed in one second)
    env.warp_to(CLAIM_DEADLINE);
    assert_por_error(&env.timeout_refund(&req), PorError::DeadlineNotReached);
    env.claim(&req).unwrap();
}

#[test]
fn the_deadline_boundary_open_request_succeeds_one_second_later() {
    let (mut env, req) = open();
    env.warp_to(CLAIM_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    assert_eq!(env.snapshot(&req), REFUNDED_OPEN);
}

#[test]
fn the_deadline_boundary_claimed_request() {
    let (mut env, req) = claimed();
    assert_eq!(env.request(&req).commit_deadline, COMMIT_DEADLINE);
    for t in [BASE_TIME + 10, COMMIT_DEADLINE - 1, COMMIT_DEADLINE] {
        env.warp_to(t);
        assert_por_error(&env.timeout_refund(&req), PorError::DeadlineNotReached);
        assert_eq!((env.snapshot(&req), env.request(&req).status), (LOCKED_CLAIMED, RequestStatus::Claimed), "t = {t}");
    }
    // at the commit deadline the commit is still allowed
    env.warp_to(COMMIT_DEADLINE);
    env.commit(&req, ROOT, 10).unwrap();
    assert_eq!(env.request(&req).status, RequestStatus::Committed);

    let (mut env, req) = claimed();
    env.warp_to(COMMIT_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    assert_eq!(env.snapshot(&req), REFUNDED_CLAIMED);
}

#[test]
fn a_claimed_request_is_not_refunded_at_the_claim_deadline() {
    // a Claimed request is judged by the commit deadline, not by the claim deadline that has long passed
    let (mut env, req) = claimed();
    env.warp_to(CLAIM_DEADLINE + 1);
    assert_por_error(&env.timeout_refund(&req), PorError::DeadlineNotReached);
    assert_eq!(env.snapshot(&req), LOCKED_CLAIMED);
}

#[test]
fn every_other_status_is_rejected() {
    // real flows for the states that need a submission
    let mut env = Env::setup();
    let committed = env.committed_request(1);
    env.warp_to(COMMIT_DEADLINE + 10_000);
    assert_por_error(&env.timeout_refund(&committed), PorError::InvalidStatus);
    assert_eq!(env.request(&committed).status, RequestStatus::Committed);

    let mut env = Env::setup();
    let challenged = env.challenged_request(1);
    env.warp_to(COMMIT_DEADLINE + 10_000);
    assert_por_error(&env.timeout_refund(&challenged), PorError::InvalidStatus);
    assert_eq!(env.request(&challenged).status, RequestStatus::Challenged);

    for status in [RequestStatus::Committed, RequestStatus::Challenged, RequestStatus::Verified, RequestStatus::Paid, RequestStatus::Slashed, RequestStatus::Expired] {
        let (mut env, req) = claimed();
        let before = env.snapshot(&req);
        env.patch_status(&req, status);
        env.warp_to(COMMIT_DEADLINE + 10_000);
        println!("status {status:?}");
        assert_por_error(&env.timeout_refund(&req), PorError::InvalidStatus);
        assert_eq!(env.snapshot(&req), before);
    }
}

#[test]
fn a_second_refund_fails_and_changes_nothing() {
    let (mut env, req) = open();
    env.warp_to(CLAIM_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    for _ in 0..2 {
        assert_por_error(&env.timeout_refund(&req), PorError::InvalidStatus);
    }
    assert_eq!((env.snapshot(&req), env.request(&req).status), (REFUNDED_OPEN, RequestStatus::Expired));

    let (mut env, req) = claimed();
    env.warp_to(COMMIT_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    for _ in 0..2 {
        assert_por_error(&env.timeout_refund(&req), PorError::InvalidStatus);
    }
    assert_eq!((env.snapshot(&req), env.request(&req).status), (REFUNDED_CLAIMED, RequestStatus::Expired));
}

#[test]
fn an_expired_request_can_be_neither_claimed_nor_committed() {
    let (mut env, req) = open();
    env.warp_to(CLAIM_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    assert_por_error(&env.claim(&req), PorError::InvalidStatus);

    let (mut env, req) = claimed();
    env.warp_to(COMMIT_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    assert_por_error(&env.commit(&req, ROOT, 10), PorError::InvalidStatus);
    assert_eq!(env.snapshot(&req), REFUNDED_CLAIMED);
}

#[test]
fn refund_to_the_wrong_destination_fails() {
    let (mut env, req) = claimed();
    env.warp_to(COMMIT_DEADLINE + 1);
    let (stranger, stranger_token) = env.new_actor(0);
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let requester_other_mint = create_token_account(&mut env.svm, &env.requester, &other_mint, &env.requester.pubkey());
    let cases: Vec<(&str, anchor_lang::prelude::Pubkey, AnchorError)> = vec![
        ("the node's account (it would keep its stake)", env.node_token, AnchorError::ConstraintTokenOwner),
        ("an attacker's own account", stranger_token, AnchorError::ConstraintTokenOwner),
        ("the stake vault itself", req.stake_vault, AnchorError::ConstraintDuplicateMutableAccount),
        ("the requester's account of another mint", requester_other_mint, AnchorError::ConstraintTokenMint),
    ];
    for (name, dest, expected) in cases {
        let ix = ix_timeout_refund(&stranger.pubkey(), &env.mint, &req, &dest);
        println!("destination: {name}");
        assert_anchor_error(&send(&mut env.svm, &[ix], &stranger, &[&stranger]), expected);
        assert_eq!(env.snapshot(&req), LOCKED_CLAIMED);
        assert_eq!(env.balance(&stranger_token), 0);
    }
    env.timeout_refund(&req).unwrap();
    assert_eq!(env.snapshot(&req), REFUNDED_CLAIMED);
}

#[test]
fn refund_with_the_wrong_vaults_or_mint_fails() {
    let (mut env, req1) = claimed();
    let req2 = env.claimed_request(2); // a second, still-locked request
    env.warp_to(COMMIT_DEADLINE + 1);
    let locked = env.snapshot(&req1);
    for (from, to, expected) in [
        (req1.escrow, req2.escrow, AnchorError::ConstraintSeeds),
        (req1.stake_vault, req2.stake_vault, AnchorError::ConstraintSeeds),
        (req1.escrow, req1.stake_vault, AnchorError::ConstraintDuplicateMutableAccount),
    ] {
        let mut ix = ix_timeout_refund(&env.admin.pubkey(), &env.mint, &req1, &env.requester_token);
        let idx = ix.accounts.iter().position(|a| a.pubkey == from).unwrap();
        ix.accounts[idx].pubkey = to;
        assert_anchor_error(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]), expected);
    }
    assert_eq!(env.snapshot(&req1), locked);
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let ix = ix_timeout_refund(&env.admin.pubkey(), &other_mint, &req1, &env.requester_token);
    assert_por_error(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]), PorError::WrongMint);
    assert_eq!(env.snapshot(&req1), locked);
}

#[test]
fn anyone_may_call_but_the_caller_must_sign() {
    for caller in ["requester", "node", "stranger"] {
        let (mut env, req) = claimed();
        env.warp_to(COMMIT_DEADLINE + 1);
        let key = match caller {
            "requester" => env.requester.insecure_clone(),
            "node" => env.node.insecure_clone(),
            _ => env.admin.insecure_clone(),
        };
        let ix = ix_timeout_refund(&key.pubkey(), &env.mint, &req, &env.requester_token);
        send(&mut env.svm, &[ix], &key, &[&key]).unwrap();
        assert_eq!(env.snapshot(&req), REFUNDED_CLAIMED, "caller {caller}");
    }
    let (mut env, req) = open();
    env.warp_to(CLAIM_DEADLINE + 1);
    let mut ix = ix_timeout_refund(&env.admin.pubkey(), &env.mint, &req, &env.requester_token);
    ix.accounts[0].is_signer = false;
    assert_fails(&send(&mut env.svm, &[ix], &env.verifier, &[&env.verifier]));
    assert_eq!(env.snapshot(&req), LOCKED_OPEN);
}

#[test]
fn donations_to_the_vaults_are_not_swept_by_a_refund() {
    let (mut env, req) = open();
    mint_to(&mut env.svm, &env.mint_authority, &env.mint, &req.stake_vault, 77); // an unclaimed request has an empty stake vault
    env.warp_to(CLAIM_DEADLINE + 1);
    env.timeout_refund(&req).unwrap();
    assert_eq!(env.balance(&req.stake_vault), 77);
    assert_eq!(env.balance(&env.requester_token), START_TOKENS); // exactly the escrow came back
}

#[test]
fn refund_on_an_open_request_leaves_the_node_untouched_even_with_a_stake_amount_recorded() {
    let (mut env, req) = open();
    assert_eq!(env.request(&req).stake_amount, STAKE); // recorded but never deposited
    env.warp_to(CLAIM_DEADLINE + 1);
    env.timeout_refund(&req).unwrap(); // must not try to move a stake that is not there
    assert_eq!(env.snapshot(&req), REFUNDED_OPEN);
}
