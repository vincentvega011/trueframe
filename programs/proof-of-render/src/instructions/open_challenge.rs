use crate::{
    constants::*,
    errors::PorError,
    slot_hashes::newest_slot_hash,
    state::{sample_count_for, Config, Request, RequestStatus, Submission},
};
use anchor_lang::{prelude::*, solana_program::sysvar::SysvarId};
use solana_sha256_hasher::hashv;

#[derive(Accounts)]
pub struct OpenChallenge<'info> {
    pub verifier: Signer<'info>,
    #[account(
        seeds = [CONFIG_SEED],
        bump = config.bump,
        constraint = config.verifier == verifier.key() @ PorError::UnauthorizedVerifier
    )]
    pub config: Account<'info, Config>,
    #[account(
        mut,
        seeds = [REQUEST_SEED, request.requester.as_ref(), &request.request_id.to_le_bytes()],
        bump = request.bump
    )]
    pub request: Account<'info, Request>,
    #[account(mut, seeds = [SUBMISSION_SEED, request.key().as_ref()], bump = submission.bump)]
    pub submission: Account<'info, Submission>,
    /// CHECK: constrained to the SlotHashes sysvar address; only the newest entry is parsed from the raw bytes
    /// (deserializing the whole sysvar is not supported on-chain and would be wasteful).
    #[account(address = SlotHashes::id())]
    pub slot_hashes: UncheckedAccount<'info>,
}

pub fn handle_open_challenge(ctx: Context<OpenChallenge>) -> Result<()> {
    require!(ctx.accounts.request.status == RequestStatus::Committed, PorError::InvalidStatus);

    // The newest SlotHashes entry is the PREVIOUS slot (the current slot's hash does not exist yet), so a
    // challenge opened in the same slot as the commit fails here and the verifier retries a slot later.
    let (slot, slot_hash) = {
        let data = ctx.accounts.slot_hashes.try_borrow_data()?;
        newest_slot_hash(&data)?
    };
    let submission = &mut ctx.accounts.submission;
    require!(slot > submission.commit_slot, PorError::SlotHashNotNewer);

    let now = Clock::get()?.unix_timestamp;
    let window_end = now
        .checked_add(ctx.accounts.request.challenge_window_secs)
        .ok_or(PorError::MathOverflow)?;

    submission.sample_seed = hashv(&[&submission.merkle_root, &slot_hash]).to_bytes();
    submission.sample_slot = slot;
    submission.sample_count = sample_count_for(submission.frame_count);

    let request = &mut ctx.accounts.request;
    request.challenge_window_end = window_end;
    request.status = RequestStatus::Challenged;
    Ok(())
}
