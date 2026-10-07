mod common;
use anchor_lang::error::ErrorCode as AnchorError;
use common::*;
use proof_of_render::{
    errors::PorError,
    state::{RequestStatus, Verdict},
};
use solana_signer::Signer;

const ESCROW: u64 = 10 * USDC;
const STAKE: u64 = 2 * USDC;

/// Requester paid the escrow, node got it back plus its stake returned; both vaults empty.
const PAID: Balances = Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS + ESCROW, escrow: 0, stake: 0 };
/// Before any payout: escrow and stake are both locked in their vaults.
const LOCKED: Balances = Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS - STAKE, escrow: ESCROW, stake: STAKE };

fn challenged() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.challenged_request(1);
    (env, req)
}

#[test]
fn pass_then_payout_after_the_window_pays_the_node() {
    let (mut env, req) = challenged();
    assert_eq!(env.snapshot(&req), LOCKED);
    env.verdict(&req, Verdict::Pass).unwrap();
    env.warp_to(WINDOW_END);
    env.payout(&req).unwrap(); // signed by a stranger (admin), not by the node
    assert_eq!(env.snapshot(&req), PAID);
    let r = env.request(&req);
    assert_eq!((r.status, r.node), (RequestStatus::Paid, env.node.pubkey()));
    assert_eq!(env.submission(&req).verdict, Verdict::Pass);
}

#[test]
fn a_pending_verdict_is_paid_after_the_window() {
    // exactly at the window end, and long after it
    for t in [WINDOW_END, WINDOW_END + 1, WINDOW_END + 1_000_000] {
        let (mut env, req) = challenged();
        env.warp_to(t);
        env.payout(&req).unwrap();
        assert_eq!(env.snapshot(&req), PAID, "t = {t}");
        assert_eq!(env.request(&req).status, RequestStatus::Paid);
        assert_eq!(env.submission(&req).verdict, Verdict::Pending); // the optimistic payout does not invent a verdict
    }
}

#[test]
fn payout_one_second_before_the_window_ends_fails() {
    // with a Pass verdict (Verified) and with no verdict (Pending)
    for pass in [true, false] {
        let (mut env, req) = challenged();
        if pass {
            env.verdict(&req, Verdict::Pass).unwrap();
        }
        for t in [OPEN_TIME, WINDOW_END - 1] {
            env.warp_to(t);
            assert_por_error(&env.payout(&req), PorError::ChallengeWindowOpen);
            assert_eq!(env.snapshot(&req), LOCKED, "pass={pass} t={t}");
        }
        assert_eq!(env.request(&req).status, if pass { RequestStatus::Verified } else { RequestStatus::Challenged });
        // and the very next second it works
        env.warp_to(WINDOW_END);
        env.payout(&req).unwrap();
        assert_eq!(env.snapshot(&req), PAID);
    }
}

#[test]
fn payout_after_a_fail_verdict_is_rejected_forever() {
    for t in [OPEN_TIME, WINDOW_END, WINDOW_END + 1_000_000] {
        let (mut env, req) = challenged();
        env.verdict(&req, Verdict::Fail).unwrap();
        env.warp_to(t);
        assert_por_error(&env.payout(&req), PorError::VerdictIsFail);
        assert_eq!(env.snapshot(&req), LOCKED, "t = {t}");
        assert_eq!(env.request(&req).status, RequestStatus::Challenged);
    }
}

#[test]
fn double_payout_fails_and_changes_nothing() {
    let (mut env, req) = challenged();
    env.warp_to(WINDOW_END);
    env.payout(&req).unwrap();
    for _ in 0..2 {
        assert_por_error(&env.payout(&req), PorError::InvalidStatus);
    }
    assert_eq!(env.snapshot(&req), PAID);
    assert_eq!(env.request(&req).status, RequestStatus::Paid);
}

#[test]
fn payout_after_a_slash_fails() {
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    env.slash(&req).unwrap();
    env.warp_to(WINDOW_END + 10);
    let res = env.payout(&req);
    assert_por_error(&res, PorError::InvalidStatus); // status is checked before the verdict
    assert_eq!(env.request(&req).status, RequestStatus::Slashed);
}

#[test]
fn payout_requires_status_challenged_or_verified() {
    // a real Committed request (challenge never opened)
    let mut env = Env::setup();
    let req = env.committed_request(1);
    env.warp_to(WINDOW_END + 100);
    assert_por_error(&env.payout(&req), PorError::InvalidStatus);
    assert_eq!(env.snapshot(&req), LOCKED);

    for status in [RequestStatus::Open, RequestStatus::Claimed, RequestStatus::Committed, RequestStatus::Paid, RequestStatus::Slashed, RequestStatus::Expired] {
        let (mut env, req) = challenged();
        env.patch_status(&req, status);
        env.warp_to(WINDOW_END + 100);
        println!("status {status:?}");
        assert_por_error(&env.payout(&req), PorError::InvalidStatus);
        assert_eq!(env.snapshot(&req), LOCKED);
    }
}

#[test]
fn payout_to_the_wrong_destination_fails() {
    let (mut env, req) = challenged();
    env.warp_to(WINDOW_END);
    let (stranger, stranger_token) = env.new_actor(0);
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let node_other_mint = create_token_account(&mut env.svm, &env.node, &other_mint, &env.node.pubkey());

    let cases: Vec<(&str, anchor_lang::prelude::Pubkey, AnchorError)> = vec![
        ("the requester's account", env.requester_token, AnchorError::ConstraintTokenOwner),
        ("an attacker's own account", stranger_token, AnchorError::ConstraintTokenOwner),
        // the same account in two mutable slots is refused outright (Anchor 1.x), before any owner check
        ("the escrow vault itself", req.escrow, AnchorError::ConstraintDuplicateMutableAccount),
        ("the node's account of another mint", node_other_mint, AnchorError::ConstraintTokenMint),
    ];
    for (name, dest, expected) in cases {
        let ix = ix_finalize_payout(&stranger.pubkey(), &env.mint, &req, &dest);
        println!("destination: {name}");
        assert_anchor_error(&send(&mut env.svm, &[ix], &stranger, &[&stranger]), expected);
        assert_eq!(env.snapshot(&req), LOCKED);
        assert_eq!(env.balance(&stranger_token), 0);
    }
    env.payout(&req).unwrap(); // the correct destination still works afterwards
    assert_eq!(env.snapshot(&req), PAID);
}

#[test]
fn payout_with_the_wrong_vaults_or_mint_fails() {
    let (mut env, req1) = challenged();
    let req2 = env.committed_request(2); // locks another 10 + 2 USDC, so compare against a fresh snapshot, not LOCKED
    env.warp_to(WINDOW_END);
    let locked = env.snapshot(&req1);

    let swap = |env: &mut Env, from: anchor_lang::prelude::Pubkey, to: anchor_lang::prelude::Pubkey| {
        let mut ix = ix_finalize_payout(&env.admin.pubkey(), &env.mint, &req1, &env.node_token);
        let idx = ix.accounts.iter().position(|a| a.pubkey == from).unwrap();
        ix.accounts[idx].pubkey = to;
        send(&mut env.svm, &[ix], &env.admin, &[&env.admin])
    };
    // another request's vaults, and the two vaults swapped with each other
    assert_anchor_error(&swap(&mut env, req1.escrow, req2.escrow), AnchorError::ConstraintSeeds);
    assert_anchor_error(&swap(&mut env, req1.stake_vault, req2.stake_vault), AnchorError::ConstraintSeeds);
    assert_anchor_error(&swap(&mut env, req1.escrow, req1.stake_vault), AnchorError::ConstraintDuplicateMutableAccount); // same account twice
    assert_eq!(env.snapshot(&req1), locked);

    // a mint account that is not the request's mint
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let ix = ix_finalize_payout(&env.admin.pubkey(), &other_mint, &req1, &env.node_token);
    assert_por_error(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]), PorError::WrongMint);
    assert_eq!(env.snapshot(&req1), locked);
}

#[test]
fn payout_on_a_request_that_was_never_committed_fails() {
    let mut env = Env::setup();
    let req = env.claimed_request(1);
    env.warp_to(WINDOW_END + 100);
    assert_anchor_error(&env.payout(&req), AnchorError::AccountNotInitialized);
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
}

#[test]
fn only_the_recorded_amounts_move_and_donations_to_a_vault_stay_put() {
    let (mut env, req) = challenged();
    mint_to(&mut env.svm, &env.mint_authority, &env.mint, &req.escrow, 123); // someone donates dust to the escrow
    env.warp_to(WINDOW_END);
    env.payout(&req).unwrap();
    assert_eq!(env.balance(&req.escrow), 123); // the dust is not swept and not stolen
    assert_eq!((env.balance(&env.node_token), env.balance(&env.requester_token)), (START_TOKENS + ESCROW, START_TOKENS - ESCROW));
}

#[test]
fn payout_requires_the_caller_to_sign() {
    let (mut env, req) = challenged();
    env.warp_to(WINDOW_END);
    let mut ix = ix_finalize_payout(&env.admin.pubkey(), &env.mint, &req, &env.node_token);
    ix.accounts[0].is_signer = false;
    assert_fails(&send(&mut env.svm, &[ix], &env.verifier, &[&env.verifier]));
    assert_eq!(env.snapshot(&req), LOCKED);
}
