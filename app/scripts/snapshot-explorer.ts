// Reads the two explorer requests from devnet and writes their decoded accounts and transaction list as snapshot.json, the
// fallback the explorer uses if devnet no longer serves them. Run by scripts/build_explorer.sh:
//   tsx app/scripts/snapshot-explorer.ts <out dir>          (reports/<request>/snapshot.json is written under it)
// RPC: $DEVNET_RPC_URL if set (never written anywhere), else the public devnet endpoint.
import { AnchorProvider, Program, type Idl } from "@anchor-lang/core";
import { Connection, PublicKey } from "@solana/web3.js";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { toRequest, toSubmission } from "../src/decode";
import { instructionFromLogs, type TxRecord } from "../src/timeline";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(process.argv[2] ?? "");
if (!process.argv[2]) throw new Error("usage: snapshot-explorer.ts <out dir>");
const REQUESTS = ["EDN2pWCD2DJ8Yo9YxP5P2SrasK11oF7UxtbhF9tc3C4y", "FUbhyAU19SKaFB4tNfKU4URWwksVAL9jJorfeTVo5d2j"];
const idl = JSON.parse(readFileSync(join(here, "../src/idl/proof_of_render.json"), "utf8")) as Idl;
const connection = new Connection(process.env.DEVNET_RPC_URL || "https://api.devnet.solana.com", "confirmed");
const program = new Program(idl, new AnchorProvider(connection, { publicKey: PublicKey.default, signTransaction: async (t: never) => t, signAllTransactions: async (t: never) => t } as never, { commitment: "confirmed" }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const acct = program.account as any;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function retry<T>(fn: () => Promise<T>): Promise<T> {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) { if (i >= 5) throw e; await sleep(1500 * (i + 1)); }
  }
}

for (const address of REQUESTS) {
  const pk = new PublicKey(address);
  const request = await retry(() => acct.request.fetch(pk));
  const sub = await retry(() => acct.submission.fetchNullable(PublicKey.findProgramAddressSync([new TextEncoder().encode("submission"), pk.toBytes()], program.programId)[0]));
  const sigs = (await retry(() => connection.getSignaturesForAddress(pk, { limit: 50 }))).filter((s) => !s.err);
  const txs: TxRecord[] = [];
  for (const s of sigs) {
    const tx = await retry(() => connection.getTransaction(s.signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }));
    if (tx) txs.push({ signature: s.signature, slot: s.slot, blockTime: s.blockTime ?? tx.blockTime ?? null, instruction: instructionFromLogs(tx.meta?.logMessages) });
  }
  const snap = { taken_at: Math.floor(Date.now() / 1000), request: toRequest(pk, request), submission: sub ? toSubmission(sub) : null, txs };
  const dir = join(out, "reports", address);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "snapshot.json"), JSON.stringify(snap, null, 1) + "\n");
  console.log(`snapshot ${address}: ${snap.request.status}, ${txs.length} transactions`);
}
