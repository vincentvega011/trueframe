use crate::{
    constants::*,
    errors::PorError,
    state::{Config, Request, RequestStatus, Submission, Verdict},
};
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct SubmitVerdict<'info> {
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
}

/// `Pass` moves the request to `Verified`; `Fail` records the verdict and leaves the status `Challenged`
/// (docs/PROTOCOL.md section 1 and 6). Check order: status, verdict argument, verdict not yet set, window still open.
pub fn handle_submit_verdict(ctx: Context<SubmitVerdict>, verdict: Verdict) -> Result<()> {
    require!(ctx.accounts.request.status == RequestStatus::Challenged, PorError::InvalidStatus);
    require!(verdict != Verdict::Pending, PorError::InvalidVerdict);
    require!(ctx.accounts.submission.verdict == Verdict::Pending, PorError::VerdictAlreadySet);
    let now = Clock::get()?.unix_timestamp;
    require!(now < ctx.accounts.request.challenge_window_end, PorError::ChallengeWindowClosed);

    ctx.accounts.submission.verdict = verdict;
    if verdict == Verdict::Pass {
        ctx.accounts.request.status = RequestStatus::Verified;
    }
    Ok(())
}
