import { Buffer } from "buffer";
import { AnchorProvider, BN, Program, type Idl } from "@anchor-lang/core";
import { getAssociatedTokenAddressSync, TOKEN_PROGRAM_ID } from "@solana/spl-token";
import { Connection, PublicKey, SystemProgram, Transaction, type TransactionInstruction } from "@solana/web3.js";
import idlJson from "./idl/proof_of_render.json";
import { CLUSTER, DEFAULTS, DEVNET_GENESIS, RPC_HOST, RPC_URL } from "./config";
import { explainError, type Explained } from "./errors";
import { instructionFromLogs, type RequestView, type SubmissionView, type TxRecord } from "./timeline";

export const idl = idlJson as unknown as Idl;
export const PROGRAM_ID = new PublicKey((idlJson as { address: string }).address);
export const connection = new Connection(RPC_URL, "confirmed");

/** "embedded" is reserved for Phantom Connect (Google/Apple sign-in). It is not implemented: Phantom Portal is not issuing new App IDs (docs/LIMITATIONS.md). */
export type WalletKind = "burner" | "extension" | "embedded";

/** Everything the rest of the app needs from a wallet. Views and chain code see only this, never a wallet library. */
export interface AppWallet {
  kind: WalletKind;
  name: string;
  publicKey: PublicKey;
  signTransaction: (tx: Transaction) => Promise<Transaction>;
}

const seeds = {
  config: () => PublicKey.findProgramAddressSync([new TextEncoder().encode("config")], PROGRAM_ID)[0],
  request: (requester: PublicKey, id: BN) =>
    PublicKey.findProgramAddressSync([new TextEncoder().encode("request"), requester.toBytes(), id.toArrayLike(Buffer, "le", 8)], PROGRAM_ID)[0],
  escrow: (request: PublicKey) => PublicKey.findProgramAddressSync([new TextEncoder().encode("escrow"), request.toBytes()], PROGRAM_ID)[0],
  stake: (request: PublicKey) => PublicKey.findProgramAddressSync([new TextEncoder().encode("stake"), request.toBytes()], PROGRAM_ID)[0],
  submission: (request: PublicKey) => PublicKey.findProgramAddressSync([new TextEncoder().encode("submission"), request.toBytes()], PROGRAM_ID)[0],
};
export const pdas = seeds;

/** A read-only program handle (no wallet needed to look at accounts). */
function readProgram(): Program {
  const provider = new AnchorProvider(connection, { publicKey: PublicKey.default, signTransaction: async (t: never) => t, signAllTransactions: async (t: never) => t } as never, { commitment: "confirmed" });
  return new Program(idl, provider);
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const accounts = () => readProgram().account as any;

const num = (v: { toNumber(): number }) => v.toNumber();
const hex = (b: number[] | Uint8Array) => Array.from(b).map((x) => x.toString(16).padStart(2, "0")).join("");

export interface RequestAccount extends RequestView {
  address: string;
  requester: string;
  requestId: string;
  mint: string;
  seed: string;
  numFrames: number;
  occlusionLevel: number;
  pricePerFrame: string;
  escrowAmount: string;
  stakeAmount: string;
  node: string;
  challengeWindowSecs: number;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toRequest(address: PublicKey, a: any): RequestAccount {
  return {
    address: address.toBase58(),
    requester: a.requester.toBase58(),
    requestId: a.requestId.toString(),
    mint: a.mint.toBase58(),
    seed: a.seed.toString(),
    numFrames: a.numFrames,
    occlusionLevel: a.occlusionLevel,
    pricePerFrame: a.pricePerFrame.toString(),
    escrowAmount: a.escrowAmount.toString(),
    stakeAmount: a.stakeAmount.toString(),
    node: a.node.toBase58(),
    challengeWindowSecs: num(a.challengeWindowSecs),
    status: Object.keys(a.status)[0].replace(/^./, (c) => c.toUpperCase()),
    createdAt: num(a.createdAt),
    claimedAt: num(a.claimedAt),
    claimDeadline: num(a.claimDeadline),
    commitDeadline: num(a.commitDeadline),
    challengeWindowEnd: num(a.challengeWindowEnd),
  };
}

export async function listRequests(): Promise<RequestAccount[]> {
  const all = await accounts().request.all();
  return all.map((r: { publicKey: PublicKey; account: unknown }) => toRequest(r.publicKey, r.account)).sort((x: RequestAccount, y: RequestAccount) => y.createdAt - x.createdAt);
}

export async function getRequest(address: string): Promise<RequestAccount | null> {
  const pk = new PublicKey(address);
  const a = await accounts().request.fetchNullable(pk);
  return a ? toRequest(pk, a) : null;
}

export async function getSubmission(request: string): Promise<SubmissionView | null> {
  const a = await accounts().submission.fetchNullable(seeds.submission(new PublicKey(request)));
  if (!a) return null;
  const seedHex = hex(a.sampleSeed);
  return {
    merkleRoot: hex(a.merkleRoot),
    commitSlot: num(a.commitSlot),
    sampleSeed: /^0+$/.test(seedHex) ? null : seedHex,
    sampleSlot: num(a.sampleSlot),
    sampleCount: a.sampleCount,
    verdict: Object.keys(a.verdict)[0].replace(/^./, (c) => c.toUpperCase()),
  };
}

const txCache = new Map<string, TxRecord>();

/** Successful transactions that touched the request account, with the instruction each one ran. A confirmed transaction never
 *  changes, so each one is fetched once: a poll costs one signatures call plus one call per NEW transaction (public devnet RPCs rate-limit). */
export async function getTransactions(request: string): Promise<TxRecord[]> {
  const sigs = (await connection.getSignaturesForAddress(new PublicKey(request), { limit: 50 })).filter((s) => !s.err);
  const fresh = sigs.filter((s) => !txCache.has(s.signature));
  // one at a time: six parallel calls at page load are enough to trip the public devnet rate limit
  const fetched: Awaited<ReturnType<typeof connection.getTransaction>>[] = [];
  for (const s of fresh) fetched.push(await connection.getTransaction(s.signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }));
  fresh.forEach((s, i) => {
    const tx = fetched[i];
    if (tx) txCache.set(s.signature, { signature: s.signature, slot: s.slot, blockTime: s.blockTime ?? tx.blockTime ?? null, instruction: instructionFromLogs(tx.meta?.logMessages) });
  });
  return sigs.map((s) => txCache.get(s.signature)).filter((t): t is TxRecord => !!t);
}

let mintCache: PublicKey | null = null;
export async function getMint(): Promise<PublicKey> {
  if (!mintCache) mintCache = (await accounts().config.fetch(seeds.config())).mint as PublicKey;
  return mintCache;
}

export async function getStakeBps(): Promise<number> {
  return (await accounts().config.fetch(seeds.config())).stakeBps as number;
}

export async function usdcBalance(owner: PublicKey): Promise<bigint | null> {
  const mint = await getMint();
  try {
    const bal = await connection.getTokenAccountBalance(getAssociatedTokenAddressSync(mint, owner));
    return BigInt(bal.value.amount);
  } catch {
    return null;
  }
}

/** Is the RPC the cluster this build was made for? Null when fine, otherwise a plain message. */
export async function checkNetwork(): Promise<string | null> {
  try {
    const genesis = await connection.getGenesisHash();
    if (CLUSTER === "devnet" && genesis !== DEVNET_GENESIS) return `Wrong network: this build talks to devnet, but ${RPC_HOST} is a different cluster.`;
    if (CLUSTER === "localnet" && genesis === DEVNET_GENESIS) return `Wrong network: this build expects a local validator, but ${RPC_HOST} is devnet.`;
    const info = await connection.getAccountInfo(PROGRAM_ID);
    if (!info) return `The TrueFrame program is not deployed on ${RPC_HOST}.`;
    return null;
  } catch (e) {
    return explainError(e).message;
  }
}

export interface CreateParams {
  claimSecs: number;
  commitSecs: number;
  windowSecs: number;
  seed: bigint;
  numFrames: number;
  occlusionLevel: number;
  pricePerFrame: bigint;
}

export function newRequestId(): BN {
  return new BN(Date.now()).muln(1000).addn(Math.floor(Math.random() * 1000));
}

export async function sendAndConfirm(wallet: AppWallet, ixs: TransactionInstruction[]): Promise<string> {
  const tx = new Transaction().add(...ixs);
  tx.feePayer = wallet.publicKey;
  const latest = await connection.getLatestBlockhash("confirmed");
  tx.recentBlockhash = latest.blockhash;
  const signed = await wallet.signTransaction(tx);
  let sig: string;
  try {
    sig = await connection.sendRawTransaction(signed.serialize());
  } catch (e) {
    const logs = typeof (e as { getLogs?: unknown }).getLogs === "function" ? await (e as { getLogs(c: Connection): Promise<string[]> }).getLogs(connection).catch(() => undefined) : (e as { logs?: string[] }).logs;
    throw new TxError(explainError(e, logs ?? undefined));
  }
  const res = await connection.confirmTransaction({ signature: sig, ...latest }, "confirmed");
  if (res.value.err) {
    const t = await connection.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 });
    throw new TxError(explainError(new Error(JSON.stringify(res.value.err)), t?.meta?.logMessages ?? undefined));
  }
  return sig;
}

export class TxError extends Error {
  constructor(public readonly explained: Explained) {
    super(explained.message);
  }
}

export async function createRequest(wallet: AppWallet, p: CreateParams): Promise<{ signature: string; request: string }> {
  const program = readProgram();
  const mint = await getMint();
  const requestId = newRequestId();
  const request = seeds.request(wallet.publicKey, requestId);
  const params = {
    requestId,
    sceneType: 0,
    seed: new BN(p.seed.toString()),
    numFrames: p.numFrames,
    width: DEFAULTS.width,
    height: DEFAULTS.height,
    occlusionLevel: p.occlusionLevel,
    pricePerFrame: new BN(p.pricePerFrame.toString()),
    claimDuration: new BN(p.claimSecs),
    commitDuration: new BN(p.commitSecs),
    challengeWindowSecs: new BN(p.windowSecs),
  };
  const ix = await program.methods
    .createRequest(params)
    .accountsPartial({
      requester: wallet.publicKey,
      config: seeds.config(),
      mint,
      request,
      escrow: seeds.escrow(request),
      stakeVault: seeds.stake(request),
      requesterToken: getAssociatedTokenAddressSync(mint, wallet.publicKey),
      tokenProgram: TOKEN_PROGRAM_ID,
      systemProgram: SystemProgram.programId,
    })
    .instruction();
  const signature = await sendAndConfirm(wallet, [ix]);
  return { signature, request: request.toBase58() };
}

/** timeout_refund: anyone may call it once a deadline has passed (docs/PROTOCOL.md section 15.8). */
export async function timeoutRefund(wallet: AppWallet, req: { address: string; requester: string; mint: string }): Promise<string> {
  const request = new PublicKey(req.address);
  const mint = new PublicKey(req.mint);
  const ix = await readProgram()
    .methods.timeoutRefund()
    .accountsPartial({
      caller: wallet.publicKey,
      request,
      mint,
      escrow: seeds.escrow(request),
      stakeVault: seeds.stake(request),
      requesterToken: getAssociatedTokenAddressSync(mint, new PublicKey(req.requester)),
      tokenProgram: TOKEN_PROGRAM_ID,
    })
    .instruction();
  return sendAndConfirm(wallet, [ix]);
}

/** Chain clock minus browser clock, in seconds. Deadlines are judged by the chain's clock (which can trail the wall clock), so the refund button must use it. */
export async function chainClockOffset(): Promise<number> {
  const slot = await connection.getSlot("confirmed");
  for (const s of [slot, slot - 1, slot - 2]) {
    const t = await connection.getBlockTime(s).catch(() => null);
    if (t) return t - Date.now() / 1000;
  }
  return 0;
}
