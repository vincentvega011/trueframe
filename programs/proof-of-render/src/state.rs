use anchor_lang::prelude::*;

/// PDA ["config"]. Singleton created by `init_config`.
#[account]
#[derive(InitSpace)]
pub struct Config {
    pub admin: Pubkey,
    pub verifier: Pubkey,
    pub mint: Pubkey,
    pub stake_bps: u16,
    pub bump: u8,
}

/// PDA ["request", requester, request_id_le]. Also the authority of the escrow and stake vaults.
#[account]
#[derive(InitSpace)]
pub struct Request {
    pub requester: Pubkey,
    pub request_id: u64,
    pub mint: Pubkey,
    pub scene_type: u8,
    pub seed: u64,
    pub num_frames: u32,
    pub width: u16,
    pub height: u16,
    pub occlusion_level: u8,
    pub price_per_frame: u64,
    pub escrow_amount: u64,
    pub stake_amount: u64,
    pub claim_duration: i64,
    pub commit_duration: i64,
    pub challenge_window_secs: i64,
    pub node: Pubkey,
    pub status: RequestStatus,
    pub created_at: i64,
    pub claimed_at: i64,
    pub claim_deadline: i64,
    pub commit_deadline: i64,
    pub challenge_window_end: i64,
    pub bump: u8,
}

/// PDA ["submission", request]. Created by `submit_commitment` (Day 3).
#[account]
#[derive(InitSpace)]
pub struct Submission {
    pub request: Pubkey,
    pub node: Pubkey,
    pub merkle_root: [u8; 32],
    pub frame_count: u32,
    pub commit_slot: u64,
    pub sample_seed: [u8; 32],
    pub sample_count: u8,
    pub verdict: Verdict,
    pub bump: u8,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug, InitSpace)]
pub enum RequestStatus {
    Open,
    Claimed,
    Committed,
    Challenged,
    Verified,
    Paid,
    Slashed,
    Expired,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug, InitSpace)]
pub enum Verdict {
    Pending,
    Pass,
    Fail,
}
