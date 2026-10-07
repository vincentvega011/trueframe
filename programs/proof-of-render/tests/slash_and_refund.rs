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

/// The requester gets the escrow back AND the node's stake; the node loses its stake; both vaults empty.
const SLASHED: Balances = Balances { requester: START_TOKENS + STAKE, node: START_TOKENS - STAKE, escrow: 0, stake: 0 };
const LOCKED: Balances = Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS - STAKE, escrow: ESCROW, stake: STAKE };
const PAID: Balances = Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS + ESCROW, escrow: 0, stake: 0 };

fn challenged() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.challenged_request(1);
    (env, req)
}

#[test]
fn fail_then_slash_refunds_the_requester_and_takes_the_stake() {
    let (mut env, req) = challenged();
    assert_eq!(env.snapshot(&req), LOCKED);
    env.verdict(&req, Verdict::Fail).unwrap();
    env.slash(&req).unwrap(); // a stranger signs; still inside the challenge window
    assert_eq!(env.snapshot(&req), SLASHED);
    let r = env.request(&req);
    assert_eq!((r.status, r.node), (RequestStatus::Slashed, env.node.pubkey()));
    assert_eq!(env.submission(&req).verdict, Verdict::Fail);
}

#[test]
fn a_slash_works_at_any_time_after_the_fail_verdict() {
    for t in [OPEN_TIME, WINDOW_END - 1, WINDOW_END, WINDOW_END + 1_000_000] {
        let (mut env, req) = challenged();
        env.verdict(&req, Verdict::Fail).unwrap();
        env.warp_to(t);
        env.slash(&req).unwrap();
        assert_eq!(env.snapshot(&req), SLASHED, "t = {t}");
    }
}

#[test]
fn slash_on_a_pass_or_pending_verdict_fails() {
    // Pass
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Pass).unwrap();
    for t in [OPEN_TIME, WINDOW_END + 100] {
        env.warp_to(t);
        assert_por_error(&env.slash(&req), PorError::VerdictNotFail);
    }
    assert_eq!(env.snapshot(&req), LOCKED);
    assert_eq!(env.request(&req).status, RequestStatus::Verified);

    // Pending, inside and after the window, and before the challenge was even opened
    let (mut env, req) = challenged();
    for t in [OPEN_TIME, WINDOW_END - 1, WINDOW_END, WINDOW_END + 100] {
        env.warp_to(t);
        assert_por_error(&env.slash(&req), PorError::VerdictNotFail);
    }
    assert_eq!(env.snapshot(&req), LOCKED);

    let mut env = Env::setup();
    let req = env.committed_request(1);
    assert_por_error(&env.slash(&req), PorError::VerdictNotFail);
    assert_eq!(env.snapshot(&req), LOCKED);
}

#[test]
fn double_slash_fails_and_changes_nothing() {
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    env.slash(&req).unwrap();
    for _ in 0..2 {
        assert_por_error(&env.slash(&req), PorError::InvalidStatus);
    }
    assert_eq!(env.snapshot(&req), SLASHED);
    assert_eq!(env.request(&req).status, RequestStatus::Slashed);
}

#[test]
fn slash_after_a_payout_fails() {
    // paid on a Pass verdict, and paid optimistically on a Pending one
    for pass in [true, false] {
        let (mut env, req) = challenged();
        if pass {
            env.verdict(&req, Verdict::Pass).unwrap();
        }
        env.warp_to(WINDOW_END);
        env.payout(&req).unwrap();
        assert_por_error(&env.slash(&req), PorError::VerdictNotFail);
        assert_eq!(env.snapshot(&req), PAID);
        assert_eq!(env.request(&req).status, RequestStatus::Paid);
    }
}

#[test]
fn a_fail_verdict_with_the_wrong_status_cannot_be_slashed() {
    for status in [RequestStatus::Committed, RequestStatus::Verified, RequestStatus::Paid, RequestStatus::Expired] {
        let (mut env, req) = challenged();
        env.verdict(&req, Verdict::Fail).unwrap();
        env.patch_status(&req, status);
        println!("status {status:?}");
        assert_por_error(&env.slash(&req), PorError::InvalidStatus);
        assert_eq!(env.snapshot(&req), LOCKED);
    }
}

#[test]
fn refund_to_the_wrong_destination_fails() {
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    let (stranger, stranger_token) = env.new_actor(0);
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let requester_other_mint = create_token_account(&mut env.svm, &env.requester, &other_mint, &env.requester.pubkey());

    let cases: Vec<(&str, anchor_lang::prelude::Pubkey, AnchorError)> = vec![
        ("the node's account", env.node_token, AnchorError::ConstraintTokenOwner),
        ("an attacker's own account", stranger_token, AnchorError::ConstraintTokenOwner),
        // the same account in two mutable slots is refused outright (Anchor 1.x), before any owner check
        ("the stake vault itself", req.stake_vault, AnchorError::ConstraintDuplicateMutableAccount),
        ("the requester's account of another mint", requester_other_mint, AnchorError::ConstraintTokenMint),
    ];
    for (name, dest, expected) in cases {
        let ix = ix_slash_and_refund(&stranger.pubkey(), &env.mint, &req, &dest);
        println!("destination: {name}");
        assert_anchor_error(&send(&mut env.svm, &[ix], &stranger, &[&stranger]), expected);
        assert_eq!(env.snapshot(&req), LOCKED);
        assert_eq!(env.balance(&stranger_token), 0);
    }
    env.slash(&req).unwrap();
    assert_eq!(env.snapshot(&req), SLASHED);
}

#[test]
fn refund_with_the_wrong_vaults_or_mint_fails() {
    let (mut env, req1) = challenged();
    env.verdict(&req1, Verdict::Fail).unwrap();
    let req2 = env.committed_request(2); // locks another 10 + 2 USDC, so compare against a fresh snapshot, not LOCKED
    let locked = env.snapshot(&req1);

    for (from, to, expected) in [
        (req1.escrow, req2.escrow, AnchorError::ConstraintSeeds),
        (req1.stake_vault, req2.stake_vault, AnchorError::ConstraintSeeds),
        (req1.escrow, req1.stake_vault, AnchorError::ConstraintDuplicateMutableAccount), // same account twice
    ] {
        let mut ix = ix_slash_and_refund(&env.admin.pubkey(), &env.mint, &req1, &env.requester_token);
        let idx = ix.accounts.iter().position(|a| a.pubkey == from).unwrap();
        ix.accounts[idx].pubkey = to;
        assert_anchor_error(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]), expected);
    }
    assert_eq!(env.snapshot(&req1), locked);

    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let ix = ix_slash_and_refund(&env.admin.pubkey(), &other_mint, &req1, &env.requester_token);
    assert_por_error(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]), PorError::WrongMint);
    assert_eq!(env.snapshot(&req1), locked);
}

#[test]
fn slash_on_a_request_that_was_never_committed_fails() {
    let mut env = Env::setup();
    let req = env.claimed_request(1);
    assert_anchor_error(&env.slash(&req), AnchorError::AccountNotInitialized);
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
}

#[test]
fn donations_to_a_vault_are_not_swept_by_a_slash() {
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    mint_to(&mut env.svm, &env.mint_authority, &env.mint, &req.stake_vault, 77);
    env.slash(&req).unwrap();
    assert_eq!(env.balance(&req.stake_vault), 77);
    assert_eq!((env.balance(&env.requester_token), env.balance(&env.node_token)), (SLASHED.requester, SLASHED.node));
}

#[test]
fn slash_requires_the_caller_to_sign() {
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    let mut ix = ix_slash_and_refund(&env.admin.pubkey(), &env.mint, &req, &env.requester_token);
    ix.accounts[0].is_signer = false;
    assert_fails(&send(&mut env.svm, &[ix], &env.verifier, &[&env.verifier]));
    assert_eq!(env.snapshot(&req), LOCKED);
}

/// The two terminal outcomes side by side: an honest node ends +escrow, a slashed node ends -stake.
#[test]
fn terminal_balances_are_exact_for_both_outcomes() {
    let (mut env, honest) = challenged();
    env.verdict(&honest, Verdict::Pass).unwrap();
    env.warp_to(WINDOW_END);
    env.payout(&honest).unwrap();
    assert_eq!(env.snapshot(&honest), PAID);

    let (mut env, cheat) = challenged();
    env.verdict(&cheat, Verdict::Fail).unwrap();
    env.slash(&cheat).unwrap();
    assert_eq!(env.snapshot(&cheat), SLASHED);
    // conservation: nothing is created or destroyed
    for (b, name) in [(PAID, "paid"), (SLASHED, "slashed")] {
        assert_eq!(b.requester + b.node + b.escrow + b.stake, 2 * START_TOKENS, "{name}");
    }
}
