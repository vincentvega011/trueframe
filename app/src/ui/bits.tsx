import { useEffect, useState, type ReactNode } from "react";
import { txUrl } from "../config";
import { shortHash } from "../format";

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`mono ${className}`}>{children}</span>;
}

/** A long hex value: shown whole when it fits, always selectable, full value in the tooltip. */
export function Hash({ value, short = false }: { value: string; short?: boolean }) {
  return <span className="mono hash" title={value}>{short ? shortHash(value) : value}</span>;
}

export function TxLink({ sig, label }: { sig: string; label?: string }) {
  return <a className="mono txlink" href={txUrl(sig)} target="_blank" rel="noreferrer" title={sig}>{label ?? shortHash(sig, 8, 6)}</a>;
}

/** Pixel-grid loader with elapsed time, for waits on the node and the verifier. */
export function Waiting({ text, since }: { text: string; since?: number }) {
  const [t0] = useState(() => since ?? Date.now() / 1000);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => { const i = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(i); }, []);
  const elapsed = Math.max(0, Math.floor(now - t0));
  return (
    <div className="waiting" role="status">
      <span className="pixels" aria-hidden="true">{Array.from({ length: 16 }, (_, i) => <i key={i} style={{ animationDelay: `${((i % 4) + Math.floor(i / 4)) * 120}ms` }} />)}</span>
      <span>{text}</span>
      <Mono className="elapsed">{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}</Mono>
    </div>
  );
}

export function Notice({ tone, title, children }: { tone: "fail" | "wait" | "info"; title: string; children?: ReactNode }) {
  return (
    <div className={`notice ${tone}`} role={tone === "fail" ? "alert" : "status"}>
      <strong>{title}</strong>
      {children && <div>{children}</div>}
    </div>
  );
}

export function Mark({ ok }: { ok: boolean }) {
  return ok ? (
    <svg className="mark ok" viewBox="0 0 20 20" aria-label="passed"><path d="M3.5 10.5l4.2 4.2L16.5 5.5" fill="none" strokeWidth="2.6" strokeLinecap="square" /></svg>
  ) : (
    <svg className="mark bad" viewBox="0 0 20 20" aria-label="failed"><path d="M4.5 4.5l11 11M15.5 4.5l-11 11" fill="none" strokeWidth="2.6" strokeLinecap="square" /></svg>
  );
}
