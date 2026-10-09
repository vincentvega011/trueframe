import { unzipSync } from "fflate";
import { describe, expect, it } from "vitest";
import fixture from "./__fixtures__/frames.json";
import { runDownload, verificationNote, zipName } from "./download";
import { FrameUnavailable, type FrameSource } from "./frames";
import type { FrameReveal } from "./verify";

const frames = fixture.frames as unknown as FrameReveal[];
const source = (patch: (i: number, f: FrameReveal) => FrameReveal | Error = (_, f) => f): FrameSource => ({
  label: "test",
  async get(i) {
    const r = patch(i, JSON.parse(JSON.stringify(frames[i])));
    if (r instanceof Error) throw r;
    return r;
  },
});
const params = (s: FrameSource) => ({ request: "AbCdEfGhRequest", cluster: "localnet", root: fixture.merkle_root, frameCount: 5, source: s, sampled: [4, 0, 2] });
const text = (b: Uint8Array) => new TextDecoder().decode(b);

describe("dataset download", () => {
  it("zips every verified frame with a manifest that carries the on-chain root", async () => {
    const seen: number[] = [];
    const r = await runDownload({ ...params(source()), onProgress: (fs) => seen.push(fs.filter((f) => f.status === "verified").length) });
    expect(r.verified).toBe(5);
    expect(seen.at(-1)).toBe(5);
    const z = unzipSync(r.zip!);
    expect(Object.keys(z).sort()).toEqual(["manifest.json", ...[0, 1, 2, 3, 4].flatMap((i) => ["annotations.json", "rgb.png", "scene.json", "seg.png"].map((f) => `frame_${i}/${f}`))].sort());
    const m = JSON.parse(text(z["manifest.json"]));
    expect(m).toMatchObject({ request: "AbCdEfGhRequest", cluster: "localnet", merkle_root: fixture.merkle_root, frame_count: 5, complete: true, verified_frames: 5, sampled_frames: [0, 2, 4] });
    expect(m.frames.map((f: { leaf_hash: string }) => f.leaf_hash)).toEqual(frames.map((f) => f.leaf.leaf_hash));
    expect(m.verification).toBe("Each frame's bytes were checked against the Merkle root committed on Solana (integrity). Correctness was established by the verifier's random sample of 3 frames, listed in sampled_frames.");
    expect(text(z["frame_2/scene.json"])).toBe(frames[2].scene_json); // the exact bytes that were hashed
  });
  it("rejects exactly the tampered frame, keeps going, and leaves it out of the zip", async () => {
    const r = await runDownload(params(source((i, f) => (i === 3 ? { ...f, rgb_png: btoa("tampered") } : f))));
    expect(r.verified).toBe(4);
    expect(r.frames.filter((f) => f.status === "rejected").map((f) => [f.index, f.reason])).toEqual([[3, "rgb.png differs from what the node committed on chain"]]);
    const z = unzipSync(r.zip!);
    expect(Object.keys(z).some((k) => k.startsWith("frame_3/"))).toBe(false);
    expect(Object.keys(z).filter((k) => k.endsWith("rgb.png"))).toHaveLength(4);
    expect(JSON.parse(text(z["manifest.json"]))).toMatchObject({ complete: false, verified_frames: 4 });
  });
  it("treats an unreachable frame as rejected, and returns no zip when nothing verified", async () => {
    const r = await runDownload(params(source(() => new FrameUnavailable("the frame source answered HTTP 404"))));
    expect(r.zip).toBeNull();
    expect(r.frames[0]).toMatchObject({ status: "rejected", reason: "reveal unavailable: the frame source answered HTTP 404" });
  });
  it("says so when the verifier's sample is unknown", () => {
    expect(verificationNote(null)).toContain("sampled_frames is null");
  });
  it("names the zip after the first 8 characters of the request", () => {
    expect(zipName("3Yp8gHTDTRCav2YpT7TF5EJkairEf6DQWv6LnqYjRkyT")).toBe("trueframe_3Yp8gHTD.zip");
  });
});
