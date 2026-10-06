mod common;
use anchor_lang::error::ErrorCode as AnchorError;
use common::*;
use proof_of_render::{errors::PorError, state::RequestStatus};
use solana_signer::Signer;

const STAKE: u64 = 2 * USDC; // 20% of the default 10 USDC escrow
const ESCROW: u64 = 10 * USDC;

fn open() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.open_request(1);
    (env, req)
}

#[test]
fn claim_happy_path_exact_balances_and_commit_deadline_set_at_claim_time() {
    let (mut env, req) = open();
    assert_eq!(
        env.snapshot(&req),
        Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS, escrow: ESCROW, stake: 0 }
    );

    // the node arrives 50 s after creation, inside the 120 s claim window
    env.warp_to(BASE_TIME + 50);
    env.claim(&req).unwrap();

    assert_eq!(
        env.snapshot(&req),
        Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS - STAKE, escrow: ESCROW, stake: STAKE }
    );
    let r = env.request(&req);
    assert_eq!(r.status, RequestStatus::Claimed);
    assert_eq!(r.node, env.node.pubkey());
    assert_eq!(r.claimed_at, BASE_TIME + 50);
    // commit_deadline = claimed_at + commit_duration (600), NOT created_at + ...
    assert_eq!(r.commit_deadline, BASE_TIME + 50 + 600);
    // untouched by the claim
    assert_eq!((r.created_at, r.claim_deadline, r.challenge_window_end), (BASE_TIME, BASE_TIME + 120, 0));
    assert_eq!((r.escrow_amount, r.stake_amount), (ESCROW, STAKE));
}

#[test]
fn claim_exactly_at_the_claim_deadline_succeeds() {
    let (mut env, req) = open();
    env.warp_to(BASE_TIME + 120); // now == claim_deadline
    env.claim(&req).unwrap();
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
    assert_eq!(env.request(&req).commit_deadline, BASE_TIME + 120 + 600);
}

#[test]
fn claim_after_the_claim_deadline_fails() {
    let (mut env, req) = open();
    let before = env.snapshot(&req);
    env.warp_to(BASE_TIME + 121); // one second late
    let res = env.claim(&req);
    assert_por_error(&res, PorError::ClaimDeadlinePassed);
    assert_eq!(env.snapshot(&req), before);
    let r = env.request(&req);
    assert_eq!((r.status, r.node, r.claimed_at, r.commit_deadline), (RequestStatus::Open, Default::default(), 0, 0));
}

#[test]
fn double_claim_fails_and_changes_nothing() {
    let (mut env, req) = open();
    env.claim(&req).unwrap();
    let after_first = env.snapshot(&req);

    // a second node tries to claim the same request
    let (node2, node2_token) = env.new_actor(START_TOKENS);
    let ix = ix_stake_and_claim(&node2.pubkey(), &env.mint, &req, &node2_token);
    let res = send(&mut env.svm, &[ix], &node2, &[&node2]);
    assert_por_error(&res, PorError::InvalidStatus);
    assert_eq!(env.balance(&node2_token), START_TOKENS);

    // and the first node tries again
    assert_por_error(&env.claim(&req), PorError::InvalidStatus);

    assert_eq!(env.snapshot(&req), after_first);
    assert_eq!(env.request(&req).node, env.node.pubkey());
}

#[test]
fn claim_fails_for_every_non_open_status() {
    for status in [
        RequestStatus::Claimed,
        RequestStatus::Committed,
        RequestStatus::Challenged,
        RequestStatus::Verified,
        RequestStatus::Paid,
        RequestStatus::Slashed,
        RequestStatus::Expired,
    ] {
        let (mut env, req) = open();
        env.patch_status(&req, status);
        let before = env.snapshot(&req);
        let res = env.claim(&req);
        println!("status {status:?}");
        assert_por_error(&res, PorError::InvalidStatus);
        assert_eq!(env.snapshot(&req), before);
        assert_eq!(env.request(&req).status, status);
    }
}

#[test]
fn claim_status_check_comes_before_the_deadline_check() {
    // an Expired request past its deadline reports the status error, not a deadline error
    let (mut env, req) = open();
    env.patch_status(&req, RequestStatus::Expired);
    env.warp_to(BASE_TIME + 10_000);
    assert_por_error(&env.claim(&req), PorError::InvalidStatus);
}

#[test]
fn claim_with_wrong_mint_account_fails() {
    let (mut env, req) = open();
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let other_token = create_token_account(&mut env.svm, &env.node, &other_mint, &env.node.pubkey());
    mint_to(&mut env.svm, &env.mint_authority, &other_mint, &other_token, START_TOKENS);
    let before = env.snapshot(&req);

    let ix = ix_stake_and_claim(&env.node.pubkey(), &other_mint, &req, &other_token);
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_por_error(&res, PorError::WrongMint);
    assert_eq!(env.snapshot(&req), before);
    assert_eq!(env.balance(&other_token), START_TOKENS);
}

#[test]
fn claim_with_node_token_account_of_another_mint_fails() {
    let (mut env, req) = open();
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let other_token = create_token_account(&mut env.svm, &env.node, &other_mint, &env.node.pubkey());
    mint_to(&mut env.svm, &env.mint_authority, &other_mint, &other_token, START_TOKENS);

    let ix = ix_stake_and_claim(&env.node.pubkey(), &env.mint, &req, &other_token);
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_anchor_error(&res, AnchorError::ConstraintTokenMint);
    assert_eq!(env.request(&req).status, RequestStatus::Open);
}

#[test]
fn claim_with_someone_elses_token_account_fails() {
    let (mut env, req) = open();
    // the requester's account has the right mint and plenty of funds, but the node does not own it
    let ix = ix_stake_and_claim(&env.node.pubkey(), &env.mint, &req, &env.requester_token);
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_anchor_error(&res, AnchorError::ConstraintTokenOwner);
    assert_eq!(env.balance(&env.requester_token), START_TOKENS - ESCROW);
    assert_eq!(env.request(&req).status, RequestStatus::Open);
}

#[test]
fn claim_with_insufficient_node_balance_fails_and_stays_open() {
    let (mut env, req) = open();
    let (poor, poor_token) = env.new_actor(STAKE - 1); // one base unit short of the stake
    let before = env.snapshot(&req);
    let ix = ix_stake_and_claim(&poor.pubkey(), &env.mint, &req, &poor_token);
    let res = send(&mut env.svm, &[ix], &poor, &[&poor]);
    assert_fails(&res);
    assert_eq!(env.balance(&poor_token), STAKE - 1);
    assert_eq!(env.snapshot(&req), before);
    assert_eq!(env.request(&req).status, RequestStatus::Open);

    // exactly the stake is enough
    let (ok, ok_token) = env.new_actor(STAKE);
    let ix = ix_stake_and_claim(&ok.pubkey(), &env.mint, &req, &ok_token);
    send(&mut env.svm, &[ix], &ok, &[&ok]).unwrap();
    assert_eq!(env.balance(&ok_token), 0);
    assert_eq!(env.balance(&req.stake_vault), STAKE);
    assert_eq!(env.request(&req).node, ok.pubkey());
}

#[test]
fn claim_with_another_requests_stake_vault_fails() {
    let (mut env, req1) = open();
    let req2 = env.open_request(2);
    let mut ix = ix_stake_and_claim(&env.node.pubkey(), &env.mint, &req1, &env.node_token);
    // swap request 1's stake vault for request 2's
    let idx = ix.accounts.iter().position(|a| a.pubkey == req1.stake_vault).unwrap();
    ix.accounts[idx].pubkey = req2.stake_vault;
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_anchor_error(&res, AnchorError::ConstraintSeeds);
    assert_eq!(env.balance(&req2.stake_vault), 0);
    assert_eq!(env.request(&req1).status, RequestStatus::Open);
}

#[test]
fn claim_with_the_escrow_vault_in_place_of_the_stake_vault_fails() {
    let (mut env, req) = open();
    let mut ix = ix_stake_and_claim(&env.node.pubkey(), &env.mint, &req, &env.node_token);
    let idx = ix.accounts.iter().position(|a| a.pubkey == req.stake_vault).unwrap();
    ix.accounts[idx].pubkey = req.escrow;
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_anchor_error(&res, AnchorError::ConstraintSeeds);
    assert_eq!(env.balance(&req.escrow), ESCROW);
}

#[test]
fn claim_requires_the_node_signature() {
    let (mut env, req) = open();
    let mut ix = ix_stake_and_claim(&env.node.pubkey(), &env.mint, &req, &env.node_token);
    ix.accounts[0].is_signer = false; // node does not sign; a third party pays the fee
    let res = send(&mut env.svm, &[ix], &env.admin, &[&env.admin]);
    assert_fails(&res);
    assert_eq!(env.balance(&env.node_token), START_TOKENS);
    assert_eq!(env.request(&req).status, RequestStatus::Open);
}

#[test]
fn claim_with_a_fake_config_account_fails() {
    // the config account must be the real config PDA; a wallet in its place is rejected
    let (mut env, req) = open();
    let mut ix = ix_stake_and_claim(&env.node.pubkey(), &env.mint, &req, &env.node_token);
    let idx = ix.accounts.iter().position(|a| a.pubkey == config_pda()).unwrap();
    ix.accounts[idx].pubkey = env.admin.pubkey(); // not the config PDA
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_fails(&res);
    assert_eq!(env.request(&req).status, RequestStatus::Open);
}
