// Browser-side check that a served frame is exactly what the node committed on chain. Spec: docs/PROTOCOL.md section 4
// (leaf, node, proof format, the four checks) and section 16.4 (the reveal payload). Pure functions plus WebCrypto; no DOM.

export interface ProofStep { sibling_hash: string; sibling_is_left: boolean }
export interface LeafFields { frame_index: number; scene_hash: string; annotation_hash: string; image_hash: string; seg_hash: string; leaf_hash: string }

/** The part of a /frame/<i> body this module reads. scene_json and annotations_json are the exact file text (what the leaf hashes cover). */
export interface FrameReveal {
  request: string;
  frame_index: number;
  frame_count: number;
  leaf: LeafFields;
  proof: ProofStep[];
  scene_json: string;
  annotations_json: string;
  rgb_png: string; // base64
  seg_png: string; // base64
}

export interface Expected { index: number; frameCount: number; root: string } // root: 64 hex, read from the Submission account

export interface FrameFiles { scene: Uint8Array; annotations: Uint8Array; rgb: Uint8Array; seg: Uint8Array }
export type Verified = { ok: true; leaf: string; files: FrameFiles } | { ok: false; reason: string; leaf?: string };

const HEX64 = /^[0-9a-f]{64}$/;
const enc = new TextEncoder();

export function hexToBytes(h: string): Uint8Array {
  if (!/^([0-9a-f]{2})*$/.test(h)) throw new Error("not lowercase hex");
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16);
  return out;
}
export const bytesToHex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

export function base64ToBytes(s: string): Uint8Array {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export async function sha256(data: Uint8Array): Promise<Uint8Array> {
  if (!globalThis.crypto?.subtle) throw new Error("This page cannot compute SHA-256: WebCrypto needs https or http://localhost.");
  return new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", data as BufferSource));
}

const concat = (...parts: Uint8Array[]) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
};

/** sha256(0x00 || frame_index u32 LE || scene_hash || annotation_hash || image_hash || seg_hash): 133 bytes, section 4.1. */
export function leafPreimage(index: number, scene: Uint8Array, annotations: Uint8Array, image: Uint8Array, seg: Uint8Array): Uint8Array {
  const idx = new Uint8Array(4);
  new DataView(idx.buffer).setUint32(0, index, true);
  return concat(Uint8Array.of(0x00), idx, scene, annotations, image, seg);
}
export async function leafHash(index: number, scene: Uint8Array, annotations: Uint8Array, image: Uint8Array, seg: Uint8Array): Promise<Uint8Array> {
  return sha256(leafPreimage(index, scene, annotations, image, seg));
}
/** sha256(0x01 || left || right), section 4. */
export const nodeHash = (left: Uint8Array, right: Uint8Array) => sha256(concat(Uint8Array.of(0x01), left, right));

/** Expected proof length: ceil(log2(frame_count)), i.e. the bit length of frame_count - 1. */
export const proofLength = (frameCount: number) => (frameCount <= 1 ? 0 : (frameCount - 1).toString(2).length);

export type ProofCheck = { ok: true; position: number } | { ok: false; reason: string };

/** Checks 2 to 4 of section 4 (range, proof length, position) without hashing anything. */
export function checkShape(index: number, frameCount: number, proof: ProofStep[]): ProofCheck {
  if (!Number.isInteger(index) || index < 0 || !Number.isInteger(frameCount) || frameCount < 1) return { ok: false, reason: "frame index or frame count is not valid" };
  if (index >= frameCount) return { ok: false, reason: `frame ${index} is outside the committed range 0..${frameCount - 1}` };
  if (!Array.isArray(proof) || proof.length !== proofLength(frameCount)) return { ok: false, reason: `proof has ${Array.isArray(proof) ? proof.length : "no"} steps, ${proofLength(frameCount)} expected for ${frameCount} frames` };
  let position = 0;
  for (let i = 0; i < proof.length; i++) {
    const s = proof[i];
    if (!s || typeof s.sibling_is_left !== "boolean" || typeof s.sibling_hash !== "string" || !HEX64.test(s.sibling_hash)) return { ok: false, reason: `proof step ${i} is malformed` };
    if (s.sibling_is_left) position += 2 ** i;
  }
  if (position !== index) return { ok: false, reason: `the proof is for position ${position}, not frame ${index}` };
  return { ok: true, position };
}

/** Fold a leaf up the proof to a root. */
export async function foldProof(leaf: Uint8Array, proof: ProofStep[]): Promise<Uint8Array> {
  let cur = leaf;
  for (const s of proof) {
    const sib = hexToBytes(s.sibling_hash);
    cur = await (s.sibling_is_left ? nodeHash(sib, cur) : nodeHash(cur, sib));
  }
  return cur;
}

const FILE_NAMES: [keyof LeafFields, string][] = [["scene_hash", "scene.json"], ["annotation_hash", "annotations.json"], ["image_hash", "rgb.png"], ["seg_hash", "seg.png"]];

/**
 * Verify one served frame against the on-chain root. The four hashes are computed here from the bytes as served; nothing
 * in the response is trusted except as the thing being checked (its own merkle_root field is never read).
 */
export async function verifyFrame(reveal: FrameReveal, expected: Expected): Promise<Verified> {
  if (!HEX64.test(expected.root)) return { ok: false, reason: "the on-chain root is not available" };
  let files: FrameFiles, hashes: Uint8Array[];
  try {
    if (reveal.frame_index !== expected.index) return { ok: false, reason: `the node answered with frame ${reveal.frame_index}, not frame ${expected.index}` };
    if (reveal.frame_count !== expected.frameCount) return { ok: false, reason: `the node reports ${reveal.frame_count} frames, the request has ${expected.frameCount}` };
    const shape = checkShape(expected.index, expected.frameCount, reveal.proof);
    if (!shape.ok) return shape;
    files = { scene: enc.encode(reveal.scene_json), annotations: enc.encode(reveal.annotations_json), rgb: base64ToBytes(reveal.rgb_png), seg: base64ToBytes(reveal.seg_png) };
    hashes = await Promise.all([files.scene, files.annotations, files.rgb, files.seg].map(sha256));
  } catch (e) {
    return { ok: false, reason: `the frame could not be read: ${e instanceof Error ? e.message : String(e)}` };
  }
  const leaf = await leafHash(expected.index, hashes[0], hashes[1], hashes[2], hashes[3]);
  const leafHex = bytesToHex(leaf);
  if (bytesToHex(await foldProof(leaf, reveal.proof)) === expected.root) return { ok: true, leaf: leafHex, files };

  // It does not match. Say why: does the committed leaf (as served) lead to the on-chain root, with different files?
  const changed = FILE_NAMES.filter(([key], i) => reveal.leaf?.[key] !== bytesToHex(hashes[i])).map(([, name]) => name);
  try {
    const served = [reveal.leaf.scene_hash, reveal.leaf.annotation_hash, reveal.leaf.image_hash, reveal.leaf.seg_hash].map(hexToBytes);
    const committed = await leafHash(expected.index, served[0], served[1], served[2], served[3]);
    if (changed.length > 0 && bytesToHex(await foldProof(committed, reveal.proof)) === expected.root) {
      return { ok: false, leaf: leafHex, reason: `${changed.join(" and ")} differ${changed.length === 1 ? "s" : ""} from what the node committed on chain` };
    }
  } catch { /* malformed leaf fields: fall through to the general message */ }
  return { ok: false, leaf: leafHex, reason: "the frame and its proof do not lead to the Merkle root on chain" };
}
