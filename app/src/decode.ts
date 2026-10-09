// Decoding of the program's accounts into the plain objects the views use. No config, no network: the explorer's snapshot
// script (scripts/snapshot-explorer.ts) runs this same code against devnet.
import type { PublicKey } from "@solana/web3.js";
import type { RequestView, SubmissionView } from "./timeline";

export const num = (v: { toNumber(): number }) => v.toNumber();
export const hex = (b: number[] | Uint8Array) => Array.from(b).map((x) => x.toString(16).padStart(2, "0")).join("");

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
export function toRequest(address: PublicKey, a: any): RequestAccount {
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


// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function toSubmission(a: any): SubmissionView {
  const seedHex = hex(a.sampleSeed);
  return {
    merkleRoot: hex(a.merkleRoot),
    commitSlot: num(a.commitSlot),
    sampleSeed: /^0+$/.test(seedHex) ? null : seedHex,
    sampleSlot: num(a.sampleSlot),
    sampleCount: a.sampleCount,
    verdict: Object.keys(a.verdict)[0].replace(/^./, (c: string) => c.toUpperCase()),
  };
}
