// What the views read for one request. Normal builds read the chain only. The explorer build reads the chain first and, when the
// account is gone or unreadable (devnet reset, pruned, RPC down), falls back to a snapshot taken at build time and says so.
import { getRequest, getSubmission, getTransactions, type RequestAccount } from "./chain";
import { EXPLORER, REPORT_URL } from "./config";
import type { SubmissionView, TxRecord } from "./timeline";

/** scripts/snapshot-explorer.ts writes this to reports/<request>/snapshot.json. */
export interface Snapshot {
  taken_at: number; // unix seconds
  request: RequestAccount;
  submission: SubmissionView | null;
  txs: TxRecord[];
}

export interface RequestData {
  request: RequestAccount;
  submission: SubmissionView | null;
  txs: TxRecord[];
  /** Set when the account itself comes from the snapshot (the chain no longer serves it). */
  accountsFromSnapshot: number | null;
  /** Set when only the transaction list comes from the snapshot. */
  txsFromSnapshot: number | null;
}

const snapshots = new Map<string, Promise<Snapshot | null>>();
export function fetchSnapshot(address: string): Promise<Snapshot | null> {
  let p = snapshots.get(address);
  if (!p) {
    p = fetch(`${REPORT_URL}/${address}/snapshot.json`, { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<Snapshot>) : null))
      .catch(() => null);
    snapshots.set(address, p);
  }
  return p;
}

export const snapshotDate = (takenAt: number) => new Date(takenAt * 1000).toISOString().slice(0, 10);

/** Live first; null when neither the chain nor a snapshot knows the request. `missing` distinguishes "the chain says it does not exist". */
export async function loadRequest(address: string, opts: { txs: boolean }): Promise<RequestData | { missing: true }> {
  let live: RequestAccount | null | undefined;
  try { live = await getRequest(address); } catch { live = undefined; }   // undefined: could not read
  if (live) {
    const submission = EXPLORER || opts.txs ? await getSubmission(address).catch(() => null) : null;
    let txs: TxRecord[] = [];
    let txsFromSnapshot: number | null = null;
    if (opts.txs) {
      try { txs = await getTransactions(address); } catch { txs = []; }
      if (EXPLORER && txs.length === 0) {
        const snap = await fetchSnapshot(address);
        if (snap && snap.txs.length > 0) { txs = snap.txs; txsFromSnapshot = snap.taken_at; }
      }
    }
    return { request: live, submission, txs, accountsFromSnapshot: null, txsFromSnapshot };
  }
  if (EXPLORER) {
    const snap = await fetchSnapshot(address);
    if (snap) return { request: snap.request, submission: snap.submission, txs: snap.txs, accountsFromSnapshot: snap.taken_at, txsFromSnapshot: null };
  }
  if (live === undefined) throw new Error("Could not read the chain.");
  return { missing: true };
}

export const isMissing = (d: RequestData | { missing: true } | null | undefined): d is { missing: true } => !!d && "missing" in d;
