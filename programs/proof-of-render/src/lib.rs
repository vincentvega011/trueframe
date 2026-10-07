pub mod constants;
pub mod errors;
pub mod instructions;
pub mod slot_hashes;
pub mod state;

use anchor_lang::prelude::*;

pub use constants::*;
pub use instructions::*;
pub use state::*;

declare_id!("7jhnpAu8H9rtDhEyXRmFCicjGLUepXoafE4SiE7EvDLw");

#[program]
pub mod proof_of_render {
    use super::*;

    pub fn init_config(ctx: Context<InitConfig>, verifier: Pubkey, stake_bps: u16) -> Result<()> {
        instructions::init_config::handle_init_config(ctx, verifier, stake_bps)
    }

    pub fn create_request(ctx: Context<CreateRequest>, params: CreateRequestParams) -> Result<()> {
        instructions::create_request::handle_create_request(ctx, params)
    }

    pub fn stake_and_claim(ctx: Context<StakeAndClaim>) -> Result<()> {
        instructions::stake_and_claim::handle_stake_and_claim(ctx)
    }

    pub fn submit_commitment(ctx: Context<SubmitCommitment>, merkle_root: [u8; 32], frame_count: u32) -> Result<()> {
        instructions::submit_commitment::handle_submit_commitment(ctx, merkle_root, frame_count)
    }

    pub fn open_challenge(ctx: Context<OpenChallenge>) -> Result<()> {
        instructions::open_challenge::handle_open_challenge(ctx)
    }

    pub fn submit_verdict(ctx: Context<SubmitVerdict>, verdict: Verdict) -> Result<()> {
        instructions::submit_verdict::handle_submit_verdict(ctx, verdict)
    }

    pub fn finalize_payout(ctx: Context<FinalizePayout>) -> Result<()> {
        instructions::finalize_payout::handle_finalize_payout(ctx)
    }

    pub fn slash_and_refund(ctx: Context<SlashAndRefund>) -> Result<()> {
        instructions::slash_and_refund::handle_slash_and_refund(ctx)
    }

    // ---- not implemented yet (see instructions/stubs.rs)

    pub fn timeout_refund(ctx: Context<TimeoutRefund>) -> Result<()> {
        instructions::stubs::handle_timeout_refund(ctx)
    }
}
