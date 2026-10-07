use super::payout::pay_all;
use crate::{
    constants::*,
    errors::PorError,
    state::{Request, RequestStatus, Submission, Verdict},
};
use anchor_lang::prelude::*;
use anchor_spl::token::{Mint, Token, TokenAccount};

#[derive(Accounts)]
/// Token accounts are boxed: unboxed, the generated deserializer overflows the 4 KB SBF stack frame.
pub struct FinalizePayout<'info> {
    /// Anyone may finalize; the signer only pays the fee.
    pub caller: Signer<'info>,
    #[account(
        mut,
        seeds = [REQUEST_SEED, request.requester.as_ref(), &request.request_id.to_le_bytes()],
        bump = request.bump
    )]
    pub request: Account<'info, Request>,
    #[account(address = request.mint @ PorError::WrongMint)]
    pub mint: Box<Account<'info, Mint>>,
    #[account(seeds = [SUBMISSION_SEED, request.key().as_ref()], bump = submission.bump)]
    pub submission: Account<'info, Submission>,
    #[account(mut, seeds = [ESCROW_SEED, request.key().as_ref()], bump, token::mint = mint, token::authority = request)]
    pub escrow: Box<Account<'info, TokenAccount>>,
    #[account(mut, seeds = [STAKE_SEED, request.key().as_ref()], bump, token::mint = mint, token::authority = request)]
    pub stake_vault: Box<Account<'info, TokenAccount>>,
    /// Must belong to the node that claimed the request.
    #[account(mut, token::mint = mint, token::authority = request.node)]
    pub node_token: Box<Account<'info, TokenAccount>>,
    pub token_program: Program<'info, Token>,
}

/// Escrow and stake go to the node. Allowed for a `Pass` verdict (status `Verified`) or a still-`Pending` verdict
/// (status `Challenged`, optimistic payout), once `now >= challenge_window_end`.
pub fn handle_finalize_payout(ctx: Context<FinalizePayout>) -> Result<()> {
    let request = &ctx.accounts.request;
    require!(
        matches!(request.status, RequestStatus::Challenged | RequestStatus::Verified),
        PorError::InvalidStatus
    );
    require!(ctx.accounts.submission.verdict != Verdict::Fail, PorError::VerdictIsFail);
    require!(Clock::get()?.unix_timestamp >= request.challenge_window_end, PorError::ChallengeWindowOpen);

    pay_all(
        &ctx.accounts.token_program,
        &ctx.accounts.request,
        &ctx.accounts.mint,
        &ctx.accounts.escrow,
        &ctx.accounts.stake_vault,
        &ctx.accounts.node_token,
    )?;
    ctx.accounts.request.status = RequestStatus::Paid;
    Ok(())
}
