// One command per implemented instruction, account fetches, and dev utilities for localnet setup.
import { AnchorProvider, BN, Idl, Program, Wallet } from "@anchor-lang/core";
import {
  getAssociatedTokenAddressSync,
  getOrCreateAssociatedTokenAccount,
  createMint,
  mintTo,
  TOKEN_PROGRAM_ID,
} from "@solana/spl-token";
import { Connection, Keypair, LAMPORTS_PER_SOL, PublicKey, SYSVAR_SLOT_HASHES_PUBKEY, SystemProgram } from "@solana/web3.js";
import {
  CliError, Pdas, coerceStruct, hexToBytes32, idlPath, loadIdl, loadKeypair, need, pubkey, rpcUrl, toJson,
} from "./util";

type Args = Record<string, unknown>;
type Result = Record<string, unknown>;

export class Ctx {
  readonly idl: Idl = loadIdl();
  readonly pdas = new Pdas(this.idl);
  readonly connection = new Connection(rpcUrl(), "confirmed");

  program(signer?: Keypair): Program<Idl> {
    const wallet = new Wallet(signer ?? Keypair.generate());
    const provider = new AnchorProvider(this.connection, wallet, { commitment: "confirmed", preflightCommitment: "confirmed" });
    return new Program(this.idl, provider);
  }
  signer(args: Args, field = "signer"): Keypair {
    return loadKeypair(String(need(args, field)));
  }
  async config(): Promise<any> {
    const acct = await (this.program().account as any).config.fetchNullable(this.pdas.config());
    if (!acct) throw new CliError("AccountNotFound", "config account does not exist; run init-config first");
    return acct;
  }
  async mint(args: Args): Promise<PublicKey> {
    return args.mint ? pubkey(args.mint, "mint") : (await this.config()).mint;
  }
  request(args: Args): PublicKey {
    if (args.request) return pubkey(args.request, "request");
    const requester = pubkey(need(args, "requester"), "requester");
    return this.pdas.request(requester, new BN(String(need(args, "request_id")), 10));
  }
}

async function send(builder: any): Promise<string> {
  return builder.rpc({ commitment: "confirmed" });
}

// ---------------------------------------------------------------- instructions

async function initConfig(ctx: Ctx, args: Args): Promise<Result> {
  const admin = ctx.signer(args);
  const mint = pubkey(need(args, "mint"), "mint");
  const stakeBps = Number(need(args, "stake_bps"));
  const sig = await send(
    ctx.program(admin).methods
      .initConfig(pubkey(need(args, "verifier"), "verifier"), stakeBps)
      .accountsPartial({ admin: admin.publicKey, config: ctx.pdas.config(), mint, systemProgram: SystemProgram.programId }),
  );
  return { signature: sig, config: ctx.pdas.config().toBase58() };
}

async function createRequest(ctx: Ctx, args: Args): Promise<Result> {
  const requester = ctx.signer(args);
  const mint = await ctx.mint(args);
  const params = coerceStruct(ctx.idl, "CreateRequestParams", need<Args>(args, "params"));
  const request = ctx.pdas.request(requester.publicKey, params.requestId as BN);
  const requesterToken = args.requester_token
    ? pubkey(args.requester_token, "requester_token")
    : getAssociatedTokenAddressSync(mint, requester.publicKey);
  const sig = await send(
    ctx.program(requester).methods.createRequest(params).accountsPartial({
      requester: requester.publicKey,
      config: ctx.pdas.config(),
      mint,
      request,
      escrow: ctx.pdas.escrow(request),
      stakeVault: ctx.pdas.stake(request),
      requesterToken,
      tokenProgram: TOKEN_PROGRAM_ID,
      systemProgram: SystemProgram.programId,
    }),
  );
  return { signature: sig, request: request.toBase58(), escrow: ctx.pdas.escrow(request).toBase58(), stake_vault: ctx.pdas.stake(request).toBase58() };
}

async function stakeAndClaim(ctx: Ctx, args: Args): Promise<Result> {
  const node = ctx.signer(args);
  const mint = await ctx.mint(args);
  const request = ctx.request(args);
  const nodeToken = args.node_token ? pubkey(args.node_token, "node_token") : getAssociatedTokenAddressSync(mint, node.publicKey);
  const sig = await send(
    ctx.program(node).methods.stakeAndClaim().accountsPartial({
      node: node.publicKey,
      config: ctx.pdas.config(),
      mint,
      request,
      stakeVault: ctx.pdas.stake(request),
      nodeToken,
      tokenProgram: TOKEN_PROGRAM_ID,
    }),
  );
  return { signature: sig, request: request.toBase58() };
}

async function submitCommitment(ctx: Ctx, args: Args): Promise<Result> {
  const node = ctx.signer(args);
  const request = ctx.request(args);
  const root = hexToBytes32(need(args, "merkle_root"), "merkle_root");
  const sig = await send(
    ctx.program(node).methods.submitCommitment(root, Number(need(args, "frame_count"))).accountsPartial({
      node: node.publicKey,
      request,
      submission: ctx.pdas.submission(request),
      systemProgram: SystemProgram.programId,
    }),
  );
  return { signature: sig, request: request.toBase58(), submission: ctx.pdas.submission(request).toBase58() };
}

/** Request account as stored on chain (needed to find the mint and the payee), or AccountNotFound. */
async function fetchRequest(ctx: Ctx, request: PublicKey): Promise<any> {
  const acct = await (ctx.program().account as any).request.fetchNullable(request);
  if (!acct) throw new CliError("AccountNotFound", `request account ${request.toBase58()} does not exist`);
  return acct;
}

/** "Pass" / "pass" -> {pass: {}}, validated against the IDL's own Verdict enum rather than a hardcoded list. */
function verdictArg(ctx: Ctx, raw: unknown): Record<string, object> {
  const def = (ctx.idl.types ?? []).find((t) => t.name === "Verdict");
  const variants = ((def?.type as unknown as { variants?: { name: string }[] })?.variants ?? []).map((v) => v.name);
  const match = variants.find((v) => v.toLowerCase() === String(raw).toLowerCase());
  if (!match) throw new CliError("InvalidInput", `verdict must be one of ${variants.join(", ")}, got ${JSON.stringify(raw)}`);
  return { [match.charAt(0).toLowerCase() + match.slice(1)]: {} };
}

async function openChallenge(ctx: Ctx, args: Args): Promise<Result> {
  const verifier = ctx.signer(args);
  const request = ctx.request(args);
  const sig = await send(
    ctx.program(verifier).methods.openChallenge().accountsPartial({
      verifier: verifier.publicKey,
      config: ctx.pdas.config(),
      request,
      submission: ctx.pdas.submission(request),
      slotHashes: SYSVAR_SLOT_HASHES_PUBKEY,
    }),
  );
  return { signature: sig, request: request.toBase58() };
}

async function submitVerdict(ctx: Ctx, args: Args): Promise<Result> {
  const verifier = ctx.signer(args);
  const request = ctx.request(args);
  const sig = await send(
    ctx.program(verifier).methods.submitVerdict(verdictArg(ctx, need(args, "verdict"))).accountsPartial({
      verifier: verifier.publicKey,
      config: ctx.pdas.config(),
      request,
      submission: ctx.pdas.submission(request),
    }),
  );
  return { signature: sig, request: request.toBase58() };
}

async function finalizePayout(ctx: Ctx, args: Args): Promise<Result> {
  const caller = ctx.signer(args);
  const request = ctx.request(args);
  const acct = await fetchRequest(ctx, request);
  const nodeToken = args.node_token ? pubkey(args.node_token, "node_token") : getAssociatedTokenAddressSync(acct.mint, acct.node);
  const sig = await send(
    ctx.program(caller).methods.finalizePayout().accountsPartial({
      caller: caller.publicKey,
      request,
      mint: args.mint ? pubkey(args.mint, "mint") : acct.mint,
      submission: ctx.pdas.submission(request),
      escrow: ctx.pdas.escrow(request),
      stakeVault: ctx.pdas.stake(request),
      nodeToken,
      tokenProgram: TOKEN_PROGRAM_ID,
    }),
  );
  return { signature: sig, request: request.toBase58(), node_token: nodeToken.toBase58() };
}

async function slashAndRefund(ctx: Ctx, args: Args): Promise<Result> {
  const caller = ctx.signer(args);
  const request = ctx.request(args);
  const acct = await fetchRequest(ctx, request);
  const requesterToken = args.requester_token
    ? pubkey(args.requester_token, "requester_token")
    : getAssociatedTokenAddressSync(acct.mint, acct.requester);
  const sig = await send(
    ctx.program(caller).methods.slashAndRefund().accountsPartial({
      caller: caller.publicKey,
      request,
      mint: args.mint ? pubkey(args.mint, "mint") : acct.mint,
      submission: ctx.pdas.submission(request),
      escrow: ctx.pdas.escrow(request),
      stakeVault: ctx.pdas.stake(request),
      requesterToken,
      tokenProgram: TOKEN_PROGRAM_ID,
    }),
  );
  return { signature: sig, request: request.toBase58(), requester_token: requesterToken.toBase58() };
}

// ---------------------------------------------------------------- fetches

async function getConfig(ctx: Ctx): Promise<Result> {
  return { address: ctx.pdas.config().toBase58(), account: toJson(await ctx.config()) } as Result;
}

async function tokenAmount(ctx: Ctx, address: PublicKey): Promise<string | null> {
  try {
    return (await ctx.connection.getTokenAccountBalance(address)).value.amount;
  } catch {
    return null;
  }
}

async function getRequest(ctx: Ctx, args: Args): Promise<Result> {
  const address = ctx.request(args);
  const acct = await (ctx.program().account as any).request.fetchNullable(address);
  if (!acct) throw new CliError("AccountNotFound", `request account ${address.toBase58()} does not exist`);
  const escrow = ctx.pdas.escrow(address), stake = ctx.pdas.stake(address);
  return {
    address: address.toBase58(),
    escrow: escrow.toBase58(),
    stake_vault: stake.toBase58(),
    submission: ctx.pdas.submission(address).toBase58(),
    escrow_balance: await tokenAmount(ctx, escrow),
    stake_vault_balance: await tokenAmount(ctx, stake),
    account: toJson(acct),
  };
}

async function getSubmission(ctx: Ctx, args: Args): Promise<Result> {
  const request = ctx.request(args);
  const address = ctx.pdas.submission(request);
  const acct = await (ctx.program().account as any).submission.fetchNullable(address);
  if (!acct) throw new CliError("AccountNotFound", `no submission for request ${request.toBase58()}`);
  return { address: address.toBase58(), account: toJson(acct) };
}

async function listRequests(ctx: Ctx, args: Args): Promise<Result> {
  const wantStatus = args.status ? String(args.status).toLowerCase() : undefined;
  const wantRequester = args.requester ? pubkey(args.requester, "requester").toBase58() : undefined;
  const all = await (ctx.program().account as any).request.all();
  const rows = all
    .map((r: any) => ({ address: r.publicKey.toBase58(), account: toJson(r.account) as Record<string, any> }))
    .filter((r: any) => (wantStatus ? String(r.account.status).toLowerCase() === wantStatus : true))
    .filter((r: any) => (wantRequester ? r.account.requester === wantRequester : true))
    .sort((a: any, b: any) => Number(a.account.created_at) - Number(b.account.created_at) || a.address.localeCompare(b.address));
  return { count: rows.length, requests: rows };
}

// ---------------------------------------------------------------- dev utilities (localnet setup and tests)

async function createMintCmd(ctx: Ctx, args: Args): Promise<Result> {
  const payer = ctx.signer(args);
  const mintKp = args.mint_keypair ? loadKeypair(String(args.mint_keypair)) : Keypair.generate();
  const existing = await ctx.connection.getAccountInfo(mintKp.publicKey);
  if (existing) return { mint: mintKp.publicKey.toBase58(), created: false };
  const mint = await createMint(ctx.connection, payer, payer.publicKey, null, Number(args.decimals ?? 6), mintKp);
  return { mint: mint.toBase58(), created: true };
}

async function createTokenAccount(ctx: Ctx, args: Args): Promise<Result> {
  const payer = ctx.signer(args);
  const owner = args.owner ? pubkey(args.owner, "owner") : payer.publicKey;
  const acct = await getOrCreateAssociatedTokenAccount(ctx.connection, payer, pubkey(need(args, "mint"), "mint"), owner);
  return { token_account: acct.address.toBase58(), owner: owner.toBase58() };
}

async function mintToCmd(ctx: Ctx, args: Args): Promise<Result> {
  const authority = ctx.signer(args);
  const mint = pubkey(need(args, "mint"), "mint");
  const dest = args.token_account
    ? pubkey(args.token_account, "token_account")
    : getAssociatedTokenAddressSync(mint, pubkey(need(args, "owner"), "owner"));
  const sig = await mintTo(ctx.connection, authority, mint, dest, authority, BigInt(String(need(args, "amount"))));
  return { signature: sig, token_account: dest.toBase58() };
}

async function tokenBalance(ctx: Ctx, args: Args): Promise<Result> {
  const address = args.token_account
    ? pubkey(args.token_account, "token_account")
    : getAssociatedTokenAddressSync(pubkey(need(args, "mint"), "mint"), pubkey(need(args, "owner"), "owner"));
  const amount = await tokenAmount(ctx, address);
  if (amount === null) throw new CliError("AccountNotFound", `token account ${address.toBase58()} does not exist`);
  return { token_account: address.toBase58(), amount };
}

async function airdrop(ctx: Ctx, args: Args): Promise<Result> {
  const to = pubkey(need(args, "to"), "to");
  const lamports = Math.round(Number(need(args, "sol")) * LAMPORTS_PER_SOL);
  const sig = await ctx.connection.requestAirdrop(to, lamports);
  const latest = await ctx.connection.getLatestBlockhash();
  await ctx.connection.confirmTransaction({ signature: sig, ...latest }, "confirmed");
  return { signature: sig, lamports };
}

async function pubkeyCmd(ctx: Ctx, args: Args): Promise<Result> {
  return { pubkey: ctx.signer(args).publicKey.toBase58() };
}

async function info(ctx: Ctx): Promise<Result> {
  return {
    rpc_url: rpcUrl(),
    program_id: ctx.pdas.programId.toBase58(),
    idl_path: idlPath(),
    config_pda: ctx.pdas.config().toBase58(),
    instructions: ctx.idl.instructions.map((i) => i.name),
    errors: (ctx.idl.errors ?? []).length,
  };
}

export const COMMANDS: Record<string, (ctx: Ctx, args: Args) => Promise<Result>> = {
  "init-config": initConfig,
  "create-request": createRequest,
  "stake-and-claim": stakeAndClaim,
  "submit-commitment": submitCommitment,
  "open-challenge": openChallenge,
  "submit-verdict": submitVerdict,
  "finalize-payout": finalizePayout,
  "slash-and-refund": slashAndRefund,
  "get-config": (ctx) => getConfig(ctx),
  "get-request": getRequest,
  "get-submission": getSubmission,
  "list-requests": listRequests,
  "create-mint": createMintCmd,
  "create-token-account": createTokenAccount,
  "mint-to": mintToCmd,
  "token-balance": tokenBalance,
  airdrop,
  pubkey: pubkeyCmd,
  info: (ctx) => info(ctx),
};
