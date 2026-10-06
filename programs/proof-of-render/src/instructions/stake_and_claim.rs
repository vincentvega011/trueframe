use crate::{
    constants::*,
    errors::PorError,
    state::{Config, Request, RequestStatus},
};
use anchor_lang::prelude::*;
use anchor_spl::token::{transfer_checked, Mint, Token, TokenAccount, TransferChecked};

#[derive(Accounts)]
pub struct StakeAndClaim<'info> {
    #[account(mut)]
    pub node: Signer<'info>,
    #[account(seeds = [CONFIG_SEED], bump = config.bump)]
    pub config: Account<'info, Config>,
    #[account(address = config.mint @ PorError::WrongMint)]
    pub mint: Account<'info, Mint>,
    #[account(
        mut,
        seeds = [REQUEST_SEED, request.requester.as_ref(), &request.request_id.to_le_bytes()],
        bump = request.bump,
        constraint = request.mint == mint.key() @ PorError::WrongMint
    )]
    pub request: Account<'info, Request>,
    #[account(
        mut,
        seeds = [STAKE_SEED, request.key().as_ref()],
        bump,
        token::mint = mint,
        token::authority = request
    )]
    pub stake_vault: Account<'info, TokenAccount>,
    #[account(mut, token::mint = mint, token::authority = node)]
    pub node_token: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

pub fn handle_stake_and_claim(ctx: Context<StakeAndClaim>) -> Result<()> {
    let request = &ctx.accounts.request;
    require!(request.status == RequestStatus::Open, PorError::InvalidStatus);
    let now = Clock::get()?.unix_timestamp;
    // claim is allowed while now <= claim_deadline; timeout_refund only when now > claim_deadline
    require!(now <= request.claim_deadline, PorError::ClaimDeadlinePassed);
    let commit_deadline = now
        .checked_add(request.commit_duration)
        .ok_or(PorError::MathOverflow)?;
    let stake_amount = request.stake_amount;

    transfer_checked(
        CpiContext::new(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.node_token.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.stake_vault.to_account_info(),
                authority: ctx.accounts.node.to_account_info(),
            },
        ),
        stake_amount,
        ctx.accounts.mint.decimals,
    )?;

    let request = &mut ctx.accounts.request;
    request.node = ctx.accounts.node.key();
    request.claimed_at = now;
    request.commit_deadline = commit_deadline;
    request.status = RequestStatus::Claimed;
    Ok(())
}
