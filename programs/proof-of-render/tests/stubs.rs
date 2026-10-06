mod common;
use anchor_lang::{solana_program::instruction::Instruction, InstructionData, ToAccountMetas};
use common::*;
use proof_of_render::{errors::PorError, state::Verdict};
use solana_signer::Signer;

fn ix<D: InstructionData, A: ToAccountMetas>(data: D, accounts: A) -> Instruction {
    Instruction::new_with_bytes(proof_of_render::id(), &data.data(), accounts.to_account_metas(None))
}

/// Instructions that are not implemented yet must fail loudly, never succeed silently.
#[test]
fn unimplemented_instructions_return_not_implemented() {
    let mut env = Env::setup();
    let req = env.open_request(1);
    let w = env.admin.pubkey();
    let r = req.request;
    let ixs = vec![
        ix(proof_of_render::instruction::SubmitCommitment { merkle_root: [0; 32], frame_count: 10 }, proof_of_render::accounts::SubmitCommitment { node: w, request: r }),
        ix(proof_of_render::instruction::OpenChallenge {}, proof_of_render::accounts::OpenChallenge { verifier: w, request: r }),
        ix(proof_of_render::instruction::SubmitVerdict { verdict: Verdict::Pass }, proof_of_render::accounts::SubmitVerdict { verifier: w, request: r }),
        ix(proof_of_render::instruction::FinalizePayout {}, proof_of_render::accounts::FinalizePayout { caller: w, request: r }),
        ix(proof_of_render::instruction::SlashAndRefund {}, proof_of_render::accounts::SlashAndRefund { caller: w, request: r }),
        ix(proof_of_render::instruction::TimeoutRefund {}, proof_of_render::accounts::TimeoutRefund { caller: w, request: r }),
    ];
    let before = env.snapshot(&req);
    for instruction in ixs {
        let res = send(&mut env.svm, &[instruction], &env.admin, &[&env.admin]);
        assert_por_error(&res, PorError::NotImplemented);
    }
    assert_eq!(env.snapshot(&req), before);
}
