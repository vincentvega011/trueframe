use crate::{
    constants::*,
    errors::PorError,
    state::{Config, Request, RequestStatus},
};
use anchor_lang::prelude::*;
use anchor_spl::token::{transfer_checked, Mint, Token, TokenAccount, TransferChecked};

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug)]
pub struct CreateRequestParams {
    pub request_id: u64,
    pub scene_type: u8,
    pub seed: u64,
    pub num_frames: u32,
    pub width: u16,
    pub height: u16,
    pub occlusion_level: u8,
    pub price_per_frame: u64,
    pub claim_duration: i64,
    pub commit_duration: i64,
    pub challenge_window_secs: i64,
}

#[derive(Accounts)]
#[instruction(params: CreateRequestParams)]
pub struct CreateRequest<'info> {
    #[account(mut)]
    pub requester: Signer<'info>,
    #[account(seeds = [CONFIG_SEED], bump = config.bump)]
    pub config: Account<'info, Config>,
    #[account(address = config.mint @ PorError::WrongMint)]
    pub mint: Account<'info, Mint>,
    #[account(
        init,
        payer = requester,
        space = 8 + Request::INIT_SPACE,
        seeds = [REQUEST_SEED, requester.key().as_ref(), &params.request_id.to_le_bytes()],
        bump
    )]
    pub request: Account<'info, Request>,
    /// Escrow vault. The Request PDA is its authority.
    #[account(
        init,
        payer = requester,
        token::mint = mint,
        token::authority = request,
        seeds = [ESCROW_SEED, request.key().as_ref()],
        bump
    )]
    pub escrow: Account<'info, TokenAccount>,
    /// Stake vault (empty until `stake_and_claim`). The Request PDA is its authority.
    #[account(
        init,
        payer = requester,
        token::mint = mint,
        token::authority = request,
        seeds = [STAKE_SEED, request.key().as_ref()],
        bump
    )]
    pub stake_vault: Account<'info, TokenAccount>,
    #[account(mut, token::mint = mint, token::authority = requester)]
    pub requester_token: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
    pub system_program: Program<'info, System>,
}

pub fn handle_create_request(ctx: Context<CreateRequest>, p: CreateRequestParams) -> Result<()> {
    require!(p.num_frames >= 1, PorError::ZeroFrames);
    require!(p.num_frames <= MAX_FRAMES, PorError::TooManyFrames);
    require!(p.price_per_frame > 0, PorError::ZeroPrice);
    require!(p.scene_type == SCENE_WAREHOUSE_V1, PorError::InvalidSceneType);
    require!(p.occlusion_level <= MAX_OCCLUSION_LEVEL, PorError::InvalidOcclusionLevel);
    require!(p.width >= 1 && p.height >= 1, PorError::InvalidResolution);
    require!(
        p.width <= MAX_RESOLUTION && p.height <= MAX_RESOLUTION,
        PorError::ResolutionTooLarge
    );
    require!(
        p.claim_duration > 0 && p.commit_duration > 0 && p.challenge_window_secs > 0,
        PorError::InvalidDuration
    );

    let escrow_amount = p
        .price_per_frame
        .checked_mul(u64::from(p.num_frames))
        .ok_or(PorError::MathOverflow)?;
    let stake_amount = escrow_amount
        .checked_mul(u64::from(ctx.accounts.config.stake_bps))
        .ok_or(PorError::MathOverflow)?
        .checked_div(BPS_DENOMINATOR)
        .ok_or(PorError::MathOverflow)?;
    require!(stake_amount > 0, PorError::ZeroStake);

    // Reject durations whose later deadlines (claim, commit, challenge window) could overflow, so a
    // request can never become stuck at a later instruction because of an addition overflow.
    let now = Clock::get()?.unix_timestamp;
    let total_span = p
        .claim_duration
        .checked_add(p.commit_duration)
        .and_then(|v| v.checked_add(p.challenge_window_secs))
        .ok_or(PorError::MathOverflow)?;
    now.checked_add(total_span).ok_or(PorError::MathOverflow)?;
    let claim_deadline = now.checked_add(p.claim_duration).ok_or(PorError::MathOverflow)?;

    let decimals = ctx.accounts.mint.decimals;
    transfer_checked(
        CpiContext::new(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.requester_token.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.escrow.to_account_info(),
                authority: ctx.accounts.requester.to_account_info(),
            },
        ),
        escrow_amount,
        decimals,
    )?;

    let request = &mut ctx.accounts.request;
    request.requester = ctx.accounts.requester.key();
    request.request_id = p.request_id;
    request.mint = ctx.accounts.mint.key();
    request.scene_type = p.scene_type;
    request.seed = p.seed;
    request.num_frames = p.num_frames;
    request.width = p.width;
    request.height = p.height;
    request.occlusion_level = p.occlusion_level;
    request.price_per_frame = p.price_per_frame;
    request.escrow_amount = escrow_amount;
    request.stake_amount = stake_amount;
    request.claim_duration = p.claim_duration;
    request.commit_duration = p.commit_duration;
    request.challenge_window_secs = p.challenge_window_secs;
    request.node = Pubkey::default();
    request.status = RequestStatus::Open;
    request.created_at = now;
    request.claimed_at = 0;
    request.claim_deadline = claim_deadline;
    request.commit_deadline = 0;
    request.challenge_window_end = 0;
    request.bump = ctx.bumps.request;
    Ok(())
}
