mod common;
use anchor_lang::{solana_program::instruction::Instruction, InstructionData, ToAccountMetas};
use common::*;
use proof_of_render::errors::PorError;
use solana_signer::Signer;

/// Instructions that are not implemented yet must fail loudly, never succeed silently.
#[test]
fn timeout_refund_is_still_a_stub_and_fails_loudly() {
    let mut env = Env::setup();
    let req = env.open_request(1);
    let ix = Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::TimeoutRefund {}.data(),
        proof_of_render::accounts::TimeoutRefund { caller: env.admin.pubkey(), request: req.request }.to_account_metas(None),
    );
    let before = env.snapshot(&req);
    let res = send(&mut env.svm, &[ix], &env.admin, &[&env.admin]);
    assert_por_error(&res, PorError::NotImplemented);
    assert_eq!(env.snapshot(&req), before);
}
