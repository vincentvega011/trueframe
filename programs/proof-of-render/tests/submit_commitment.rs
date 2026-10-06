mod common;
use anchor_lang::{error::ErrorCode as AnchorError, solana_program::system_program, Space};
use common::*;
use proof_of_render::{
    errors::PorError,
    state::{RequestStatus, Submission, Verdict},
};
use solana_signer::Signer;

const ROOT: [u8; 32] = [
    0x5b, 0x4f, 0xaa, 0x09, 0xef, 0x2b, 0xcb, 0x0f, 0x4a, 0x44, 0x48, 0x89, 0xda, 0x86, 0x95, 0x2d, 0xb0, 0x53, 0x4a, 0x0f, 0x1e,
    0x6c, 0xf2, 0x09, 0xb8, 0x1e, 0x41, 0x1e, 0x5d, 0xe5, 0x3f, 0xd4,
];
const FRAMES: u32 = 10; // the default request's num_frames
const ESCROW: u64 = 10 * USDC;
const STAKE: u64 = 2 * USDC;

/// Claimed at BASE_TIME + 10, so commit_deadline = BASE_TIME + 10 + 600.
fn claimed() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.claimed_request(1);
    (env, req)
}
const COMMIT_DEADLINE: i64 = BASE_TIME + 10 + 600;

fn lamports(env: &Env, address: &anchor_lang::prelude::Pubkey) -> u64 {
    env.svm.get_account(address).map(|a| a.lamports).unwrap_or(0)
}

#[test]
fn commit_happy_path_exact_submission_fields() {
    let (mut env, req) = claimed();
    env.warp_to_slot(777);
    let tokens_before = env.snapshot(&req);
    let (node_sol, requester_sol) = (lamports(&env, &env.node.pubkey()), lamports(&env, &env.requester.pubkey()));

    env.commit(&req, ROOT, FRAMES).unwrap();

    let s = env.submission(&req);
    assert_eq!(s.request, req.request);
    assert_eq!(s.node, env.node.pubkey());
    assert_eq!(s.merkle_root, ROOT);
    assert_eq!(s.frame_count, FRAMES);
    assert_eq!(s.commit_slot, 777); // recorded from the Clock at commit time
    assert_eq!(s.sample_seed, [0u8; 32]);
    assert_eq!(s.sample_count, 0);
    assert_eq!(s.verdict, Verdict::Pending);
    assert_eq!(s.bump, anchor_lang::prelude::Pubkey::find_program_address(&[proof_of_render::constants::SUBMISSION_SEED, req.request.as_ref()], &proof_of_render::id()).1);

    let r = env.request(&req);
    assert_eq!(r.status, RequestStatus::Committed);
    assert_eq!((r.node, r.claimed_at, r.commit_deadline, r.challenge_window_end), (env.node.pubkey(), BASE_TIME + 10, COMMIT_DEADLINE, 0));

    // no tokens move on commit
    assert_eq!(env.snapshot(&req), tokens_before);
    assert_eq!(tokens_before, Balances { requester: START_TOKENS - ESCROW, node: START_TOKENS - STAKE, escrow: ESCROW, stake: STAKE });

    // the node pays the Submission's rent; the requester pays nothing
    let rent = env.svm.minimum_balance_for_rent_exemption(8 + Submission::INIT_SPACE);
    let sub_account = env.svm.get_account(&submission_pda(&req.request)).unwrap();
    assert_eq!(sub_account.lamports, rent);
    assert_eq!(sub_account.owner, proof_of_render::id());
    assert!(node_sol - lamports(&env, &env.node.pubkey()) >= rent);
    assert_eq!(lamports(&env, &env.requester.pubkey()), requester_sol);
}

#[test]
fn commit_accepts_a_root_with_a_single_nonzero_byte() {
    let (mut env, req) = claimed();
    let mut root = [0u8; 32];
    root[31] = 1;
    env.commit(&req, root, FRAMES).unwrap();
    assert_eq!(env.submission(&req).merkle_root, root);
}

#[test]
fn commit_by_a_node_that_did_not_claim_fails() {
    let (mut env, req) = claimed();
    let (other, _) = env.new_actor(0);
    let ix = ix_submit_commitment(&other.pubkey(), &req, ROOT, FRAMES);
    let res = send(&mut env.svm, &[ix], &other, &[&other]);
    assert_por_error(&res, PorError::UnauthorizedNode);
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
}

#[test]
fn commit_by_the_requester_or_verifier_fails() {
    let (mut env, req) = claimed();
    for who in [&env.requester, &env.verifier, &env.admin] {
        let ix = ix_submit_commitment(&who.pubkey(), &req, ROOT, FRAMES);
        let res = send(&mut env.svm, &[ix], who, &[who]);
        assert_por_error(&res, PorError::UnauthorizedNode);
    }
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
}

#[test]
fn commit_requires_the_node_signature() {
    let (mut env, req) = claimed();
    let mut ix = ix_submit_commitment(&env.node.pubkey(), &req, ROOT, FRAMES);
    ix.accounts[0].is_signer = false; // node does not sign; someone else pays the fee
    let res = send(&mut env.svm, &[ix], &env.admin, &[&env.admin]);
    assert_fails(&res);
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
}

#[test]
fn commit_fails_for_every_status_other_than_claimed() {
    for status in [
        RequestStatus::Open,
        RequestStatus::Committed,
        RequestStatus::Challenged,
        RequestStatus::Verified,
        RequestStatus::Paid,
        RequestStatus::Slashed,
        RequestStatus::Expired,
    ] {
        let (mut env, req) = claimed();
        env.patch_status(&req, status);
        let res = env.commit(&req, ROOT, FRAMES);
        println!("status {status:?}");
        assert_por_error(&res, PorError::InvalidStatus);
        assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
        assert_eq!(env.request(&req).status, status);
    }
}

#[test]
fn commit_on_an_unclaimed_request_fails() {
    // nobody has claimed it: request.node is the default key, so no signer can be "the node"
    let mut env = Env::setup();
    let req = env.open_request(1);
    assert_por_error(&env.commit(&req, ROOT, FRAMES), PorError::UnauthorizedNode);
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
}

#[test]
fn commit_exactly_at_the_commit_deadline_succeeds() {
    let (mut env, req) = claimed();
    env.warp_to(COMMIT_DEADLINE); // now == commit_deadline
    env.commit(&req, ROOT, FRAMES).unwrap();
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
}

#[test]
fn commit_one_second_after_the_commit_deadline_fails() {
    let (mut env, req) = claimed();
    env.warp_to(COMMIT_DEADLINE + 1);
    let res = env.commit(&req, ROOT, FRAMES);
    assert_por_error(&res, PorError::CommitDeadlinePassed);
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
    let r = env.request(&req);
    assert_eq!((r.status, r.commit_deadline), (RequestStatus::Claimed, COMMIT_DEADLINE));
}

#[test]
fn commit_deadline_counts_from_the_claim_not_from_creation() {
    // created at BASE_TIME, claimed at BASE_TIME + 100 (late but allowed): the node still gets a full
    // 600 s to commit, i.e. until BASE_TIME + 700, even though 600 s after creation is BASE_TIME + 600
    let mut env = Env::setup();
    let req = env.open_request(1);
    env.warp_to(BASE_TIME + 100);
    env.claim(&req).unwrap();
    env.warp_to(BASE_TIME + 650); // past creation + 600, within claim + 600
    env.commit(&req, ROOT, FRAMES).unwrap();
}

#[test]
fn commit_with_the_wrong_frame_count_fails() {
    for wrong in [0u32, 1, 9, 11, 1024, u32::MAX] {
        let (mut env, req) = claimed();
        let res = env.commit(&req, ROOT, wrong);
        println!("frame_count {wrong}");
        assert_por_error(&res, PorError::FrameCountMismatch);
        assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
        assert_eq!(env.request(&req).status, RequestStatus::Claimed);
    }
}

#[test]
fn commit_with_an_all_zero_root_fails() {
    let (mut env, req) = claimed();
    let res = env.commit(&req, [0u8; 32], FRAMES);
    assert_por_error(&res, PorError::ZeroMerkleRoot);
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
}

#[test]
fn double_commit_fails_and_keeps_the_first_root() {
    let (mut env, req) = claimed();
    env.commit(&req, ROOT, FRAMES).unwrap();
    let first = env.submission(&req);

    // same root, and a different root (the node trying to swap its commitment)
    for root in [ROOT, [9u8; 32]] {
        let res = env.commit(&req, root, FRAMES);
        let text = format!("{:?}", res.as_ref().err().expect("second commit must fail").meta.logs);
        // Anchor's `init` fails at account creation, before the handler can report InvalidStatus
        assert!(text.contains("already in use"), "{text}");
    }
    let after = env.submission(&req);
    assert_eq!((after.merkle_root, after.commit_slot, after.verdict), (first.merkle_root, first.commit_slot, first.verdict));
    assert_eq!(after.merkle_root, ROOT);
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
}

#[test]
fn commit_with_another_requests_submission_address_fails() {
    let (mut env, req1) = claimed();
    let req2 = env.open_request(2);
    let mut ix = ix_submit_commitment(&env.node.pubkey(), &req1, ROOT, FRAMES);
    let idx = ix.accounts.iter().position(|a| a.pubkey == submission_pda(&req1.request)).unwrap();
    ix.accounts[idx].pubkey = submission_pda(&req2.request);
    let res = send(&mut env.svm, &[ix], &env.node, &[&env.node]);
    assert_anchor_error(&res, AnchorError::ConstraintSeeds);
    assert!(!account_exists(&env.svm, &submission_pda(&req1.request)));
    assert!(!account_exists(&env.svm, &submission_pda(&req2.request)));
}

#[test]
fn commit_with_a_fake_system_program_fails() {
    let (mut env, req) = claimed();
    let mut ix = ix_submit_commitment(&env.node.pubkey(), &req, ROOT, FRAMES);
    let idx = ix.accounts.iter().position(|a| a.pubkey == system_program::ID).unwrap();
    ix.accounts[idx].pubkey = env.admin.pubkey();
    assert_fails(&send(&mut env.svm, &[ix], &env.node, &[&env.node]));
    assert!(!account_exists(&env.svm, &submission_pda(&req.request)));
}
