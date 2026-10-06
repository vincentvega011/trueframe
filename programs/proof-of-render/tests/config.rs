mod common;
use anchor_lang::{error::ErrorCode as AnchorError, prelude::Pubkey};
use common::*;
use proof_of_render::errors::PorError;
use solana_keypair::Keypair;
use solana_signer::Signer;

#[test]
fn init_config_happy_path() {
    let mut env = Env::new();
    assert!(!account_exists(&env.svm, &config_pda()));
    env.init_config(2000).unwrap();
    let c = env.config();
    assert_eq!(c.admin, env.admin.pubkey());
    assert_eq!(c.verifier, env.verifier.pubkey());
    assert_eq!(c.mint, env.mint);
    assert_eq!(c.stake_bps, 2000);
    assert_eq!(c.bump, Pubkey::find_program_address(&[proof_of_render::constants::CONFIG_SEED], &proof_of_render::id()).1);
}

#[test]
fn init_config_twice_fails_and_leaves_state_unchanged() {
    let mut env = Env::new();
    env.init_config(2000).unwrap();
    let before = env.config();

    // identical call
    assert_fails(&env.init_config(2000));
    // a different admin trying to take over with another verifier and fee
    let attacker = Keypair::new();
    env.svm.airdrop(&attacker.pubkey(), 10 * SOL).unwrap();
    let ix = ix_init_config(&attacker.pubkey(), &env.mint, &attacker.pubkey(), 5000);
    assert_fails(&send(&mut env.svm, &[ix], &attacker, &[&attacker]));

    let after = env.config();
    assert_eq!((after.admin, after.verifier, after.stake_bps), (before.admin, before.verifier, before.stake_bps));
}

#[test]
fn init_config_stake_bps_bounds() {
    for bps in [0u16, 10_001, u16::MAX] {
        let mut env = Env::new();
        assert_por_error(&env.init_config(bps), PorError::InvalidStakeBps);
        assert!(!account_exists(&env.svm, &config_pda()));
    }
    for bps in [1u16, 10_000] {
        let mut env = Env::new();
        env.init_config(bps).unwrap();
        assert_eq!(env.config().stake_bps, bps);
    }
}

#[test]
fn init_config_rejects_a_non_mint_account() {
    let mut env = Env::new();
    // pass the admin's wallet (a system account) where the mint belongs
    let ix = ix_init_config(&env.admin.pubkey(), &env.admin.pubkey(), &env.verifier.pubkey(), 2000);
    let res = send(&mut env.svm, &[ix], &env.admin, &[&env.admin]);
    assert_anchor_error(&res, AnchorError::AccountOwnedByWrongProgram);
    assert!(!account_exists(&env.svm, &config_pda()));
}
