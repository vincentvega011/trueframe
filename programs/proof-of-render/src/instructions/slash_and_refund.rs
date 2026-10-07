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
pub struct SlashAndRefund<'info> {
    /// Anyone may slash once the verdict is `Fail`; the signer only pays the fee.
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
    /// Must belong to the requester: the node's stake is slashed to the requester as well (no treasury).
    #[account(mut, token::mint = mint, token::authority = request.requester)]
    pub requester_token: Box<Account<'info, TokenAccount>>,
    pub token_program: Program<'info, Token>,
}

/// Escrow and stake both go to the requester. Check order: verdict is `Fail` (so a slash on `Pass` or `Pending`
/// says `VerdictNotFail`), then status is `Challenged` (so a second slash says `InvalidStatus`).
pub fn handle_slash_and_refund(ctx: Context<SlashAndRefund>) -> Result<()> {
    require!(ctx.accounts.submission.verdict == Verdict::Fail, PorError::VerdictNotFail);
    require!(ctx.accounts.request.status == RequestStatus::Challenged, PorError::InvalidStatus);

    pay_all(
        &ctx.accounts.token_program,
        &ctx.accounts.request,
        &ctx.accounts.mint,
        &ctx.accounts.escrow,
        &ctx.accounts.stake_vault,
        &ctx.accounts.requester_token,
    )?;
    ctx.accounts.request.status = RequestStatus::Slashed;
    Ok(())
}
