import { usdc } from "./format";

export type StepKey = "Open" | "Claimed" | "Committed" | "Challenged" | "Settled";
export type StepState = "done" | "active" | "pending" | "skipped";

export interface TxRecord {
  signature: string;
  slot: number;
  blockTime: number | null;
  /** Anchor instruction name from the logs, e.g. "StakeAndClaim"; null when not recognised. */
  instruction: string | null;
}

export interface RequestView {
  status: string; // Open | Claimed | Committed | Challenged | Verified | Paid | Slashed | Expired
  claimDeadline: number;
  commitDeadline: number;
  challengeWindowEnd: number;
  createdAt: number;
  claimedAt: number;
  /** Base units as decimal strings; used to say what a settlement moved (the program's own rules, docs/PROTOCOL.md section 15). */
  escrowAmount?: string;
  stakeAmount?: string;
}

export interface SubmissionView {
  merkleRoot: string;
  commitSlot: number;
  sampleSeed: string | null; // null until open_challenge
  sampleSlot: number;
  sampleCount: number;
  verdict: string; // Pending | Pass | Fail
}

export interface Step {
  key: StepKey;
  label: string;
  state: StepState;
  tx?: TxRecord;
  /** For Challenged: the verdict transaction. */
  verdictTx?: TxRecord;
  /** What the app is waiting for, for the active step. */
  waiting?: string;
  fields: { label: string; value: string; mono?: boolean; tone?: "pass" | "fail" }[];
}

const OPENING: Record<string, StepKey> = {
  CreateRequest: "Open",
  StakeAndClaim: "Claimed",
  SubmitCommitment: "Committed",
  OpenChallenge: "Challenged",
  FinalizePayout: "Settled",
  SlashAndRefund: "Settled",
  TimeoutRefund: "Settled",
};

export const TERMINAL = ["Paid", "Slashed", "Expired"];
const ORDER: StepKey[] = ["Open", "Claimed", "Committed", "Challenged", "Settled"];

function rank(status: string): number {
  switch (status) {
    case "Open": return 0;
    case "Claimed": return 1;
    case "Committed": return 2;
    case "Challenged":
    case "Verified": return 3;
    default: return 4;
  }
}

export function instructionFromLogs(logs: string[] | null | undefined): string | null {
  for (const line of logs ?? []) {
    const m = /Program log: Instruction: (\w+)/.exec(line);
    if (m && m[1] in OPENING || (m && m[1] === "SubmitVerdict")) return m[1];
  }
  return null;
}

/** Anyone may refund once a deadline has passed (strictly after it, as the program requires). */
export function refundable(req: RequestView, now: number): "unclaimed" | "uncommitted" | null {
  if (req.status === "Open" && now > req.claimDeadline) return "unclaimed";
  if (req.status === "Claimed" && now > req.commitDeadline) return "uncommitted";
  return null;
}

/** What the settlement moved, from the request's amounts and the program's payout rules. */
export function settlementText(req: RequestView): string | null {
  if (!req.escrowAmount || !req.stakeAmount) return null;
  const e = usdc(req.escrowAmount), s = usdc(req.stakeAmount);
  if (req.status === "Paid") return `${e} USDC to the node, ${s} stake returned`;
  if (req.status === "Slashed") return `${e} USDC refunded to the requester, ${s} stake slashed to the requester`;
  if (req.status === "Expired") return req.claimedAt > 0 ? `${e} USDC refunded to the requester, ${s} stake slashed to the requester` : `${e} USDC refunded to the requester`;
  return null;
}

export function countdownText(deadline: number, now: number): string {
  const left = Math.ceil(deadline - now);
  if (left <= 0) return "deadline passed";
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left`;
}

export function deriveTimeline(req: RequestView, sub: SubmissionView | null, txs: TxRecord[], now: number): Step[] {
  const byKey = new Map<StepKey, TxRecord>();
  let verdictTx: TxRecord | undefined;
  for (const t of [...txs].sort((a, b) => a.slot - b.slot)) {
    if (t.instruction === "SubmitVerdict") verdictTx = t;
    else if (t.instruction && OPENING[t.instruction]) byKey.set(OPENING[t.instruction], t);
  }
  const at = rank(req.status);
  const terminal = TERMINAL.includes(req.status);
  const settledLabel = req.status === "Paid" ? "Paid" : req.status === "Slashed" ? "Slashed" : req.status === "Expired" ? "Expired" : "Paid or slashed";

  // What happened comes from the accounts, not from transaction history: a local validator purges old ledger, and a
  // timeline that went blank for an old request would be wrong. Transactions only add the time, slot and link.
  const happened: Record<StepKey, boolean> = {
    Open: true,
    Claimed: req.claimedAt > 0 || at >= 1 && !terminal,
    Committed: sub !== null || (at >= 2 && !terminal),
    Challenged: !!sub?.sampleSeed || (at >= 3 && !terminal),
    Settled: terminal,
  };

  return ORDER.map((key, i): Step => {
    const tx = byKey.get(key);
    let state: StepState;
    if (happened[key] && (key === "Settled" || i < at || terminal)) state = "done";
    else if (i === at && !terminal) state = "active";
    else state = terminal ? "skipped" : "pending";

    const step: Step = { key, label: key === "Settled" ? settledLabel : key, state, tx, fields: [] };
    if (key === "Open") {
      step.fields.push({ label: "Claim deadline", value: new Date(req.claimDeadline * 1000).toLocaleTimeString("en-GB", { hour12: false }) });
      if (state === "active") step.waiting = now > req.claimDeadline ? "No node claimed it before the deadline" : `Waiting for a node to claim (${countdownText(req.claimDeadline, now)})`;
    }
    if (key === "Claimed" && state !== "pending" && state !== "skipped") step.fields.push({ label: "Commit deadline", value: new Date(req.commitDeadline * 1000).toLocaleTimeString("en-GB", { hour12: false }) });
    if (key === "Claimed" && state === "active") step.waiting = now > req.commitDeadline ? "The node did not commit before the deadline" : `The node is rendering (commit due, ${countdownText(req.commitDeadline, now)})`;
    if (key === "Committed") {
      if (sub) step.fields.push({ label: "Merkle root", value: sub.merkleRoot, mono: true });
      if (state === "active") step.waiting = "Waiting for the verifier to open the challenge";
    }
    if (key === "Challenged") {
      step.verdictTx = verdictTx;
      if (sub?.sampleSeed) {
        step.fields.push({ label: "Sample seed", value: sub.sampleSeed, mono: true });
        step.fields.push({ label: "Sample slot", value: String(sub.sampleSlot), mono: true });
      }
      if (sub && sub.verdict !== "Pending") step.fields.push({ label: "Verdict", value: sub.verdict, tone: sub.verdict === "Pass" ? "pass" : "fail" });
      if (state === "active") {
        step.waiting = sub?.verdict === "Pending" || !sub
          ? `The verifier is checking sampled frames (window ${countdownText(req.challengeWindowEnd, now)})`
          : sub.verdict === "Pass" ? `Verdict Pass; payout opens when the window ends (${countdownText(req.challengeWindowEnd, now)})` : "Verdict Fail; waiting for the slash";
      }
    }
    if (key === "Settled" && state === "done") {
      const moved = settlementText(req);
      if (moved) step.fields.push({ label: "Moved", value: moved, tone: req.status === "Paid" ? "pass" : req.status === "Slashed" ? "fail" : undefined });
    }
    return step;
  });
}
