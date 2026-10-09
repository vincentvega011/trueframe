import { describe, expect, it } from "vitest";
import fixture from "./__fixtures__/frames.json";
import { bytesToHex, foldProof, hexToBytes, leafHash, leafPreimage, nodeHash, proofLength, sha256, verifyFrame, type FrameReveal } from "./verify";

const rep = (byte: number) => new Uint8Array(32).fill(byte);
const enc = new TextEncoder();
const fx = fixture.frames as unknown as FrameReveal[];
const expected = (index: number) => ({ index, frameCount: fixture.frame_count, root: fixture.merkle_root });
const clone = (i: number): FrameReveal => JSON.parse(JSON.stringify(fx[i]));

describe("leaf construction (PROTOCOL.md section 4.2, worked example)", () => {
  it("builds the 133-byte preimage byte for byte", () => {
    const pre = leafPreimage(5, rep(0x11), rep(0x22), rep(0x33), rep(0x44));
    expect(pre.length).toBe(133);
    expect(bytesToHex(pre)).toBe("00" + "05000000" + "11".repeat(32) + "22".repeat(32) + "33".repeat(32) + "44".repeat(32));
  });
  it("hashes it to the published leaf", async () => {
    expect(bytesToHex(await leafHash(5, rep(0x11), rep(0x22), rep(0x33), rep(0x44)))).toBe("5b4faa09ef2bcb0f4a444889da86952db0534a0f1e6cf209b81e411e5de53fd4");
  });
  it("reproduces the reference leaf 0 and the 3-leaf root from the Python vectors", async () => {
    const f = async (p: string, i: number) => sha256(enc.encode(`${p}${i}`));
    const leaves: Uint8Array[] = [];
    for (let i = 0; i < 3; i++) leaves.push(await leafHash(i, await f("scene", i), await f("ann", i), await f("img", i), await f("seg", i)));
    expect(bytesToHex(leaves[0])).toBe("696717fc55e3f9632ebcc11084a2e989f8f19bfb2d2c797035b7e78a981ab1cf");
    const l01 = await nodeHash(leaves[0], leaves[1]);
    const l22 = await nodeHash(leaves[2], leaves[2]); // odd node: duplicated
    expect(bytesToHex(await nodeHash(l01, l22))).toBe("831dc8a7f17dc9262b50dd29d5547d27b5b29479bfabfd53e55684b9d1832f08");
  });
  it("expects ceil(log2 n) proof steps", () => {
    expect([1, 2, 3, 4, 5, 8, 9, 20, 1024].map(proofLength)).toEqual([0, 1, 2, 2, 3, 3, 4, 5, 10]);
  });
});

describe("frames produced by the Python merkle module (5 frames, odd count)", () => {
  it("verifies every frame against the root", async () => {
    for (let i = 0; i < 5; i++) {
      const v = await verifyFrame(clone(i), expected(i));
      expect(v, `frame ${i}`).toMatchObject({ ok: true, leaf: fx[i].leaf.leaf_hash });
    }
  });
  it("hashes the JSON exactly as served (a 1.0 stays 1.0)", () => {
    expect(fx[0].scene_json).toContain('"yaw":1.0');
    expect(JSON.stringify(JSON.parse(fx[0].scene_json))).not.toBe(fx[0].scene_json);
  });
});

describe("rejections", () => {
  it("rejects a tampered proof", async () => {
    const f = clone(2);
    const b = hexToBytes(f.proof[1].sibling_hash); b[0] ^= 1;
    f.proof[1].sibling_hash = bytesToHex(b);
    expect(await verifyFrame(f, expected(2))).toMatchObject({ ok: false, reason: "the frame and its proof do not lead to the Merkle root on chain" });
  });
  it("rejects a wrong index: another frame's data served for this index", async () => {
    expect(await verifyFrame(clone(3), expected(1))).toMatchObject({ ok: false, reason: "the node answered with frame 3, not frame 1" });
  });
  it("rejects a wrong index even when the body is relabelled (the position check)", async () => {
    const f = clone(3); f.frame_index = 1;
    expect(await verifyFrame(f, expected(1))).toMatchObject({ ok: false, reason: "the proof is for position 3, not frame 1" });
  });
  it("rejects the duplicated-last-leaf forgery (section 4.3): frame 4 with its sibling flipped to the left", async () => {
    const f = clone(4); f.proof[0].sibling_is_left = true;
    expect(await verifyFrame(f, { index: 4, frameCount: 5, root: fixture.merkle_root })).toMatchObject({ ok: false });
    const g = clone(4); g.frame_index = 5; g.proof[0].sibling_is_left = true; g.proof[2].sibling_is_left = false;
    expect(await verifyFrame(g, { index: 5, frameCount: 5, root: fixture.merkle_root })).toMatchObject({ ok: false, reason: "frame 5 is outside the committed range 0..4" });
  });
  it("names the file that was changed after the commit", async () => {
    const f = clone(2);
    f.rgb_png = btoa("not the committed picture");
    expect(await verifyFrame(f, expected(2))).toMatchObject({ ok: false, reason: "rgb.png differs from what the node committed on chain" });
    const g = clone(2); g.scene_json = g.scene_json.replace("1.0", "1.5");
    expect(await verifyFrame(g, expected(2))).toMatchObject({ ok: false, reason: "scene.json differs from what the node committed on chain" });
  });
  it("rejects a different root, a wrong frame count and a short proof", async () => {
    expect(await verifyFrame(clone(0), { ...expected(0), root: "00".repeat(32) })).toMatchObject({ ok: false });
    expect(await verifyFrame(clone(0), { ...expected(0), frameCount: 6 })).toMatchObject({ ok: false, reason: "the node reports 5 frames, the request has 6" });
    const f = clone(0); f.proof.pop();
    expect(await verifyFrame(f, expected(0))).toMatchObject({ ok: false, reason: "proof has 2 steps, 3 expected for 5 frames" });
  });
  it("does not trust the leaf fields the node serves: a forged leaf with matching files still fails", async () => {
    const f = clone(1);
    f.leaf.scene_hash = "ab".repeat(32);
    expect(await verifyFrame(f, expected(1))).toMatchObject({ ok: true }); // files are the committed ones, the served copy of the hash is irrelevant
  });
  it("folds a proof of one leaf to that leaf", async () => {
    const leaf = rep(7);
    expect(bytesToHex(await foldProof(leaf, []))).toBe(bytesToHex(leaf));
  });
});
