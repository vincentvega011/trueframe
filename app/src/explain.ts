import type { CheckResult, FrameReport } from "./report";

function pct(x: number): string {
  const p = x * 100;
  if (p > 0 && p < 0.1) return "under 0.1%";
  return `${p >= 10 ? p.toFixed(0) : p.toFixed(1)}%`;
}

/** One plain sentence for a failed check, built from that check's own detail and measurements. */
export function explainCheck(c: CheckResult): string {
  const d = c.detail;
  const m = (c.metrics ?? {}) as Record<string, unknown>;
  switch (c.name) {
    case "seg_rerender": {
      const ga = typeof m.global_agreement === "number" ? m.global_agreement : null;
      return ga !== null ? `Labels don't match the scene: ${pct(1 - ga)} of pixels differ from the verifier's re-render.` : "Labels don't match the scene: the node's segmentation differs from the verifier's re-render.";
    }
    case "annotation_plausibility": {
      const obj = /object \d+ \((\w+)\)/.exec(d);
      const by = /by ([\d.]+)px/.exec(d);
      if (obj && by) return `Boxes don't match the scene: the ${obj[1]} box sticks out ${Math.round(Number(by[1]))} px beyond where the scene puts it.`;
      const ghost = /\((\w+)\) claims a visible box/.exec(d);
      if (ghost) return `Boxes don't match the scene: the node drew a box for a ${ghost[1]} that is not in view.`;
      if (/outside the/.test(d)) return "Boxes don't match the scene: a box lies outside the image.";
      if (/IoU/.test(d)) return "Boxes don't match the scene: a box covers too little of the object it labels.";
      return "Boxes don't match the scene.";
    }
    case "segmentation_consistency": return "The boxes disagree with the node's own segmentation image, so they were not measured from it.";
    case "scene_match": return "The node rendered a different scene than the one this request's seed produces.";
    case "hash_consistency": return "The files the node served are not the files it committed.";
    case "merkle_proof": return "This frame is not part of the dataset the node committed.";
    case "image_sanity": return /duplicate/.test(d) ? "This picture is a copy of another frame." : "This picture is not a valid, distinct render.";
    default: return `${c.name.replace(/_/g, " ")} failed.`;
  }
}

/** The sentence shown above a failing frame: the most telling failed check first, with a count of the others. */
export function explainFrame(f: FrameReport): string | null {
  if (f.status === "reveal_unavailable") return "The node did not serve this frame.";
  if (f.status === "not_checked") return null;
  const failed = f.checks.filter((c) => !c.passed);
  if (failed.length === 0) return null;
  const order = ["seg_rerender", "annotation_plausibility", "scene_match", "hash_consistency", "merkle_proof", "image_sanity", "segmentation_consistency"];
  failed.sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name));
  const rest = failed.length - 1;
  return explainCheck(failed[0]) + (rest > 0 ? ` ${rest} more ${rest === 1 ? "check" : "checks"} failed too.` : "");
}
