mod common;
use anchor_lang::error::ErrorCode as AnchorError;
use common::*;
use proof_of_render::{
    errors::PorError,
    state::{RequestStatus, Verdict},
};
use solana_signer::Signer;

fn challenged() -> (Env, Req) {
    let mut env = Env::setup();
    let req = env.challenged_request(1); // clock is at OPEN_TIME; the window ends at WINDOW_END
    (env, req)
}

#[test]
fn pass_moves_the_request_to_verified() {
    let (mut env, req) = challenged();
    let tokens = env.snapshot(&req);
    env.verdict(&req, Verdict::Pass).unwrap();
    assert_eq!(env.submission(&req).verdict, Verdict::Pass);
    let r = env.request(&req);
    assert_eq!((r.status, r.challenge_window_end), (RequestStatus::Verified, WINDOW_END));
    assert_eq!(env.snapshot(&req), tokens); // a verdict moves no money
}

#[test]
fn fail_records_the_verdict_and_leaves_the_status_challenged() {
    let (mut env, req) = challenged();
    let tokens = env.snapshot(&req);
    env.verdict(&req, Verdict::Fail).unwrap();
    assert_eq!(env.submission(&req).verdict, Verdict::Fail);
    assert_eq!(env.request(&req).status, RequestStatus::Challenged);
    assert_eq!(env.snapshot(&req), tokens);
}

#[test]
fn only_the_verifier_may_submit_a_verdict() {
    let (mut env, req) = challenged();
    for who in [&env.node, &env.requester, &env.admin] {
        let ix = ix_submit_verdict(&who.pubkey(), &req, Verdict::Pass);
        assert_por_error(&send(&mut env.svm, &[ix], who, &[who]), PorError::UnauthorizedVerifier);
    }
    let mut ix = ix_submit_verdict(&env.verifier.pubkey(), &req, Verdict::Pass);
    ix.accounts[0].is_signer = false; // verifier does not sign
    assert_fails(&send(&mut env.svm, &[ix], &env.admin, &[&env.admin]));
    assert_eq!(env.submission(&req).verdict, Verdict::Pending);
    assert_eq!(env.request(&req).status, RequestStatus::Challenged);
}

#[test]
fn a_verdict_requires_status_challenged() {
    // a real Committed request (challenge not opened yet)
    let mut env = Env::setup();
    let req = env.committed_request(1);
    assert_por_error(&env.verdict(&req, Verdict::Pass), PorError::InvalidStatus);
    assert_eq!(env.submission(&req).verdict, Verdict::Pending);

    for status in [
        RequestStatus::Open,
        RequestStatus::Claimed,
        RequestStatus::Committed,
        RequestStatus::Verified,
        RequestStatus::Paid,
        RequestStatus::Slashed,
        RequestStatus::Expired,
    ] {
        let (mut env, req) = challenged();
        env.patch_status(&req, status);
        println!("status {status:?}");
        assert_por_error(&env.verdict(&req, Verdict::Fail), PorError::InvalidStatus);
        assert_eq!(env.submission(&req).verdict, Verdict::Pending);
        assert_eq!(env.request(&req).status, status);
    }
}

#[test]
fn pending_is_not_a_verdict() {
    let (mut env, req) = challenged();
    assert_por_error(&env.verdict(&req, Verdict::Pending), PorError::InvalidVerdict);
    assert_eq!(env.submission(&req).verdict, Verdict::Pending);
    assert_eq!(env.request(&req).status, RequestStatus::Challenged);
}

#[test]
fn a_second_verdict_is_rejected_and_the_first_stands() {
    // after Pass the status is Verified, so the status check fires
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Pass).unwrap();
    for v in [Verdict::Pass, Verdict::Fail] {
        assert_por_error(&env.verdict(&req, v), PorError::InvalidStatus);
    }
    assert_eq!((env.submission(&req).verdict, env.request(&req).status), (Verdict::Pass, RequestStatus::Verified));

    // after Fail the status is still Challenged, so the verdict check fires
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    for v in [Verdict::Fail, Verdict::Pass] {
        assert_por_error(&env.verdict(&req, v), PorError::VerdictAlreadySet);
    }
    assert_eq!((env.submission(&req).verdict, env.request(&req).status), (Verdict::Fail, RequestStatus::Challenged));
}

#[test]
fn the_window_closes_exactly_at_window_end() {
    for verdict in [Verdict::Pass, Verdict::Fail] {
        // the last second inside the window is fine
        let (mut env, req) = challenged();
        env.warp_to(WINDOW_END - 1);
        env.verdict(&req, verdict).unwrap();
        assert_eq!(env.submission(&req).verdict, verdict);

        // at window_end and later it is too late, whatever the verdict
        for t in [WINDOW_END, WINDOW_END + 1, WINDOW_END + 100_000] {
            let (mut env, req) = challenged();
            env.warp_to(t);
            assert_por_error(&env.verdict(&req, verdict), PorError::ChallengeWindowClosed);
            assert_eq!(env.submission(&req).verdict, Verdict::Pending, "t = {t}");
            assert_eq!(env.request(&req).status, RequestStatus::Challenged);
        }
    }
}

#[test]
fn checks_run_in_the_documented_order() {
    // status before argument: a Committed request with a Pending argument reports InvalidStatus
    let mut env = Env::setup();
    let req = env.committed_request(1);
    assert_por_error(&env.verdict(&req, Verdict::Pending), PorError::InvalidStatus);

    // argument before the window: a closed window with a Pending argument reports InvalidVerdict
    let (mut env, req) = challenged();
    env.warp_to(WINDOW_END + 5);
    assert_por_error(&env.verdict(&req, Verdict::Pending), PorError::InvalidVerdict);

    // "already set" before the window: a recorded Fail, then a late Pass, reports VerdictAlreadySet
    let (mut env, req) = challenged();
    env.verdict(&req, Verdict::Fail).unwrap();
    env.warp_to(WINDOW_END + 5);
    assert_por_error(&env.verdict(&req, Verdict::Pass), PorError::VerdictAlreadySet);
}

#[test]
fn a_verdict_for_another_requests_submission_fails() {
    let (mut env, req1) = challenged();
    let req2 = env.committed_request(2);
    let mut ix = ix_submit_verdict(&env.verifier.pubkey(), &req1, Verdict::Pass);
    let idx = ix.accounts.iter().position(|a| a.pubkey == submission_pda(&req1.request)).unwrap();
    ix.accounts[idx].pubkey = submission_pda(&req2.request);
    assert_anchor_error(&send(&mut env.svm, &[ix], &env.verifier, &[&env.verifier]), AnchorError::ConstraintSeeds);
    assert_eq!(env.submission(&req2).verdict, Verdict::Pending);
}
