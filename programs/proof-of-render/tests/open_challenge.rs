mod common;
use anchor_lang::error::ErrorCode as AnchorError;
use common::*;
use proof_of_render::{
    errors::PorError,
    state::{RequestStatus, Verdict},
};
use solana_signer::Signer;

fn committed() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.committed_request(1);
    (env, req)
}

#[test]
fn open_challenge_happy_path_exact_fields() {
    let (mut env, req) = committed();
    let tokens = env.snapshot(&req);
    env.prepare_open_challenge(); // newest SlotHashes entry: (COMMIT_SLOT + 49, SLOT_HASH)
    env.open_challenge(&req).unwrap();

    let s = env.submission(&req);
    assert_eq!(to_hex(&s.sample_seed), EXPECTED_SEED); // sha256(ROOT || SLOT_HASH), computed independently in Python
    assert_eq!(s.sample_slot, COMMIT_SLOT + 49);
    assert_eq!(s.sample_count, 7); // 10 frames
    assert_eq!(s.verdict, Verdict::Pending);
    assert_eq!((s.merkle_root, s.frame_count, s.commit_slot), (ROOT, 10, COMMIT_SLOT)); // untouched

    let r = env.request(&req);
    assert_eq!(r.status, RequestStatus::Challenged);
    assert_eq!(r.challenge_window_end, WINDOW_END); // OPEN_TIME + challenge_window_secs (30)
    assert_eq!((r.node, r.claimed_at, r.commit_deadline), (env.node.pubkey(), BASE_TIME + 10, BASE_TIME + 10 + 600));
    assert_eq!(env.snapshot(&req), tokens); // nothing moves
}

#[test]
fn the_seed_uses_only_the_newest_entry() {
    let seed_with = |newest: [u8; 32], older: [u8; 32]| {
        let (mut env, req) = committed();
        env.warp_to(OPEN_TIME);
        env.warp_to_slot(COMMIT_SLOT + 50);
        env.set_slot_hashes(&[(COMMIT_SLOT + 49, newest), (COMMIT_SLOT + 48, older), (1, older)]);
        env.open_challenge(&req).unwrap();
        to_hex(&env.submission(&req).sample_seed)
    };
    assert_eq!(seed_with([0xAB; 32], [0x11; 32]), EXPECTED_SEED);
    assert_eq!(seed_with([0xAB; 32], [0xEE; 32]), EXPECTED_SEED); // older entries are irrelevant
    // a different newest hash gives a different seed: sha256(ROOT || [0xCD; 32]), computed in Python
    assert_eq!(seed_with([0xCD; 32], [0x11; 32]), "0e87f8a975b01845a9aea18a8ca0bae0c33db9c4bf9666efa4462c8404fa7af3");
}

#[test]
fn sample_count_follows_the_protocol_table() {
    // docs/PROTOCOL.md section 5: 7 when N >= 7 (frames 0, N-1 and 5 drawn), otherwise min(5, N)
    for (frames, expected) in [(1u32, 1u8), (2, 2), (5, 5), (6, 5), (7, 7), (8, 7), (20, 7), (1024, 7)] {
        let mut env = Env::setup();
        let mut p = default_params(1);
        p.num_frames = frames;
        p.price_per_frame = 100_000; // keeps 1024 frames affordable
        let req = env.challenged_request_with(&p);
        let s = env.submission(&req);
        assert_eq!((s.frame_count, s.sample_count), (frames, expected), "N = {frames}");
    }
}

#[test]
fn stale_slot_hashes_are_rejected_until_a_newer_slot_exists() {
    let (mut env, req) = committed();
    let before = env.submission(&req);
    env.warp_to(OPEN_TIME);

    // LiteSVM's default SlotHashes holds only slot 0; the commit happened at slot 100
    env.warp_to_slot(COMMIT_SLOT + 1);
    assert_por_error(&env.open_challenge(&req), PorError::SlotHashNotNewer);

    // newest entry older than, and equal to, the commit slot: both rejected
    for newest in [COMMIT_SLOT - 1, COMMIT_SLOT] {
        env.set_slot_hashes(&[(newest, SLOT_HASH), (newest - 1, [1; 32])]);
        assert_por_error(&env.open_challenge(&req), PorError::SlotHashNotNewer);
    }
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
    assert_eq!(env.submission(&req).sample_seed, before.sample_seed); // nothing was written by the failures

    // one slot after the commit is enough
    env.set_slot_hashes(&[(COMMIT_SLOT + 1, SLOT_HASH)]);
    env.open_challenge(&req).unwrap();
    assert_eq!(env.submission(&req).sample_slot, COMMIT_SLOT + 1);
    assert_eq!(to_hex(&env.submission(&req).sample_seed), EXPECTED_SEED);
}

#[test]
fn an_empty_slot_hashes_sysvar_is_unavailable() {
    let (mut env, req) = committed();
    env.warp_to(OPEN_TIME);
    env.set_slot_hashes(&[]);
    assert_por_error(&env.open_challenge(&req), PorError::SlotHashesUnavailable);
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
}

#[test]
fn a_different_account_in_the_sysvar_slot_is_rejected() {
    let (mut env, req) = committed();
    env.prepare_open_challenge();
    let mut ix = ix_open_challenge(&env.verifier.pubkey(), &req);
    let idx = ix.accounts.len() - 1; // slot_hashes is the last account
    ix.accounts[idx].pubkey = env.admin.pubkey();
    let res = send(&mut env.svm, &[ix], &env.verifier, &[&env.verifier]);
    assert_anchor_error(&res, AnchorError::ConstraintAddress);
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
}

#[test]
fn only_the_verifier_may_open_a_challenge() {
    let (mut env, req) = committed();
    env.prepare_open_challenge();
    for who in [&env.node, &env.requester, &env.admin] {
        let ix = ix_open_challenge(&who.pubkey(), &req);
        assert_por_error(&send(&mut env.svm, &[ix], who, &[who]), PorError::UnauthorizedVerifier);
    }
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
    // and the verifier must actually sign
    let mut ix = ix_open_challenge(&env.verifier.pubkey(), &req);
    ix.accounts[0].is_signer = false;
    assert_fails(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]));
    assert_eq!(env.request(&req).status, RequestStatus::Committed);
}

#[test]
fn open_challenge_requires_status_committed() {
    for status in [
        RequestStatus::Open,
        RequestStatus::Claimed,
        RequestStatus::Challenged,
        RequestStatus::Verified,
        RequestStatus::Paid,
        RequestStatus::Slashed,
        RequestStatus::Expired,
    ] {
        let (mut env, req) = committed();
        env.patch_status(&req, status);
        env.prepare_open_challenge();
        println!("status {status:?}");
        assert_por_error(&env.open_challenge(&req), PorError::InvalidStatus);
        assert_eq!(env.request(&req).status, status);
        assert_eq!(env.submission(&req).sample_slot, 0);
    }
}

#[test]
fn a_request_that_was_never_committed_has_no_submission_to_challenge() {
    let mut env = Env::setup();
    let req = env.claimed_request(1);
    env.prepare_open_challenge();
    assert_anchor_error(&env.open_challenge(&req), AnchorError::AccountNotInitialized);
    assert_eq!(env.request(&req).status, RequestStatus::Claimed);
}

#[test]
fn a_second_open_challenge_changes_nothing() {
    let (mut env, req) = committed();
    env.prepare_open_challenge();
    env.open_challenge(&req).unwrap();
    let (s, r) = (env.submission(&req), env.request(&req));

    // later, with different slot hashes and a later clock: the sample must not be re-rolled
    env.warp_to(OPEN_TIME + 10);
    env.warp_to_slot(COMMIT_SLOT + 90);
    env.set_slot_hashes(&[(COMMIT_SLOT + 89, [0x77; 32])]);
    assert_por_error(&env.open_challenge(&req), PorError::InvalidStatus);
    let (s2, r2) = (env.submission(&req), env.request(&req));
    assert_eq!((s2.sample_seed, s2.sample_slot, s2.sample_count), (s.sample_seed, s.sample_slot, s.sample_count));
    assert_eq!((r2.challenge_window_end, r2.status), (r.challenge_window_end, r.status));
}

#[test]
fn open_challenge_with_another_requests_submission_fails() {
    let (mut env, req1) = committed();
    let req2 = env.open_request(2);
    env.prepare_open_challenge();
    let mut ix = ix_open_challenge(&env.verifier.pubkey(), &req1);
    let idx = ix.accounts.iter().position(|a| a.pubkey == submission_pda(&req1.request)).unwrap();
    ix.accounts[idx].pubkey = submission_pda(&req2.request); // does not exist
    assert_fails(&send(&mut env.svm, &[ix], &env.verifier, &[&env.verifier]));
    assert_eq!(env.request(&req1).status, RequestStatus::Committed);
}
