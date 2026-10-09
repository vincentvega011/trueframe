import { FRAMES_URL, REVEAL_URL } from "./config";
import type { FrameReveal } from "./verify";

/** Where frames come from. Both sources return the /frame/<i> body of docs/PROTOCOL.md section 16.4 (plus scene_json and annotations_json). */
export interface FrameSource {
  readonly label: string;
  get(index: number, signal?: AbortSignal): Promise<FrameReveal>;
}

export class FrameUnavailable extends Error {}

async function getJson(url: string, signal?: AbortSignal): Promise<FrameReveal> {
  let r: Response;
  try { r = await fetch(url, { cache: "no-store", signal }); } catch (e) {
    if (signal?.aborted) throw e;
    throw new FrameUnavailable("cannot reach the frame source");
  }
  if (!r.ok) throw new FrameUnavailable(`the frame source answered HTTP ${r.status}`);
  try { return (await r.json()) as FrameReveal; } catch { throw new FrameUnavailable("the frame source did not return JSON"); }
}

/** The node's reveal server: GET <base>/frame/<i>?request=<address>. */
export function liveSource(base: string, request: string): FrameSource {
  const b = base.replace(/\/$/, "");
  return { label: "node reveal server", get: (i, signal) => getJson(`${b}/frame/${i}?request=${encodeURIComponent(request)}`, signal) };
}

/** A folder of files, one <i>.json per frame in the same shape: GET <base>/<i>.json (the static explorer build). */
export function staticSource(base: string): FrameSource {
  const b = base.replace(/\/$/, "");
  return { label: "static frame folder", get: (i, signal) => getJson(`${b}/${i}.json`, signal) };
}

/** VITE_FRAMES_URL selects the static folder; otherwise the live reveal server (VITE_REVEAL_URL). */
export const defaultSource = (request: string): FrameSource => (FRAMES_URL ? staticSource(FRAMES_URL) : liveSource(REVEAL_URL, request));
