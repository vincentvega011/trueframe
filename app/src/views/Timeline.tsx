import { chainClockOffset, TxError, timeoutRefund } from "../chain";
import { isMissing, loadRequest } from "../explorer";
import { SnapshotNotice } from "../ui/snapshot";
import { useState } from "react";
import { explainError } from "../errors";
import { EXPLORER, POLL_MS, SLOW_POLL_MS } from "../config";
import { clock, shortHash, usdc } from "../format";
import { useNow, usePoll } from "../hooks";
import { deriveTimeline, refundable, TERMINAL, type Step } from "../timeline";
import { useAppWallet } from "../wallet";
import { Hash, Mono, Notice, TxLink, Waiting } from "../ui/bits";
import { RequestPicker } from "./RequestPicker";

function StepRow({ s, last }: { s: Step; last: boolean }) {
  const tone = s.key === "Settled" && s.state === "done" ? (s.label === "Paid" ? "ok" : s.label === "Slashed" ? "bad" : "neutral") : s.state === "done" ? "ok" : s.state;
  return (
    <li className={`step ${s.state} t-${tone} ${last ? "last" : ""}`} aria-current={s.state === "active" ? "step" : undefined}>
      <div className="stepmark" aria-hidden="true" />
      <div className="stepbody">
        <div className="stephead">
          <h2>{s.label}</h2>
          <span className="stepstate dim">{s.state === "done" ? "" : s.state === "skipped" ? "did not happen" : s.state === "active" ? "now" : "not yet"}</span>
        </div>
        {s.tx && (
          <div className="stepmeta">
            <Mono>{s.tx.blockTime ? clock(s.tx.blockTime) : "—"}</Mono>
            <span className="dim">slot</span><Mono>{s.tx.slot}</Mono>
            <TxLink sig={s.tx.signature} />
          </div>
        )}
        {s.fields.map((f) => (
          <div className="stepfield" key={f.label}>
            <span className="dim">{f.label}</span>
            {f.mono ? <Hash value={f.value} /> : <span className={f.tone ? `tone-${f.tone}` : ""}>{f.value}</span>}
            {f.label === "Verdict" && s.verdictTx && <TxLink sig={s.verdictTx.signature} />}
          </div>
        ))}
        {s.waiting && <Waiting text={s.waiting} />}
      </div>
    </li>
  );
}

function RefundPanel({ kind, request, onDone }: { kind: "unclaimed" | "uncommitted"; request: { address: string; requester: string; mint: string; escrowAmount: string; stakeAmount: string }; onDone: () => void }) {
  const { wallet } = useAppWallet();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function go() {
    if (!wallet) return;
    setBusy(true);
    setError(null);
    try {
      await timeoutRefund(wallet, request);
      onDone();
    } catch (e) {
      setError(e instanceof TxError ? e.explained.message : explainError(e).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Notice tone="wait" title={kind === "unclaimed" ? "Nobody claimed this request in time" : "The node did not commit in time"}>
      <p>{kind === "unclaimed"
        ? `Anyone can send the ${usdc(request.escrowAmount)} USDC escrow back to the requester.`
        : `Anyone can send the ${usdc(request.escrowAmount)} USDC escrow and the node's ${usdc(request.stakeAmount)} USDC stake back to the requester.`}</p>
      <button className="btn primary" onClick={() => void go()} disabled={busy || !wallet}>{busy ? "Waiting for the network…" : "Refund"}</button>
      {!wallet && <p className="errtext">No wallet connected. Connect one in the left panel to send the refund.</p>}
      {error && <p className="errtext" role="alert">{error}</p>}
    </Notice>
  );
}

export function TimelineView({ address, go }: { address: string; go: (to: string) => void }) {
  const wall = useNow();
  const offset = usePoll(chainClockOffset, SLOW_POLL_MS, []);
  const now = wall + (offset.value ?? 0);   // the chain's clock: the program judges deadlines by it
  // Explorer: a settled request never changes, so once it has been read live the polling stops.
  const data = usePoll(async () => (address ? loadRequest(address, { txs: true }) : null), POLL_MS, [address],
    (d) => EXPLORER && !!d && !isMissing(d) && d.accountsFromSnapshot === null && d.txsFromSnapshot === null && d.txs.length > 0 && TERMINAL.includes(d.request.status));

  const v = data.value && !isMissing(data.value) ? data.value : null;
  const missing = isMissing(data.value);
  return (
    <div className="view">
      <header className="viewhead">
        <h1>Status timeline</h1>
        <RequestPicker address={address} onPick={(a) => go(`/timeline/${a}`)} />
      </header>
      {!address && <Notice tone="info" title="No request selected">{EXPLORER ? "Pick a request above." : "Create a request first, or pick one above."}</Notice>}
      {data.error != null && !v && <Notice tone="fail" title="Could not read the chain">{String((data.error as Error).message ?? data.error)}</Notice>}
      {missing && <Notice tone="fail" title="Request not found">No request account exists at {address} on this cluster.</Notice>}
      <SnapshotNotice data={v} />
      {v && (
        <>
          <div className="timelinegrid">
            <ol className="timeline">
              {deriveTimeline(v.request, v.submission, v.txs, now).map((s, i, a) => <StepRow key={s.key} s={s} last={i === a.length - 1} />)}
            </ol>
            <aside className="panel reqfacts" aria-label="Request">
              <h2>Request</h2>
              <dl className="facts">
                <div><dt>Frames</dt><dd className="mono">{v.request.numFrames}</dd></div>
                <div><dt>Occlusion level</dt><dd className="mono">{v.request.occlusionLevel}</dd></div>
                <div><dt>Seed</dt><dd className="mono">{v.request.seed}</dd></div>
                <div><dt>Escrow</dt><dd className="mono">{usdc(v.request.escrowAmount)} <small>USDC</small></dd></div>
                <div><dt>Node stake</dt><dd className="mono">{usdc(v.request.stakeAmount)} <small>USDC</small></dd></div>
                <div><dt>Requester</dt><dd className="mono">{shortHash(v.request.requester, 6, 6)}</dd></div>
                <div><dt>Node</dt><dd className="mono">{/^1+$/.test(v.request.node) ? "not claimed" : shortHash(v.request.node, 6, 6)}</dd></div>
              </dl>
            </aside>
          </div>
          {(() => { const k = EXPLORER ? null : refundable(v.request, now - 1); return k ? <RefundPanel kind={k} request={v.request} onDone={data.reload} /> : null; })()}
          <div className="actions">
            {(v.request.status === "Challenged" || v.request.status === "Verified" || (TERMINAL.includes(v.request.status) && v.submission)) && (
              <button className="btn" onClick={() => go(`/result/${address}`)}>Open the challenge result</button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
