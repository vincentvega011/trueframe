import { REPORT_URL } from "./config";

export interface CheckResult {
  name: string;
  passed: boolean;
  detail: string;
  metrics?: Record<string, unknown>;
}

export interface FrameReport {
  index: number;
  status: "checked" | "reveal_unavailable" | "not_checked";
  passed: boolean;
  checks: CheckResult[];
  reason?: string;
  images?: Record<string, string>;
}

export interface Outcome {
  action: "paid" | "slashed";
  to: "node" | "requester";
  destination: string;
  escrow_moved: number;
  stake_moved: number;
  destination_delta: number;
  final_status: string;
}

export interface Report {
  request: string;
  verdict?: "Pass" | "Fail";
  verdict_reasons?: string[];
  frames?: FrameReport[];
  sample?: { seed: string; slot: number; count: number; indices: number[] };
  outcome?: Outcome;
  transactions?: Record<string, string>;
  timing?: Record<string, number>;
  num_frames?: number;
  merkle_root?: string;
  verification_error?: string;
}

export const CHECK_LABELS: Record<string, string> = {
  merkle_proof: "Merkle proof",
  scene_match: "Scene match",
  hash_consistency: "Hash consistency",
  annotation_plausibility: "Box plausibility",
  image_sanity: "Image sanity",
  segmentation_consistency: "Segmentation ids",
  seg_rerender: "Segmentation re-render",
};

export type ReportResult = { kind: "ok"; report: Report } | { kind: "missing" } | { kind: "unreachable"; reason: string };

export async function fetchReport(request: string): Promise<ReportResult> {
  try {
    const r = await fetch(`${REPORT_URL}/${request}/report.json`, { cache: "no-store" });
    if (r.status === 404) return { kind: "missing" };
    if (!r.ok) return { kind: "unreachable", reason: `HTTP ${r.status}` };
    return { kind: "ok", report: (await r.json()) as Report };
  } catch (e) {
    return { kind: "unreachable", reason: (e as Error).message };
  }
}

export function imageUrl(request: string, path: string): string {
  return `${REPORT_URL}/${request}/${path}`;
}
