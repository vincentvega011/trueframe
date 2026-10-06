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
