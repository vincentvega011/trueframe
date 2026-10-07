use anchor_lang::prelude::*;

// PDA seeds. Spec: docs/PROTOCOL.md section 2 and 10.
#[constant]
pub const CONFIG_SEED: &[u8] = b"config";
#[constant]
pub const REQUEST_SEED: &[u8] = b"request";
#[constant]
pub const ESCROW_SEED: &[u8] = b"escrow";
#[constant]
pub const STAKE_SEED: &[u8] = b"stake";
#[constant]
pub const SUBMISSION_SEED: &[u8] = b"submission";

#[constant]
pub const BPS_DENOMINATOR: u64 = 10_000;

// Request parameter bounds.
#[constant]
pub const SCENE_WAREHOUSE_V1: u8 = 0;
#[constant]
pub const MAX_OCCLUSION_LEVEL: u8 = 3;
#[constant]
pub const MAX_FRAMES: u32 = 1024;
#[constant]
pub const MAX_RESOLUTION: u16 = 4096;

// Sampling (docs/PROTOCOL.md section 5): with at least 7 frames, frames 0 and N-1 plus 5 drawn from 1..N-2.
#[constant]
pub const SAMPLE_EDGE_THRESHOLD: u32 = 7;
#[constant]
pub const SAMPLE_DRAWN: u8 = 5;
