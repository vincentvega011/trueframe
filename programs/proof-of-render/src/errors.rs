use anchor_lang::prelude::*;

#[error_code]
pub enum PorError {
    // ---- config / request parameters ----
    #[msg("stake_bps must be between 1 and 10000")]
    InvalidStakeBps,
    #[msg("num_frames must be at least 1")]
    ZeroFrames,
    #[msg("num_frames exceeds the maximum of 1024")]
    TooManyFrames,
    #[msg("price_per_frame must be greater than zero")]
    ZeroPrice,
    #[msg("all durations must be greater than zero")]
    InvalidDuration,
    #[msg("occlusion_level must be 0..=3")]
    InvalidOcclusionLevel,
    #[msg("only scene_type 0 (warehouse_v1) is supported")]
    InvalidSceneType,
    #[msg("width and height must be at least 1")]
    InvalidResolution,
    #[msg("width and height must not exceed 4096")]
    ResolutionTooLarge,
    #[msg("computed stake rounds down to zero")]
    ZeroStake,
    #[msg("arithmetic overflow")]
    MathOverflow,

    // ---- accounts ----
    #[msg("mint account does not match the configured mint")]
    WrongMint,

    // ---- state machine ----
    #[msg("request is not in the status required by this instruction")]
    InvalidStatus,

    // ---- roles (used from Day 3/4) ----
    #[msg("signer is not the configured verifier")]
    UnauthorizedVerifier,
    #[msg("signer is not the node that claimed this request")]
    UnauthorizedNode,

    // ---- deadlines and windows ----
    #[msg("claim deadline has passed")]
    ClaimDeadlinePassed,
    #[msg("commit deadline has passed")]
    CommitDeadlinePassed,
    #[msg("deadline has not been reached yet")]
    DeadlineNotReached,
    #[msg("challenge window has not ended yet")]
    ChallengeWindowOpen,
    #[msg("challenge window has ended")]
    ChallengeWindowClosed,

    // ---- commit / challenge / verdict (used from Day 3/4) ----
    #[msg("frame_count does not equal the request's num_frames")]
    FrameCountMismatch,
    #[msg("SlotHashes sysvar is empty or malformed")]
    SlotHashesUnavailable,
    #[msg("most recent slot hash is not newer than the commit slot")]
    SlotHashNotNewer,
    #[msg("a verdict has already been recorded")]
    VerdictAlreadySet,
    #[msg("slash requires a Fail verdict")]
    VerdictNotFail,
    #[msg("payout is not allowed after a Fail verdict")]
    VerdictIsFail,

    // ---- placeholder ----
    #[msg("instruction not implemented yet")]
    NotImplemented,
}
