import { useRef, useState } from "react";
import { getSubmission, type RequestAccount } from "../chain";
import { CLUSTER } from "../config";
import { runDownload, saveZip, zipName, type FrameState } from "../download";
import { defaultSource } from "../frames";
import { Mono, Notice } from "../ui/bits";

/** Result view, below the verdict. Paid: download the dataset, every frame checked in the browser against the on-chain root.
 *  Slashed: disabled with the reason. Any other status: nothing. */
export function DownloadPanel({ request, sampled }: { request: RequestAccount | null; sampled: number[] | null }) {
  const [frames, setFrames] = useState<FrameState[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  if (!request) return null;
  if (request.status === "Slashed") {
    return (
      <section className="dataset" aria-label="Dataset">
        <h2>Dataset</h2>
        <p className="dim">Download disabled: this dataset failed verification and you were refunded.</p>
      </section>
    );
  }
  if (request.status !== "Paid") return null;

  async function start() {
    if (!request) return;
    setError(null); setSaved(null); setBusy(true); setFrames(null);
    abort.current = new AbortController();
    try {
      if (!globalThis.crypto?.subtle) throw new Error("This page cannot compute SHA-256. Open the console at http://localhost or over https.");
      const sub = await getSubmission(request.address); // the root is read now, from the chain
      if (!sub) throw new Error("The commitment for this request was not found on chain.");
      const r = await runDownload({ request: request.address, cluster: CLUSTER, root: sub.merkleRoot, frameCount: request.numFrames, source: defaultSource(request.address), sampled, signal: abort.current.signal, onProgress: setFrames });
      if (r.zip) { saveZip(r.zip, zipName(request.address)); setSaved(zipName(request.address)); }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }

  const verified = frames?.filter((f) => f.status === "verified").length ?? 0;
  const rejected = frames?.filter((f) => f.status === "rejected") ?? [];
  const finished = frames !== null && !busy && !error;
  const total = request.numFrames;

  return (
    <section className="dataset" aria-label="Dataset">
      <h2>Dataset</h2>
      <div className="dlrow">
        <button className="btn primary" disabled={busy} onClick={start}>{busy ? "Verifying…" : finished ? "Download again" : "Download dataset"}</button>
        <p className="dim">Each frame is checked against the Merkle root on Solana before it is saved.</p>
      </div>
      {error && <Notice tone="fail" title="Download stopped">{error}</Notice>}
      {frames && (
        <div className="dlprogress" role="status" aria-live="polite">
          <div className="dlcount">
            {busy ? <>frame <Mono>{verified}</Mono> of <Mono>{total}</Mono> verified</> : <><Mono>{verified}</Mono> of <Mono>{total}</Mono> frames verified</>}
            {rejected.length > 0 && <span className="fail"> · {rejected.length} rejected</span>}
          </div>
          <div className="dlgrid" aria-hidden="true">
            {frames.map((f) => <i key={f.index} className={`dl ${f.status}`} title={`frame ${f.index}: ${f.status}`} />)}
          </div>
          {rejected.length > 0 && (
            <ul className="dlrejects">
              {rejected.map((f) => <li key={f.index}><Mono>frame {f.index}</Mono> rejected: {f.reason}</li>)}
            </ul>
          )}
          {finished && saved && <p className={rejected.length === 0 ? "dlsaved" : "dim"}>{rejected.length === 0 ? <>Saved <Mono>{saved}</Mono>. Every frame matches the Merkle root on chain.</> : <>Saved <Mono>{saved}</Mono> with the {verified} frames that passed; the rejected frames are not in it.</>}</p>}
          {finished && !saved && <p className="dim">Nothing was saved: no frame passed.</p>}
        </div>
      )}
    </section>
  );
}
