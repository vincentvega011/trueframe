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

/// PDA ["submission", request]. Created by `submit_commitment`; the sample fields are filled by `open_challenge`.
#[account]
#[derive(InitSpace)]
pub struct Submission {
    pub request: Pubkey,
    pub node: Pubkey,
    pub merkle_root: [u8; 32],
    pub frame_count: u32,
    pub commit_slot: u64,
    pub sample_seed: [u8; 32],
    /// Slot of the SlotHashes entry that fed `sample_seed` (0 until `open_challenge`), so anyone can audit the seed.
    pub sample_slot: u64,
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

/// How many frames the verifier samples for a request of `frame_count` frames (docs/PROTOCOL.md section 5):
/// 7 when there are at least 7 frames (0, N-1 and 5 drawn from the middle), otherwise min(5, N).
pub fn sample_count_for(frame_count: u32) -> u8 {
    use crate::constants::{SAMPLE_DRAWN, SAMPLE_EDGE_THRESHOLD};
    if frame_count >= SAMPLE_EDGE_THRESHOLD {
        SAMPLE_DRAWN + 2
    } else {
        u8::try_from(frame_count.min(u32::from(SAMPLE_DRAWN))).unwrap_or(SAMPLE_DRAWN)
    }
}

#[cfg(test)]
mod tests {
    use super::sample_count_for;

    #[test]
    fn sample_count_table() {
        let table = [(0u32, 0u8), (1, 1), (2, 2), (5, 5), (6, 5), (7, 7), (8, 7), (20, 7), (1024, 7), (u32::MAX, 7)];
        for (frames, expected) in table {
            assert_eq!(sample_count_for(frames), expected, "frame_count {frames}");
        }
    }
}
