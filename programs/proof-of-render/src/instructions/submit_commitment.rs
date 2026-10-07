use crate::{
    constants::*,
    errors::PorError,
    state::{Request, RequestStatus, Submission, Verdict},
};
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct SubmitCommitment<'info> {
    /// Must be the node that claimed the request. Pays the Submission's rent.
    #[account(mut)]
    pub node: Signer<'info>,
    #[account(
        mut,
        seeds = [REQUEST_SEED, request.requester.as_ref(), &request.request_id.to_le_bytes()],
        bump = request.bump,
        constraint = request.node == node.key() @ PorError::UnauthorizedNode
    )]
    pub request: Account<'info, Request>,
    // `init` means a second commit fails here, at account creation ("already in use"),
    // before the handler runs, so it surfaces as a system error rather than InvalidStatus.
    #[account(
        init,
        payer = node,
        space = 8 + Submission::INIT_SPACE,
        seeds = [SUBMISSION_SEED, request.key().as_ref()],
        bump
    )]
    pub submission: Account<'info, Submission>,
    pub system_program: Program<'info, System>,
}

pub fn handle_submit_commitment(ctx: Context<SubmitCommitment>, merkle_root: [u8; 32], frame_count: u32) -> Result<()> {
    let request = &ctx.accounts.request;
    require!(request.status == RequestStatus::Claimed, PorError::InvalidStatus);
    let clock = Clock::get()?;
    // commit is allowed while now <= commit_deadline; timeout_refund only when now > commit_deadline
    require!(clock.unix_timestamp <= request.commit_deadline, PorError::CommitDeadlinePassed);
    require!(frame_count == request.num_frames, PorError::FrameCountMismatch);
    require!(merkle_root != [0u8; 32], PorError::ZeroMerkleRoot);

    let submission = &mut ctx.accounts.submission;
    submission.request = ctx.accounts.request.key();
    submission.node = ctx.accounts.node.key();
    submission.merkle_root = merkle_root;
    submission.frame_count = frame_count;
    submission.commit_slot = clock.slot;
    submission.sample_seed = [0u8; 32];
    submission.sample_slot = 0;
    submission.sample_count = 0;
    submission.verdict = Verdict::Pending;
    submission.bump = ctx.bumps.submission;

    ctx.accounts.request.status = RequestStatus::Committed;
    Ok(())
}
