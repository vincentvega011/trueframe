import { FrameUnavailable, type FrameSource } from "./frames";
import { verifyFrame } from "./verify";

export interface FrameState {
  index: number;
  status: "pending" | "fetching" | "verified" | "rejected";
  reason?: string;
  leaf?: string;
}

export interface DownloadParams {
  request: string;
  cluster: string;
  root: string;            // 64 hex, read live from the Submission account
  frameCount: number;      // from the Request account
  source: FrameSource;
  sampled: number[] | null; // the verifier's sample, when its report is available
  concurrency?: number;
  signal?: AbortSignal;
  onProgress?: (frames: FrameState[]) => void;
}

export interface Manifest {
  request: string;
  cluster: string;
  merkle_root: string;
  frame_count: number;
  complete: boolean;
  verified_frames: number;
  sampled_frames: number[] | null;
  verification: string;
  frames: { frame_index: number; leaf_hash: string | null; verified: boolean; reason?: string }[];
}

export interface DownloadResult { frames: FrameState[]; verified: number; manifest: Manifest; zip: Uint8Array | null }

export const zipName = (request: string) => `trueframe_${request.slice(0, 8)}.zip`;

export function verificationNote(sampled: number[] | null): string {
  const sample = sampled && sampled.length > 0
    ? `the verifier's random sample of ${sampled.length} frames, listed in sampled_frames`
    : "the verifier's random sample of frames (its report was not available, so sampled_frames is null)";
  return `Each frame's bytes were checked against the Merkle root committed on Solana (integrity). Correctness was established by ${sample}.`;
}

/** Fetch every frame, verify it in the browser, and zip the ones that verified. A frame that fails is reported and left out. */
export async function runDownload(p: DownloadParams): Promise<DownloadResult> {
  const frames: FrameState[] = Array.from({ length: p.frameCount }, (_, index) => ({ index, status: "pending" }));
  const files = new Map<number, { scene: Uint8Array; annotations: Uint8Array; rgb: Uint8Array; seg: Uint8Array }>();
  const emit = () => p.onProgress?.(frames.map((f) => ({ ...f })));
  emit();
  let next = 0;
  async function worker() {
    while (next < p.frameCount && !p.signal?.aborted) {
      const index = next++;
      frames[index] = { index, status: "fetching" };
      emit();
      try {
        const reveal = await p.source.get(index, p.signal);
        const v = await verifyFrame(reveal, { index, frameCount: p.frameCount, root: p.root });
        if (v.ok) { files.set(index, v.files); frames[index] = { index, status: "verified", leaf: v.leaf }; }
        else frames[index] = { index, status: "rejected", reason: v.reason, leaf: v.leaf };
      } catch (e) {
        if (p.signal?.aborted) return;
        frames[index] = { index, status: "rejected", reason: e instanceof FrameUnavailable ? `reveal unavailable: ${e.message}` : `could not be checked: ${e instanceof Error ? e.message : String(e)}` };
      }
      emit();
    }
  }
  await Promise.all(Array.from({ length: Math.min(p.concurrency ?? 4, p.frameCount) }, worker));
  if (p.signal?.aborted) throw new DOMException("cancelled", "AbortError");

  const verified = frames.filter((f) => f.status === "verified").length;
  const manifest: Manifest = {
    request: p.request,
    cluster: p.cluster,
    merkle_root: p.root,
    frame_count: p.frameCount,
    complete: verified === p.frameCount,
    verified_frames: verified,
    sampled_frames: p.sampled && p.sampled.length > 0 ? [...p.sampled].sort((a, b) => a - b) : null,
    verification: verificationNote(p.sampled),
    frames: frames.map((f) => ({ frame_index: f.index, leaf_hash: f.leaf ?? null, verified: f.status === "verified", ...(f.reason ? { reason: f.reason } : {}) })),
  };
  if (verified === 0) return { frames, verified, manifest, zip: null };

  const { zipSync } = await import("fflate"); // loaded on click, not with the page
  const entries: Record<string, Uint8Array> = { "manifest.json": new TextEncoder().encode(JSON.stringify(manifest, null, 2) + "\n") };
  for (const [i, f] of files) {
    entries[`frame_${i}/rgb.png`] = f.rgb;
    entries[`frame_${i}/seg.png`] = f.seg;
    entries[`frame_${i}/annotations.json`] = f.annotations;
    entries[`frame_${i}/scene.json`] = f.scene;
  }
  return { frames, verified, manifest, zip: zipSync(entries, { level: 0 }) }; // PNGs are already compressed
}

export function saveZip(zip: Uint8Array, name: string): void {
  const url = URL.createObjectURL(new Blob([zip as BlobPart], { type: "application/zip" }));
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
