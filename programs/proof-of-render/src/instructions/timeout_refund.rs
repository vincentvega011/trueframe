use super::payout::{pay_all, pay_escrow};
use crate::{
    constants::*,
    errors::PorError,
    state::{Request, RequestStatus},
};
use anchor_lang::prelude::*;
use anchor_spl::token::{Mint, Token, TokenAccount};

#[derive(Accounts)]
/// Token accounts are boxed: unboxed, the generated deserializer overflows the 4 KB SBF stack frame.
pub struct TimeoutRefund<'info> {
    /// Anyone may call once the deadline has passed; the signer only pays the fee.
    pub caller: Signer<'info>,
    #[account(
        mut,
        seeds = [REQUEST_SEED, request.requester.as_ref(), &request.request_id.to_le_bytes()],
        bump = request.bump
    )]
    pub request: Account<'info, Request>,
    #[account(address = request.mint @ PorError::WrongMint)]
    pub mint: Box<Account<'info, Mint>>,
    #[account(mut, seeds = [ESCROW_SEED, request.key().as_ref()], bump, token::mint = mint, token::authority = request)]
    pub escrow: Box<Account<'info, TokenAccount>>,
    #[account(mut, seeds = [STAKE_SEED, request.key().as_ref()], bump, token::mint = mint, token::authority = request)]
    pub stake_vault: Box<Account<'info, TokenAccount>>,
    /// Must belong to the requester.
    #[account(mut, token::mint = mint, token::authority = request.requester)]
    pub requester_token: Box<Account<'info, TokenAccount>>,
    pub token_program: Program<'info, Token>,
}

/// `Open` past `claim_deadline`: the escrow goes back to the requester (no stake was ever deposited).
/// `Claimed` past `commit_deadline`: the escrow AND the node's stake go to the requester (the node is penalised for not
/// committing). Both only when `now > deadline`, so this can never succeed in the same second as the claim or the commit
/// it replaces (those are allowed while `now <= deadline`). Any other status is `InvalidStatus` (check order: status first,
/// then the deadline), so a second refund, or a refund of a committed request, says so. The status becomes `Expired`.
pub fn handle_timeout_refund(mut ctx: Context<TimeoutRefund>) -> Result<()> {
    let now = Clock::get()?.unix_timestamp;
    let accounts = &mut ctx.accounts;
    match accounts.request.status {
        RequestStatus::Open => {
            require!(now > accounts.request.claim_deadline, PorError::DeadlineNotReached);
            pay_escrow(&accounts.token_program, &accounts.request, &accounts.mint, &accounts.escrow, &accounts.requester_token)?;
        }
        RequestStatus::Claimed => {
            require!(now > accounts.request.commit_deadline, PorError::DeadlineNotReached);
            pay_all(
                &accounts.token_program,
                &accounts.request,
                &accounts.mint,
                &accounts.escrow,
                &accounts.stake_vault,
                &accounts.requester_token,
            )?;
        }
        _ => return err!(PorError::InvalidStatus),
    }
    accounts.request.status = RequestStatus::Expired;
    Ok(())
}
