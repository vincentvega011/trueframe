import { useEffect, useState } from "react";
import { isMissing, loadRequest } from "../explorer";
import { SnapshotNotice } from "../ui/snapshot";
import { explainFrame } from "../explain";
import { EXPLORER, POLL_MS, REPORT_URL } from "../config";
import { TERMINAL } from "../timeline";
import { shortHash, usdc } from "../format";
import { usePoll } from "../hooks";
import { CHECK_LABELS, fetchReport, imageUrl, type FrameReport, type Report } from "../report";
import { Hash, Mark, Mono, Notice, TxLink, Waiting } from "../ui/bits";
import { DownloadPanel } from "./Download";
import { RequestPicker } from "./RequestPicker";

const WAITING: Record<string, string> = {
  Open: "Waiting for a node to claim the request",
  Claimed: "The node is rendering the frames",
  Committed: "Waiting for the verifier to open the challenge",
  Challenged: "The verifier is checking the sampled frames",
  Verified: "Verdict Pass is on chain; waiting for the challenge window to end",
};

function Verdict({ report, status }: { report: Report | null; status: string | null }) {
  const out = report?.outcome;
  const fail = report?.verdict === "Fail" || status === "Slashed";
  const pass = report?.verdict === "Pass";
  const word = status === "Slashed" ? "Slashed" : status === "Paid" ? "Paid" : fail ? "Failed" : pass ? "Verified" : status === "Expired" ? "Expired" : "Checking";
  const tone = fail ? "fail" : pass || status === "Paid" ? "pass" : "wait";
  return (
    <section className={`verdict ${tone}`} aria-label="Verdict">
      <div className="verdictword">{word}</div>
      <div className="verdictbody">
        {out?.action === "slashed" && (
          <dl className="amounts">
            <div><dt>Refunded to requester</dt><dd className="mono">{usdc(out.escrow_moved)} <small>USDC escrow</small></dd></div>
            <div><dt>Slashed from the node</dt><dd className="mono fail">{usdc(out.stake_moved)} <small>USDC stake, to the requester</small></dd></div>
          </dl>
        )}
        {out?.action === "paid" && (
          <dl className="amounts">
            <div><dt>Paid to the node</dt><dd className="mono pass">{usdc(out.escrow_moved)} <small>USDC escrow</small></dd></div>
            <div><dt>Stake returned</dt><dd className="mono">{usdc(out.stake_moved)} <small>USDC</small></dd></div>
          </dl>
        )}
        {!out && status === "Paid" && <p>The node has been paid. The amounts appear here when the verifier's report catches up.</p>}
        {!out && status === "Slashed" && <p>The node has been slashed. The amounts appear here when the verifier's report catches up.</p>}
        {!out && report?.verdict && status !== "Paid" && status !== "Slashed" && <p>Verdict {report.verdict} is on chain. {report.verdict === "Pass" ? "The node is paid when the challenge window ends." : "The slash is being sent."}</p>}
        {!report?.verdict && status !== "Expired" && status !== "Paid" && status !== "Slashed" && <Waiting text={WAITING[status ?? ""] ?? "Reading the request from the chain"} />}
        {status === "Expired" && !report && <p>This request expired before anyone could be checked. See the timeline for the refund.</p>}
      </div>
    </section>
  );
}

function failedNames(f: FrameReport): string[] {
  return f.checks.filter((c) => !c.passed).map((c) => CHECK_LABELS[c.name] ?? c.name);
}

function Strip({ request, frames, selected, onSelect }: { request: string; frames: FrameReport[]; selected: number; onSelect: (i: number) => void }) {
  return (
    <div className="strip" role="tablist" aria-label="Sampled frames">
      {frames.map((f) => {
        const bad = !f.passed;
        const thumb = f.images?.overlay ?? f.images?.rgb;
        return (
          <button key={f.index} role="tab" aria-selected={selected === f.index} className={`cell ${bad ? "bad" : "good"} ${selected === f.index ? "sel" : ""}`} onClick={() => onSelect(f.index)}>
            <div className="picture">
              {thumb ? <img src={imageUrl(request, f.images!.rgb ?? thumb)} alt={`Frame ${f.index}`} loading="lazy" /> : <div className="nopic">{f.status === "reveal_unavailable" ? "node offline" : "no picture"}</div>}
              <span className="stamp">{f.status === "checked" ? (bad ? "FAIL" : "PASS") : f.status === "reveal_unavailable" ? "OFFLINE" : "SKIPPED"}</span>
            </div>
            <div className="cellfoot"><Mono>frame {f.index}</Mono>{bad && f.status === "checked" && <span className="cellwhy">{failedNames(f).length} failed</span>}</div>
          </button>
        );
      })}
    </div>
  );
}

function Detail({ request, frame }: { request: string; frame: FrameReport }) {
  const img = frame.images ?? {};
  const seg = img.seg_node && img.seg_reference && img.seg_diff;
  const sentence = explainFrame(frame);
  return (
    <section className="detailwrap" aria-label={`Frame ${frame.index} checks`}>
      {sentence && <p className="plain" role="note">{sentence}</p>}
      {seg && (
        <div className="segrow">
          <figure className="fig"><img src={imageUrl(request, img.seg_node)} alt="Segmentation the node submitted" /><figcaption>Node's segmentation, frame <Mono>{frame.index}</Mono></figcaption></figure>
          <figure className="fig"><img src={imageUrl(request, img.seg_reference)} alt="Segmentation the verifier re-rendered" /><figcaption>Verifier's re-render from the seed</figcaption></figure>
          <figure className="fig"><img src={imageUrl(request, img.seg_diff)} alt="Pixels that differ" /><figcaption><span className="swatch r" /> pixels that differ</figcaption></figure>
        </div>
      )}
      <div className="detail">
        <div className="checks">
          <h2>Frame <Mono>{frame.index}</Mono></h2>
          {frame.status !== "checked" && <Notice tone="fail" title={frame.status === "reveal_unavailable" ? "The node did not serve this frame" : "Not checked"}>{frame.reason}</Notice>}
          <ul className="taskrows">
            {frame.checks.map((c) => (
              <li key={c.name} className={c.passed ? "ok" : "bad"}>
                <Mark ok={c.passed} />
                <div>
                  <div className="checkname">{CHECK_LABELS[c.name] ?? c.name}</div>
                  <div className="checkdetail">{c.detail}</div>
                </div>
              </li>
            ))}
          </ul>
        </div>
        {img.overlay && (
          <figure className="fig">
            <img src={imageUrl(request, img.overlay)} alt={`Frame ${frame.index} with boxes`} />
            <figcaption className="legend">
              <span><i className="sw solid g" /> node's label</span>
              <span><i className="sw dashed grey" /> expected from scene</span>
              <span><i className="sw dashed dim" /> hidden (correctly unlabelled)</span>
              {!frame.passed && <span><i className="sw solid r" /> label that fails a check</span>}
            </figcaption>
          </figure>
        )}
      </div>
    </section>
  );
}

export function ResultView({ address, go }: { address: string; go: (to: string) => void }) {
  const chain = usePoll(async () => (address ? loadRequest(address, { txs: false }) : null), POLL_MS, [address],
    (d) => EXPLORER && !!d && !isMissing(d) && d.accountsFromSnapshot === null && TERMINAL.includes(d.request.status));
  const data = chain.value && !isMissing(chain.value) ? chain.value : null;
  const status = data?.request.status ?? null;
  const rep = usePoll(async () => (address ? fetchReport(address) : null), 1500, [address], (r) => r?.kind === "ok" && !!r.report.outcome);
  const report = rep.value?.kind === "ok" ? rep.value.report : null;
  const frames = report?.frames ?? [];
  const firstBad = frames.find((f) => !f.passed)?.index;
  const [pick, setPick] = useState<number | null>(null);
  const [reasonsOpen, setReasonsOpen] = useState(false);
  useEffect(() => { setPick(null); setReasonsOpen(false); }, [address]);
  const selected = pick ?? firstBad ?? frames[0]?.index ?? -1;
  const frame = frames.find((f) => f.index === selected);
  const gone = rep.value?.kind === "unreachable";

  return (
    <div className="view">
      <header className="viewhead">
        <h1>Challenge result</h1>
        <RequestPicker address={address} onPick={(a) => go(`/result/${a}`)} />
      </header>
      {!address && <Notice tone="info" title="No request selected">{EXPLORER ? "Pick a request above." : "Create a request first, or pick one above."}</Notice>}
      <SnapshotNotice data={data} />
      {address && <Verdict report={report} status={status} />}
      {gone && <Notice tone="wait" title="Can't reach the report server">Start the verifier with <Mono>--serve</Mono> (it listens on {REPORT_URL}). {rep.value?.kind === "unreachable" && rep.value.reason}</Notice>}
      {rep.value?.kind === "missing" && address && !["Open", "Claimed"].includes(status ?? "") && <Notice tone="wait" title="No verifier report yet">The verifier has not written a report for this request. Reports of finished requests stay on disk, so they appear again when the report server is running.</Notice>}
      {report?.verification_error && <Notice tone="fail" title="The verifier could not finish">{report.verification_error}</Notice>}
      {report?.sample && (
        <div className="sampleline">
          <span className="dim">Sample seed</span><Hash value={report.sample.seed} />
          <span className="dim">slot</span><Mono>{report.sample.slot}</Mono>
          <span className="dim">frames</span><Mono>{report.sample.indices.join(", ")}</Mono>
        </div>
      )}
      {address && <DownloadPanel data={data} sampled={report?.sample?.indices ?? null} />}
      {frames.length > 0 && <Strip request={address} frames={frames} selected={selected} onSelect={setPick} />}
      {frame && <Detail request={address} frame={frame} />}
      {report?.verdict_reasons && report.verdict_reasons.length > 0 && (
        <details className="reasons" key={address} onToggle={(e) => setReasonsOpen(e.currentTarget.open)}>
          <summary>{reasonsOpen ? "Hide technical details" : "Show technical details"}</summary>
          <h2>Why it failed</h2>
          <ul>{report.verdict_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        </details>
      )}
      {report?.transactions && Object.keys(report.transactions).length > 0 && (
        <section className="txs"><h2>Transactions</h2>
          {Object.entries(report.transactions).map(([k, sig]) => <div className="rowline" key={k}><span className="dim">{k.replace(/_/g, " ")}</span><TxLink sig={sig} label={shortHash(sig, 10, 8)} /></div>)}
        </section>
      )}
    </div>
  );
}
