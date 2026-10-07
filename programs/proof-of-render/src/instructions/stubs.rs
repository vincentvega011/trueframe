//! Instructions that are part of the protocol but not implemented yet. The account context is a minimal
//! placeholder and will be finalised on Day 5 (timeout refund).
use crate::{errors::PorError, state::Request};
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct TimeoutRefund<'info> {
    pub caller: Signer<'info>,
    #[account(mut)]
    pub request: Account<'info, Request>,
}

pub fn handle_timeout_refund(_ctx: Context<TimeoutRefund>) -> Result<()> {
    err!(PorError::NotImplemented)
}
