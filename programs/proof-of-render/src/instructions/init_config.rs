use crate::{constants::*, errors::PorError, state::Config};
use anchor_lang::prelude::*;
use anchor_spl::token::Mint;

#[derive(Accounts)]
pub struct InitConfig<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    #[account(
        init,
        payer = admin,
        space = 8 + Config::INIT_SPACE,
        seeds = [CONFIG_SEED],
        bump
    )]
    pub config: Account<'info, Config>,
    pub mint: Account<'info, Mint>,
    pub system_program: Program<'info, System>,
}

pub fn handle_init_config(ctx: Context<InitConfig>, verifier: Pubkey, stake_bps: u16) -> Result<()> {
    require!(
        stake_bps >= 1 && u64::from(stake_bps) <= BPS_DENOMINATOR,
        PorError::InvalidStakeBps
    );
    let config = &mut ctx.accounts.config;
    config.admin = ctx.accounts.admin.key();
    config.verifier = verifier;
    config.mint = ctx.accounts.mint.key();
    config.stake_bps = stake_bps;
    config.bump = ctx.bumps.config;
    Ok(())
}
