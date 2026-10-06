//! Instructions that are part of the protocol but not implemented yet. Their account
//! contexts are minimal placeholders and will be finalised on Day 3 (commit) and Day 4
//! (challenge, verdict, payout, slash) and Day 5 (timeout refund).
use crate::{
    errors::PorError,
    state::{Request, Verdict},
};
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct SubmitCommitment<'info> {
    pub node: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

#[derive(Accounts)]
pub struct OpenChallenge<'info> {
    pub verifier: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

#[derive(Accounts)]
pub struct SubmitVerdict<'info> {
    pub verifier: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

#[derive(Accounts)]
pub struct FinalizePayout<'info> {
    pub caller: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

#[derive(Accounts)]
pub struct SlashAndRefund<'info> {
    pub caller: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

#[derive(Accounts)]
pub struct TimeoutRefund<'info> {
    pub caller: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

pub fn handle_submit_commitment(_ctx: Context<SubmitCommitment>, _merkle_root: [u8; 32], _frame_count: u32) -> Result<()> {
    err!(PorError::NotImplemented)
}
pub fn handle_open_challenge(_ctx: Context<OpenChallenge>) -> Result<()> {
    err!(PorError::NotImplemented)
}
pub fn handle_submit_verdict(_ctx: Context<SubmitVerdict>, _verdict: Verdict) -> Result<()> {
    err!(PorError::NotImplemented)
}
pub fn handle_finalize_payout(_ctx: Context<FinalizePayout>) -> Result<()> {
    err!(PorError::NotImplemented)
}
pub fn handle_slash_and_refund(_ctx: Context<SlashAndRefund>) -> Result<()> {
    err!(PorError::NotImplemented)
}
pub fn handle_timeout_refund(_ctx: Context<TimeoutRefund>) -> Result<()> {
    err!(PorError::NotImplemented)
}
