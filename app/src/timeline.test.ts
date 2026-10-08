import { describe, expect, it } from "vitest";
import { deriveTimeline, instructionFromLogs, refundable, settlementText, type RequestView, type SubmissionView, type TxRecord } from "./timeline";

const req = (status: string, claimedAt = 0): RequestView => ({ status, claimDeadline: 1000, commitDeadline: 2000, challengeWindowEnd: 3000, createdAt: 0, claimedAt });
const tx = (instruction: string, slot: number): TxRecord => ({ signature: `sig${slot}`, slot, blockTime: slot, instruction });
const sub = (verdict: string): SubmissionView => ({ merkleRoot: "aa", commitSlot: 3, sampleSeed: "bb", sampleSlot: 4, sampleCount: 7, verdict });
const states = (r: RequestView, s: SubmissionView | null, t: TxRecord[]) => deriveTimeline(r, s, t, 0).map((x) => `${x.key}:${x.state}`);

describe("deriveTimeline", () => {
  it("an open request waits at Open", () => {
    expect(states(req("Open"), null, [tx("CreateRequest", 1)])).toEqual(["Open:active", "Claimed:pending", "Committed:pending", "Challenged:pending", "Settled:pending"]);
  });
  it("a paid request has every step done", () => {
    const all = [tx("CreateRequest", 1), tx("StakeAndClaim", 2), tx("SubmitCommitment", 3), tx("OpenChallenge", 4), tx("SubmitVerdict", 5), tx("FinalizePayout", 6)];
    const steps = deriveTimeline(req("Paid", 5), sub("Pass"), all, 0);
    expect(steps.every((s) => s.state === "done")).toBe(true);
    expect(steps[3].verdictTx?.signature).toBe("sig5");
    expect(steps[4].label).toBe("Paid");
  });
  it("a slashed request labels the last step Slashed and shows the verdict", () => {
    const all = [tx("CreateRequest", 1), tx("StakeAndClaim", 2), tx("SubmitCommitment", 3), tx("OpenChallenge", 4), tx("SubmitVerdict", 5), tx("SlashAndRefund", 6)];
    const steps = deriveTimeline(req("Slashed", 5), sub("Fail"), all, 0);
    expect(steps[4].label).toBe("Slashed");
    expect(steps[3].fields.find((f) => f.label === "Verdict")?.tone).toBe("fail");
  });
  it("an unclaimed expiry skips the middle steps", () => {
    expect(states(req("Expired"), null, [tx("CreateRequest", 1), tx("TimeoutRefund", 2)])).toEqual(["Open:done", "Claimed:skipped", "Committed:skipped", "Challenged:skipped", "Settled:done"]);
  });
  it("a claimed-then-expired request keeps Claimed", () => {
    expect(states(req("Expired", 5), null, [tx("CreateRequest", 1), tx("StakeAndClaim", 2), tx("TimeoutRefund", 3)])).toEqual(["Open:done", "Claimed:done", "Committed:skipped", "Challenged:skipped", "Settled:done"]);
  });
  it("still shows every step done for a paid request when the ledger no longer has its transactions", () => {
    const steps = deriveTimeline(req("Paid", 5), sub("Pass"), [], 0);
    expect(steps.map((x) => x.state)).toEqual(["done", "done", "done", "done", "done"]);
    expect(steps.every((x) => x.tx === undefined)).toBe(true);
  });
  it("Verified stays on the Challenged step and says the payout is waiting on the window", () => {
    const steps = deriveTimeline(req("Verified"), sub("Pass"), [tx("CreateRequest", 1), tx("StakeAndClaim", 2), tx("SubmitCommitment", 3), tx("OpenChallenge", 4), tx("SubmitVerdict", 5)], 2990);
    expect(steps[3].state).toBe("active");
    expect(steps[3].waiting).toContain("payout opens");
  });
});

describe("instructionFromLogs", () => {
  it("reads the Anchor instruction line and ignores token-program lines", () => {
    expect(instructionFromLogs(["Program x invoke [1]", "Program log: Instruction: SubmitCommitment", "Program log: Instruction: TransferChecked"])).toBe("SubmitCommitment");
    expect(instructionFromLogs(["Program log: Instruction: TransferChecked"])).toBeNull();
    expect(instructionFromLogs(undefined)).toBeNull();
  });
});

describe("deadlines", () => {
  it("shows the commit deadline on a claimed step, like the claim deadline on Open", () => {
    const steps = deriveTimeline(req("Claimed", 5), null, [tx("CreateRequest", 1), tx("StakeAndClaim", 2)], 0);
    expect(steps[0].fields.map((f) => f.label)).toContain("Claim deadline");
    expect(steps[1].fields.map((f) => f.label)).toContain("Commit deadline");
    expect(deriveTimeline(req("Open"), null, [], 0)[1].fields).toEqual([]);   // not claimed yet: nothing to show
  });
});

describe("refunds and amounts", () => {
  const r = (status: string, claimedAt = 0) => ({ ...req(status, claimedAt), escrowAmount: "20000000", stakeAmount: "4000000" });
  it("offers a refund only strictly after the deadline that applies", () => {
    expect(refundable(req("Open"), 1000)).toBeNull();
    expect(refundable(req("Open"), 1001)).toBe("unclaimed");
    expect(refundable(req("Claimed", 5), 2000)).toBeNull();
    expect(refundable(req("Claimed", 5), 2001)).toBe("uncommitted");
    for (const s of ["Committed", "Challenged", "Verified", "Paid", "Slashed", "Expired"]) expect(refundable(req(s, 5), 9999)).toBeNull();
  });
  it("says what each settlement moved", () => {
    expect(settlementText(r("Paid", 5))).toBe("20.00 USDC to the node, 4.00 stake returned");
    expect(settlementText(r("Slashed", 5))).toBe("20.00 USDC refunded to the requester, 4.00 stake slashed to the requester");
    expect(settlementText(r("Expired", 0))).toBe("20.00 USDC refunded to the requester");
    expect(settlementText(r("Expired", 5))).toBe("20.00 USDC refunded to the requester, 4.00 stake slashed to the requester");
    expect(settlementText(r("Open"))).toBeNull();
  });
  it("puts the amounts on the settled step", () => {
    const steps = deriveTimeline(r("Expired"), null, [], 0);
    expect(steps[4].fields.find((f) => f.label === "Moved")?.value).toBe("20.00 USDC refunded to the requester");
  });
});
