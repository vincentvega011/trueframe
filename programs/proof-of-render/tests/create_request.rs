mod common;
use anchor_lang::error::ErrorCode as AnchorError;
use common::*;
use proof_of_render::{errors::PorError, state::RequestStatus, CreateRequestParams};
use solana_signer::Signer;

#[test]
fn create_request_happy_path_exact_balances_and_fields() {
    let mut env = Env::setup();
    let p = default_params(7);
    let req = Req::new(&env.requester.pubkey(), 7);
    assert_eq!((env.balance(&env.requester_token), env.balance(&env.node_token)), (START_TOKENS, START_TOKENS));

    env.create_request(&p).unwrap();

    let escrow = 10 * USDC;
    assert_eq!(env.balance(&env.requester_token), START_TOKENS - escrow);
    assert_eq!(env.balance(&env.node_token), START_TOKENS);
    assert_eq!(env.balance(&req.escrow), escrow);
    assert_eq!(env.balance(&req.stake_vault), 0);

    let r = env.request(&req);
    assert_eq!(r.requester, env.requester.pubkey());
    assert_eq!(r.request_id, 7);
    assert_eq!(r.mint, env.mint);
    assert_eq!((r.scene_type, r.seed, r.num_frames), (0, 42, 10));
    assert_eq!((r.width, r.height, r.occlusion_level), (640, 480, 3));
    assert_eq!(r.price_per_frame, USDC);
    assert_eq!(r.escrow_amount, escrow);
    assert_eq!(r.stake_amount, 2 * USDC); // 20% of 10 USDC
    assert_eq!((r.claim_duration, r.commit_duration, r.challenge_window_secs), (120, 600, 30));
    assert_eq!(r.node, Default::default());
    assert_eq!(r.status, RequestStatus::Open);
    assert_eq!(r.created_at, BASE_TIME);
    assert_eq!(r.claim_deadline, BASE_TIME + 120);
    assert_eq!((r.claimed_at, r.commit_deadline, r.challenge_window_end), (0, 0, 0));

    // both vaults hold the right mint and are controlled by the Request PDA
    for vault in [&req.escrow, &req.stake_vault] {
        assert_eq!(token_mint(&env.svm, vault), env.mint);
        assert_eq!(token_owner(&env.svm, vault), req.request);
    }
}

#[test]
fn create_request_accepts_the_upper_bounds() {
    let mut env = Env::setup();
    let mut p = default_params(1);
    p.num_frames = 1024;
    p.width = 4096;
    p.height = 4096;
    p.price_per_frame = 1;
    env.create_request(&p).unwrap();
    let r = env.request(&Req::new(&env.requester.pubkey(), 1));
    assert_eq!((r.num_frames, r.width, r.height), (1024, 4096, 4096));
    assert_eq!(r.escrow_amount, 1024);
    assert_eq!(r.stake_amount, 204); // floor(1024 * 2000 / 10000)
}

#[test]
fn create_request_accepts_the_lower_bounds() {
    let mut env = Env::setup();
    let mut p = default_params(1);
    p.num_frames = 1;
    p.width = 1;
    p.height = 1;
    p.price_per_frame = 5;
    p.occlusion_level = 0;
    p.claim_duration = 1;
    p.commit_duration = 1;
    p.challenge_window_secs = 1;
    env.create_request(&p).unwrap();
    assert_eq!(env.request(&Req::new(&env.requester.pubkey(), 1)).stake_amount, 1);
}

/// Every invalid parameter is rejected with its own typed error and creates nothing.
#[test]
fn create_request_rejects_invalid_parameters() {
    type Edit = fn(&mut CreateRequestParams);
    let cases: Vec<(&str, Edit, PorError)> = vec![
        ("zero frames", |p| p.num_frames = 0, PorError::ZeroFrames),
        ("1025 frames", |p| p.num_frames = 1025, PorError::TooManyFrames),
        ("u32::MAX frames", |p| p.num_frames = u32::MAX, PorError::TooManyFrames),
        ("zero price", |p| p.price_per_frame = 0, PorError::ZeroPrice),
        ("scene type 1", |p| p.scene_type = 1, PorError::InvalidSceneType),
        ("occlusion level 4", |p| p.occlusion_level = 4, PorError::InvalidOcclusionLevel),
        ("zero width", |p| p.width = 0, PorError::InvalidResolution),
        ("zero height", |p| p.height = 0, PorError::InvalidResolution),
        ("width 4097", |p| p.width = 4097, PorError::ResolutionTooLarge),
        ("height 4097", |p| p.height = 4097, PorError::ResolutionTooLarge),
        ("width u16::MAX", |p| p.width = u16::MAX, PorError::ResolutionTooLarge),
        ("zero claim duration", |p| p.claim_duration = 0, PorError::InvalidDuration),
        ("zero commit duration", |p| p.commit_duration = 0, PorError::InvalidDuration),
        ("zero challenge window", |p| p.challenge_window_secs = 0, PorError::InvalidDuration),
        ("negative claim duration", |p| p.claim_duration = -1, PorError::InvalidDuration),
        ("negative commit duration", |p| p.commit_duration = -5, PorError::InvalidDuration),
        ("negative challenge window", |p| p.challenge_window_secs = i64::MIN, PorError::InvalidDuration),
        ("escrow overflow", |p| { p.price_per_frame = u64::MAX; p.num_frames = 2 }, PorError::MathOverflow),
        ("escrow overflow near the limit", |p| { p.price_per_frame = u64::MAX / 2 + 1; p.num_frames = 2 }, PorError::MathOverflow),
        ("stake overflow", |p| { p.price_per_frame = u64::MAX / 3; p.num_frames = 3 }, PorError::MathOverflow),
        ("stake rounds to zero", |p| { p.price_per_frame = 1; p.num_frames = 1 }, PorError::ZeroStake),
        ("stake rounds to zero (4 < 5)", |p| { p.price_per_frame = 1; p.num_frames = 4 }, PorError::ZeroStake),
        ("duration sum overflows", |p| { p.claim_duration = i64::MAX / 2; p.commit_duration = i64::MAX / 2; p.challenge_window_secs = 10 }, PorError::MathOverflow),
        ("deadline overflows", |p| { p.claim_duration = i64::MAX - 5; p.commit_duration = 1; p.challenge_window_secs = 1 }, PorError::MathOverflow),
    ];
    for (name, edit, expected) in cases {
        let mut env = Env::setup();
        let mut p = default_params(9);
        edit(&mut p);
        let res = env.create_request(&p);
        println!("case: {name}");
        assert_por_error(&res, expected);
        let req = Req::new(&env.requester.pubkey(), 9);
        assert!(!account_exists(&env.svm, &req.request), "{name}: request must not exist");
        assert_eq!(env.balance(&env.requester_token), START_TOKENS, "{name}: requester balance unchanged");
    }
}

#[test]
fn create_request_stake_rounds_down() {
    let mut env = Env::setup();
    let mut p = default_params(1);
    p.price_per_frame = 3;
    p.num_frames = 7; // escrow 21, stake = floor(21 * 2000 / 10000) = floor(4.2) = 4
    env.create_request(&p).unwrap();
    let req = Req::new(&env.requester.pubkey(), 1);
    assert_eq!(env.request(&req).escrow_amount, 21);
    assert_eq!(env.request(&req).stake_amount, 4);
    assert_eq!(env.balance(&req.escrow), 21);
}

#[test]
fn create_request_uses_the_configured_stake_bps() {
    let mut env = Env::new();
    env.init_config(5000).unwrap();
    env.create_request(&default_params(1)).unwrap();
    assert_eq!(env.request(&Req::new(&env.requester.pubkey(), 1)).stake_amount, 5 * USDC);
}

#[test]
fn create_request_with_wrong_mint_account_fails() {
    let mut env = Env::setup();
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let other_token = create_token_account(&mut env.svm, &env.requester, &other_mint, &env.requester.pubkey());
    mint_to(&mut env.svm, &env.mint_authority, &other_mint, &other_token, START_TOKENS);

    // mint account is not the configured mint (token account matches that other mint, so only the config check can reject it)
    let ix = ix_create_request(&env.requester.pubkey(), &other_mint, &other_token, &default_params(1));
    let res = send(&mut env.svm, &[ix], &env.requester, &[&env.requester]);
    assert_por_error(&res, PorError::WrongMint);
    assert!(!account_exists(&env.svm, &Req::new(&env.requester.pubkey(), 1).request));
    assert_eq!(env.balance(&other_token), START_TOKENS);
}

#[test]
fn create_request_with_token_account_of_another_mint_fails() {
    let mut env = Env::setup();
    let other_mint = create_mint(&mut env.svm, &env.mint_authority);
    let other_token = create_token_account(&mut env.svm, &env.requester, &other_mint, &env.requester.pubkey());
    mint_to(&mut env.svm, &env.mint_authority, &other_mint, &other_token, START_TOKENS);

    let ix = ix_create_request(&env.requester.pubkey(), &env.mint, &other_token, &default_params(1));
    let res = send(&mut env.svm, &[ix], &env.requester, &[&env.requester]);
    assert_anchor_error(&res, AnchorError::ConstraintTokenMint);
    assert_eq!(env.balance(&other_token), START_TOKENS);
}

#[test]
fn create_request_with_someone_elses_token_account_fails() {
    let mut env = Env::setup();
    // the node's token account has the right mint and plenty of funds, but the requester does not own it
    let ix = ix_create_request(&env.requester.pubkey(), &env.mint, &env.node_token, &default_params(1));
    let res = send(&mut env.svm, &[ix], &env.requester, &[&env.requester]);
    assert_anchor_error(&res, AnchorError::ConstraintTokenOwner);
    assert_eq!(env.balance(&env.node_token), START_TOKENS);
}

#[test]
fn create_request_with_insufficient_balance_fails_and_creates_nothing() {
    let mut env = Env::setup();
    let mut p = default_params(1);
    p.price_per_frame = 200 * USDC; // 10 frames = 2000 USDC > the requester's 1000
    let res = env.create_request(&p);
    assert_fails(&res);
    assert!(!account_exists(&env.svm, &Req::new(&env.requester.pubkey(), 1).request));
    assert_eq!(env.balance(&env.requester_token), START_TOKENS);
}

#[test]
fn create_request_duplicate_id_fails_but_other_requesters_can_reuse_it() {
    let mut env = Env::setup();
    env.create_request(&default_params(5)).unwrap();
    let after_first = env.balance(&env.requester_token);
    assert_fails(&env.create_request(&default_params(5)));
    assert_eq!(env.balance(&env.requester_token), after_first);

    // a different request id from the same requester is fine
    env.create_request(&default_params(6)).unwrap();

    // the same id from a different requester is a different PDA
    let (other, other_token) = env.new_actor(START_TOKENS);
    let ix = ix_create_request(&other.pubkey(), &env.mint, &other_token, &default_params(5));
    send(&mut env.svm, &[ix], &other, &[&other]).unwrap();
    assert_eq!(env.request(&Req::new(&other.pubkey(), 5)).requester, other.pubkey());
}

#[test]
fn create_request_before_init_config_fails() {
    let mut env = Env::new(); // no init_config
    let res = env.create_request(&default_params(1));
    assert_anchor_error(&res, AnchorError::AccountNotInitialized);
    assert!(!account_exists(&env.svm, &Req::new(&env.requester.pubkey(), 1).request));
}

#[test]
fn create_request_requires_the_requester_signature() {
    let mut env = Env::setup();
    let mut ix = ix_create_request(&env.requester.pubkey(), &env.mint, &env.requester_token, &default_params(1));
    ix.accounts[0].is_signer = false; // the requester does not sign; someone else pays the fee
    let res = send(&mut env.svm, &[ix], &env.admin, &[&env.admin]);
    assert_fails(&res);
    assert_eq!(env.balance(&env.requester_token), START_TOKENS);
}
