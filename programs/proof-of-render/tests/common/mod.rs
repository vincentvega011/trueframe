//! Shared LiteSVM harness: program loading, mock-USDC mint, token accounts, instruction
//! builders, balance/state readers, clock warping and error assertions.
#![allow(dead_code)]

use {
    anchor_lang::{
        prelude::Pubkey,
        solana_program::{clock::Clock, instruction::Instruction, program_pack::Pack, system_instruction, system_program, sysvar::SysvarId},
        prelude::SlotHashes,
        AccountDeserialize, AccountSerialize, InstructionData, ToAccountMetas,
    },
    anchor_spl::token::spl_token,
    litesvm::{
        types::{FailedTransactionMetadata, TransactionMetadata},
        LiteSVM,
    },
    proof_of_render::{
        errors::PorError,
        state::{Config, Request, RequestStatus, Submission, Verdict},
        CreateRequestParams,
    },
    solana_keypair::Keypair,
    solana_message::{Message, VersionedMessage},
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
};

pub type TxResult = Result<TransactionMetadata, FailedTransactionMetadata>;

pub const BASE_TIME: i64 = 1_700_000_000;
pub const DECIMALS: u8 = 6;
pub const USDC: u64 = 1_000_000;
pub const START_TOKENS: u64 = 1_000 * USDC; // requester and node each start with 1000 mock USDC
pub const SOL: u64 = 1_000_000_000;

/// A fixed commitment root (the worked-example leaf from docs/PROTOCOL.md section 4.2) and a fixed slot hash.
pub const ROOT: [u8; 32] = [
    0x5b, 0x4f, 0xaa, 0x09, 0xef, 0x2b, 0xcb, 0x0f, 0x4a, 0x44, 0x48, 0x89, 0xda, 0x86, 0x95, 0x2d, 0xb0, 0x53, 0x4a, 0x0f, 0x1e,
    0x6c, 0xf2, 0x09, 0xb8, 0x1e, 0x41, 0x1e, 0x5d, 0xe5, 0x3f, 0xd4,
];
pub const SLOT_HASH: [u8; 32] = [0xAB; 32];
/// sha256(ROOT || SLOT_HASH), computed independently with Python's hashlib.
pub const EXPECTED_SEED: &str = "a77fb2584ba6b48a1f11e25e0b05b6cade9c11f2f7adfade76e5766edd32f4d0";

/// Timeline used by `Env::challenged_request`: claimed at BASE+10, committed at slot COMMIT_SLOT,
/// challenge opened at BASE+20, so the 30 s window ends at WINDOW_END.
pub const COMMIT_SLOT: u64 = 100;
pub const OPEN_TIME: i64 = BASE_TIME + 20;
pub const WINDOW_END: i64 = OPEN_TIME + 30;

// ---------------------------------------------------------------- PDAs

pub fn config_pda() -> Pubkey {
    Pubkey::find_program_address(&[proof_of_render::constants::CONFIG_SEED], &proof_of_render::id()).0
}
pub fn request_pda(requester: &Pubkey, request_id: u64) -> Pubkey {
    Pubkey::find_program_address(
        &[proof_of_render::constants::REQUEST_SEED, requester.as_ref(), &request_id.to_le_bytes()],
        &proof_of_render::id(),
    )
    .0
}
pub fn submission_pda(request: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[proof_of_render::constants::SUBMISSION_SEED, request.as_ref()], &proof_of_render::id()).0
}
pub fn escrow_pda(request: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[proof_of_render::constants::ESCROW_SEED, request.as_ref()], &proof_of_render::id()).0
}
pub fn stake_pda(request: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[proof_of_render::constants::STAKE_SEED, request.as_ref()], &proof_of_render::id()).0
}

// ---------------------------------------------------------------- environment

pub struct Env {
    pub svm: LiteSVM,
    pub admin: Keypair,
    pub verifier: Keypair,
    pub mint_authority: Keypair,
    pub requester: Keypair,
    pub node: Keypair,
    pub mint: Pubkey,
    pub requester_token: Pubkey,
    pub node_token: Pubkey,
}

impl Env {
    /// Program loaded, clock at BASE_TIME, funded actors, mock mint, requester and node each
    /// holding START_TOKENS. `init_config` has NOT been called.
    pub fn new() -> Env {
        let mut svm = LiteSVM::new();
        let bytes = include_bytes!(concat!(env!("CARGO_TARGET_TMPDIR"), "/../deploy/proof_of_render.so"));
        svm.add_program(proof_of_render::id(), bytes).unwrap();
        let mut clock = svm.get_sysvar::<Clock>();
        clock.unix_timestamp = BASE_TIME;
        svm.set_sysvar(&clock);

        let (admin, verifier, mint_authority, requester, node) =
            (Keypair::new(), Keypair::new(), Keypair::new(), Keypair::new(), Keypair::new());
        for k in [&admin, &verifier, &mint_authority, &requester, &node] {
            svm.airdrop(&k.pubkey(), 100 * SOL).unwrap();
        }
        let mint = create_mint(&mut svm, &mint_authority);
        let requester_token = create_token_account(&mut svm, &requester, &mint, &requester.pubkey());
        let node_token = create_token_account(&mut svm, &node, &mint, &node.pubkey());
        mint_to(&mut svm, &mint_authority, &mint, &requester_token, START_TOKENS);
        mint_to(&mut svm, &mint_authority, &mint, &node_token, START_TOKENS);
        Env { svm, admin, verifier, mint_authority, requester, node, mint, requester_token, node_token }
    }

    /// `new()` plus a successful `init_config` (stake_bps = 2000).
    pub fn setup() -> Env {
        let mut env = Env::new();
        env.init_config(2000).unwrap();
        env
    }

    pub fn init_config(&mut self, stake_bps: u16) -> TxResult {
        let ix = ix_init_config(&self.admin.pubkey(), &self.mint, &self.verifier.pubkey(), stake_bps);
        send(&mut self.svm, &[ix], &self.admin, &[&self.admin])
    }

    /// create_request as the default requester with the default token account.
    pub fn create_request(&mut self, p: &CreateRequestParams) -> TxResult {
        let ix = ix_create_request(&self.requester.pubkey(), &self.mint, &self.requester_token, p);
        send(&mut self.svm, &[ix], &self.requester, &[&self.requester])
    }

    /// Creates a request that must succeed and returns its addresses.
    pub fn open_request(&mut self, request_id: u64) -> Req {
        self.create_request(&default_params(request_id)).unwrap();
        Req::new(&self.requester.pubkey(), request_id)
    }

    /// stake_and_claim as the default node.
    pub fn claim(&mut self, req: &Req) -> TxResult {
        let ix = ix_stake_and_claim(&self.node.pubkey(), &self.mint, req, &self.node_token);
        send(&mut self.svm, &[ix], &self.node, &[&self.node])
    }

    /// An open request (id) claimed by the default node 10 s after creation. Clock is left at BASE_TIME + 10.
    pub fn claimed_request(&mut self, request_id: u64) -> Req {
        let req = self.open_request(request_id);
        self.warp_to(BASE_TIME + 10);
        self.claim(&req).unwrap();
        req
    }

    /// submit_commitment as the default node.
    pub fn commit(&mut self, req: &Req, root: [u8; 32], frame_count: u32) -> TxResult {
        let ix = ix_submit_commitment(&self.node.pubkey(), req, root, frame_count);
        send(&mut self.svm, &[ix], &self.node, &[&self.node])
    }

    pub fn submission(&self, req: &Req) -> Submission {
        read_anchor::<Submission>(&self.svm, &submission_pda(&req.request))
    }

    pub fn warp_to_slot(&mut self, slot: u64) {
        let mut clock = self.svm.get_sysvar::<Clock>();
        clock.slot = slot;
        self.svm.set_sysvar(&clock);
    }

    /// Replace the SlotHashes sysvar with exactly these (slot, hash) entries, newest first as given.
    pub fn set_slot_hashes(&mut self, entries: &[(u64, [u8; 32])]) {
        let mut data = (entries.len() as u64).to_le_bytes().to_vec();
        for (slot, hash) in entries {
            data.extend_from_slice(&slot.to_le_bytes());
            data.extend_from_slice(hash);
        }
        let mut account = self.svm.get_account(&SlotHashes::id()).expect("SlotHashes sysvar account exists");
        account.data = data;
        self.svm.set_account(SlotHashes::id(), account).unwrap();
    }

    /// Claimed at BASE+10 and committed with ROOT at slot COMMIT_SLOT. `params` decides the frame count.
    pub fn committed_request_with(&mut self, params: &CreateRequestParams) -> Req {
        self.create_request(params).unwrap();
        let req = Req::new(&self.requester.pubkey(), params.request_id);
        self.warp_to(BASE_TIME + 10);
        self.claim(&req).unwrap();
        self.warp_to_slot(COMMIT_SLOT);
        self.commit(&req, ROOT, params.num_frames).unwrap();
        req
    }

    pub fn committed_request(&mut self, request_id: u64) -> Req {
        self.committed_request_with(&default_params(request_id))
    }

    /// Make the newest SlotHashes entry (COMMIT_SLOT + 49, SLOT_HASH) newer than the commit, at OPEN_TIME.
    pub fn prepare_open_challenge(&mut self) {
        self.warp_to(OPEN_TIME);
        self.warp_to_slot(COMMIT_SLOT + 50);
        self.set_slot_hashes(&[(COMMIT_SLOT + 49, SLOT_HASH), (COMMIT_SLOT + 48, [0x11; 32]), (3, [0x22; 32])]);
    }

    pub fn open_challenge(&mut self, req: &Req) -> TxResult {
        let ix = ix_open_challenge(&self.verifier.pubkey(), req);
        send(&mut self.svm, &[ix], &self.verifier, &[&self.verifier])
    }

    /// Committed, then challenged by the verifier at OPEN_TIME. The window ends at WINDOW_END.
    pub fn challenged_request_with(&mut self, params: &CreateRequestParams) -> Req {
        let req = self.committed_request_with(params);
        self.prepare_open_challenge();
        self.open_challenge(&req).unwrap();
        req
    }

    pub fn challenged_request(&mut self, request_id: u64) -> Req {
        self.challenged_request_with(&default_params(request_id))
    }

    pub fn verdict(&mut self, req: &Req, verdict: Verdict) -> TxResult {
        let ix = ix_submit_verdict(&self.verifier.pubkey(), req, verdict);
        send(&mut self.svm, &[ix], &self.verifier, &[&self.verifier])
    }

    /// finalize_payout signed by a stranger (anyone may call it), paying the node's own token account.
    pub fn payout(&mut self, req: &Req) -> TxResult {
        let ix = ix_finalize_payout(&self.admin.pubkey(), &self.mint, req, &self.node_token);
        send(&mut self.svm, &[ix], &self.admin, &[&self.admin])
    }

    /// slash_and_refund signed by a stranger, refunding the requester's own token account.
    pub fn slash(&mut self, req: &Req) -> TxResult {
        let ix = ix_slash_and_refund(&self.admin.pubkey(), &self.mint, req, &self.requester_token);
        send(&mut self.svm, &[ix], &self.admin, &[&self.admin])
    }

    /// A funded second actor holding `tokens` mock USDC, as (keypair, token account).
    pub fn new_actor(&mut self, tokens: u64) -> (Keypair, Pubkey) {
        let k = Keypair::new();
        self.svm.airdrop(&k.pubkey(), 100 * SOL).unwrap();
        let ta = create_token_account(&mut self.svm, &k, &self.mint, &k.pubkey());
        if tokens > 0 {
            mint_to(&mut self.svm, &self.mint_authority, &self.mint, &ta, tokens);
        }
        (k, ta)
    }

    pub fn now(&self) -> i64 {
        self.svm.get_sysvar::<Clock>().unix_timestamp
    }
    pub fn warp_to(&mut self, unix_timestamp: i64) {
        let mut clock = self.svm.get_sysvar::<Clock>();
        clock.unix_timestamp = unix_timestamp;
        self.svm.set_sysvar(&clock);
    }

    pub fn request(&self, req: &Req) -> Request {
        read_anchor::<Request>(&self.svm, &req.request)
    }
    pub fn config(&self) -> Config {
        read_anchor::<Config>(&self.svm, &config_pda())
    }
    pub fn balance(&self, token_account: &Pubkey) -> u64 {
        token_balance(&self.svm, token_account)
    }
    /// Overwrite a request's status directly (used to reach states later instructions will create).
    pub fn patch_status(&mut self, req: &Req, status: RequestStatus) {
        let mut request = self.request(req);
        request.status = status;
        let mut account = self.svm.get_account(&req.request).unwrap();
        let mut data = Vec::new();
        request.try_serialize(&mut data).unwrap();
        account.data = data;
        self.svm.set_account(req.request, account).unwrap();
    }
    pub fn snapshot(&self, req: &Req) -> Balances {
        Balances {
            requester: self.balance(&self.requester_token),
            node: self.balance(&self.node_token),
            escrow: self.balance(&req.escrow),
            stake: self.balance(&req.stake_vault),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Balances {
    pub requester: u64,
    pub node: u64,
    pub escrow: u64,
    pub stake: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct Req {
    pub request: Pubkey,
    pub escrow: Pubkey,
    pub stake_vault: Pubkey,
}
impl Req {
    pub fn new(requester: &Pubkey, request_id: u64) -> Req {
        let request = request_pda(requester, request_id);
        Req { request, escrow: escrow_pda(&request), stake_vault: stake_pda(&request) }
    }
}

/// 10 frames x 1 USDC = 10 USDC escrow; with stake_bps 2000 the stake is 2 USDC.
pub fn default_params(request_id: u64) -> CreateRequestParams {
    CreateRequestParams {
        request_id,
        scene_type: 0,
        seed: 42,
        num_frames: 10,
        width: 640,
        height: 480,
        occlusion_level: 3,
        price_per_frame: USDC,
        claim_duration: 120,
        commit_duration: 600,
        challenge_window_secs: 30,
    }
}

// ---------------------------------------------------------------- transactions

pub fn send(svm: &mut LiteSVM, ixs: &[Instruction], payer: &Keypair, signers: &[&Keypair]) -> TxResult {
    let msg = Message::new_with_blockhash(ixs, Some(&payer.pubkey()), &svm.latest_blockhash());
    let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), signers).unwrap();
    let res = svm.send_transaction(tx);
    svm.expire_blockhash(); // so an identical follow-up transaction is not rejected as a duplicate
    res
}

// ---------------------------------------------------------------- instruction builders

pub fn ix_init_config(admin: &Pubkey, mint: &Pubkey, verifier: &Pubkey, stake_bps: u16) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::InitConfig { verifier: *verifier, stake_bps }.data(),
        proof_of_render::accounts::InitConfig {
            admin: *admin,
            config: config_pda(),
            mint: *mint,
            system_program: system_program::ID,
        }
        .to_account_metas(None),
    )
}

pub fn ix_create_request(requester: &Pubkey, mint: &Pubkey, requester_token: &Pubkey, p: &CreateRequestParams) -> Instruction {
    let req = Req::new(requester, p.request_id);
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::CreateRequest { params: p.clone() }.data(),
        proof_of_render::accounts::CreateRequest {
            requester: *requester,
            config: config_pda(),
            mint: *mint,
            request: req.request,
            escrow: req.escrow,
            stake_vault: req.stake_vault,
            requester_token: *requester_token,
            token_program: spl_token::ID,
            system_program: system_program::ID,
        }
        .to_account_metas(None),
    )
}

pub fn ix_stake_and_claim(node: &Pubkey, mint: &Pubkey, req: &Req, node_token: &Pubkey) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::StakeAndClaim {}.data(),
        proof_of_render::accounts::StakeAndClaim {
            node: *node,
            config: config_pda(),
            mint: *mint,
            request: req.request,
            stake_vault: req.stake_vault,
            node_token: *node_token,
            token_program: spl_token::ID,
        }
        .to_account_metas(None),
    )
}

pub fn ix_submit_commitment(node: &Pubkey, req: &Req, merkle_root: [u8; 32], frame_count: u32) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::SubmitCommitment { merkle_root, frame_count }.data(),
        proof_of_render::accounts::SubmitCommitment {
            node: *node,
            request: req.request,
            submission: submission_pda(&req.request),
            system_program: system_program::ID,
        }
        .to_account_metas(None),
    )
}

pub fn ix_open_challenge(verifier: &Pubkey, req: &Req) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::OpenChallenge {}.data(),
        proof_of_render::accounts::OpenChallenge {
            verifier: *verifier,
            config: config_pda(),
            request: req.request,
            submission: submission_pda(&req.request),
            slot_hashes: SlotHashes::id(),
        }
        .to_account_metas(None),
    )
}

pub fn ix_submit_verdict(verifier: &Pubkey, req: &Req, verdict: Verdict) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::SubmitVerdict { verdict }.data(),
        proof_of_render::accounts::SubmitVerdict {
            verifier: *verifier,
            config: config_pda(),
            request: req.request,
            submission: submission_pda(&req.request),
        }
        .to_account_metas(None),
    )
}

pub fn ix_finalize_payout(caller: &Pubkey, mint: &Pubkey, req: &Req, node_token: &Pubkey) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::FinalizePayout {}.data(),
        proof_of_render::accounts::FinalizePayout {
            caller: *caller,
            request: req.request,
            mint: *mint,
            submission: submission_pda(&req.request),
            escrow: req.escrow,
            stake_vault: req.stake_vault,
            node_token: *node_token,
            token_program: spl_token::ID,
        }
        .to_account_metas(None),
    )
}

pub fn ix_slash_and_refund(caller: &Pubkey, mint: &Pubkey, req: &Req, requester_token: &Pubkey) -> Instruction {
    Instruction::new_with_bytes(
        proof_of_render::id(),
        &proof_of_render::instruction::SlashAndRefund {}.data(),
        proof_of_render::accounts::SlashAndRefund {
            caller: *caller,
            request: req.request,
            mint: *mint,
            submission: submission_pda(&req.request),
            escrow: req.escrow,
            stake_vault: req.stake_vault,
            requester_token: *requester_token,
            token_program: spl_token::ID,
        }
        .to_account_metas(None),
    )
}

// ---------------------------------------------------------------- token helpers

pub fn create_mint(svm: &mut LiteSVM, authority: &Keypair) -> Pubkey {
    let mint = Keypair::new();
    let rent = svm.minimum_balance_for_rent_exemption(spl_token::state::Mint::LEN);
    let ixs = [
        system_instruction::create_account(&authority.pubkey(), &mint.pubkey(), rent, spl_token::state::Mint::LEN as u64, &spl_token::ID),
        spl_token::instruction::initialize_mint2(&spl_token::ID, &mint.pubkey(), &authority.pubkey(), None, DECIMALS).unwrap(),
    ];
    send(svm, &ixs, authority, &[authority, &mint]).unwrap();
    mint.pubkey()
}

pub fn create_token_account(svm: &mut LiteSVM, payer: &Keypair, mint: &Pubkey, owner: &Pubkey) -> Pubkey {
    let account = Keypair::new();
    let rent = svm.minimum_balance_for_rent_exemption(spl_token::state::Account::LEN);
    let ixs = [
        system_instruction::create_account(&payer.pubkey(), &account.pubkey(), rent, spl_token::state::Account::LEN as u64, &spl_token::ID),
        spl_token::instruction::initialize_account3(&spl_token::ID, &account.pubkey(), mint, owner).unwrap(),
    ];
    send(svm, &ixs, payer, &[payer, &account]).unwrap();
    account.pubkey()
}

pub fn mint_to(svm: &mut LiteSVM, authority: &Keypair, mint: &Pubkey, dest: &Pubkey, amount: u64) {
    let ix = spl_token::instruction::mint_to(&spl_token::ID, mint, dest, &authority.pubkey(), &[], amount).unwrap();
    send(svm, &[ix], authority, &[authority]).unwrap();
}

/// SPL token account layout: mint (32) | owner (32) | amount (8, LE) | ...
pub fn token_balance(svm: &LiteSVM, token_account: &Pubkey) -> u64 {
    let data = svm.get_account(token_account).expect("token account exists").data;
    u64::from_le_bytes(data[64..72].try_into().unwrap())
}
pub fn token_mint(svm: &LiteSVM, token_account: &Pubkey) -> Pubkey {
    Pubkey::try_from(&svm.get_account(token_account).unwrap().data[0..32]).unwrap()
}
pub fn token_owner(svm: &LiteSVM, token_account: &Pubkey) -> Pubkey {
    Pubkey::try_from(&svm.get_account(token_account).unwrap().data[32..64]).unwrap()
}

pub fn read_anchor<T: AccountDeserialize>(svm: &LiteSVM, address: &Pubkey) -> T {
    let account = svm.get_account(address).unwrap_or_else(|| panic!("account {address} does not exist"));
    let mut data: &[u8] = &account.data;
    T::try_deserialize(&mut data).unwrap()
}

pub fn to_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn account_exists(svm: &LiteSVM, address: &Pubkey) -> bool {
    svm.get_account(address).map(|a| a.lamports > 0).unwrap_or(false)
}

// ---------------------------------------------------------------- assertions

fn failure_text(res: &TxResult) -> String {
    match res {
        Ok(_) => panic!("expected the transaction to fail, but it succeeded"),
        Err(e) => format!("{:?}\nlogs: {:#?}", e.err, e.meta.logs),
    }
}

/// The transaction failed with exactly this program error.
pub fn assert_por_error(res: &TxResult, expected: PorError) {
    let code: u32 = expected.into();
    let text = failure_text(res);
    assert!(text.contains(&format!("Custom({code})")), "expected {expected:?} (Custom({code})) but got:\n{text}");
}

/// The transaction failed with exactly this built-in Anchor error (constraint, signer, ...).
pub fn assert_anchor_error(res: &TxResult, expected: anchor_lang::error::ErrorCode) {
    let code: u32 = expected.into();
    let text = failure_text(res);
    assert!(text.contains(&format!("Custom({code})")), "expected {expected:?} (Custom({code})) but got:\n{text}");
}

pub fn assert_fails(res: &TxResult) {
    let _ = failure_text(res);
}
