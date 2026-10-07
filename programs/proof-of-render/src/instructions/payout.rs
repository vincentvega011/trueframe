//! Shared by `finalize_payout` and `slash_and_refund`: move the recorded escrow and stake to one destination,
//! signing as the Request PDA (which owns both vaults).
use crate::{constants::REQUEST_SEED, state::Request};
use anchor_lang::prelude::*;
use anchor_spl::token::{transfer_checked, Mint, Token, TokenAccount, TransferChecked};

pub fn pay_all<'info>(
    token_program: &Program<'info, Token>,
    request: &Account<'info, Request>,
    mint: &Account<'info, Mint>,
    escrow: &Account<'info, TokenAccount>,
    stake_vault: &Account<'info, TokenAccount>,
    destination: &Account<'info, TokenAccount>,
) -> Result<()> {
    let request_id = request.request_id.to_le_bytes();
    let bump = [request.bump];
    let seeds: &[&[u8]] = &[REQUEST_SEED, request.requester.as_ref(), &request_id, &bump];
    for (from, amount) in [(escrow, request.escrow_amount), (stake_vault, request.stake_amount)] {
        transfer_checked(
            CpiContext::new_with_signer(
                token_program.key(),
                TransferChecked {
                    from: from.to_account_info(),
                    mint: mint.to_account_info(),
                    to: destination.to_account_info(),
                    authority: request.to_account_info(),
                },
                &[seeds],
            ),
            amount,
            mint.decimals,
        )?;
    }
    Ok(())
}
