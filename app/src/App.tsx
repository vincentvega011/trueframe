import { useEffect, useState } from "react";
import { checkNetwork } from "./chain";
import { CLUSTER, RPC_HOST, SLOW_POLL_MS } from "./config";
import { useHashRoute, usePoll } from "./hooks";
import { Notice } from "./ui/bits";
import { WalletRoot, useAppWallet } from "./wallet";
import { CreateView } from "./views/Create";
import { ResultView } from "./views/Result";
import { TimelineView } from "./views/Timeline";
import { shortHash } from "./format";

const LAST = "porf.lastRequest";
const getLast = () => { try { return localStorage.getItem(LAST) ?? ""; } catch { return ""; } };
export const setLast = (a: string) => { try { localStorage.setItem(LAST, a); } catch { /* storage blocked: the link still works */ } };

function WalletChip() {
  const { wallet, options, connect, disconnect, error } = useAppWallet();
  const [open, setOpen] = useState(false);
  return (
    <div className="walletchip">
      {wallet ? (
        <>
          <div className="who"><span className="dot ok" />{wallet.name}</div>
          <div className="mono dim">{shortHash(wallet.publicKey.toBase58(), 6, 6)}</div>
          <button className="link" onClick={() => void disconnect()}>Disconnect</button>
        </>
      ) : (
        <>
          <div className="who"><span className="dot off" />No wallet connected</div>
          <button className="btn small" onClick={() => setOpen((o) => !o)}>Connect wallet</button>
          {open && (
            <ul className="walletlist">
              {options.length === 0 && <li className="dim">No wallet found. Install Phantom or Solflare and reload.</li>}
              {options.map((o) => <li key={o.id}><button className="link" onClick={() => { setOpen(false); void connect(o.id); }}>{o.name}</button></li>)}
            </ul>
          )}
        </>
      )}
      {error && <div className="errtext" role="alert">{error}</div>}
    </div>
  );
}

function Shell() {
  const [route, go] = useHashRoute();
  const [, view, addrFromRoute] = route.split("/");
  const [last, setLastState] = useState(getLast);
  const address = addrFromRoute || last;
  useEffect(() => { if (addrFromRoute) { setLast(addrFromRoute); setLastState(addrFromRoute); } }, [addrFromRoute]);
  const net = usePoll(checkNetwork, SLOW_POLL_MS * 2, []);

  const nav = [
    { id: "create", label: "Create request", to: "/create" },
    { id: "timeline", label: "Status timeline", to: address ? `/timeline/${address}` : "/timeline" },
    { id: "result", label: "Challenge result", to: address ? `/result/${address}` : "/result" },
  ];

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <div className="brandmark" aria-hidden="true"><svg viewBox="0 0 32 24"><rect x="1" y="1" width="30" height="22" rx="2" /><path d="M8 12.5l5 5 11-11" /></svg></div>
          <div><div className="brandname">Proof-of-Render</div><div className="dim brandsub">Verification console</div></div>
        </div>
        <nav aria-label="Views">
          {nav.map((n) => (
            <a key={n.id} href={`#${n.to}`} className={`navitem ${view === n.id ? "on" : ""}`} aria-current={view === n.id ? "page" : undefined}>{n.label}</a>
          ))}
        </nav>
        <div className="railfoot">
          <div className="cluster"><span className={`dot ${net.value ? "bad" : "ok"}`} /> {CLUSTER}<div className="mono dim small">{RPC_HOST}</div></div>
          <WalletChip />
        </div>
      </aside>
      <main className="main">
        {net.value && <Notice tone="fail" title="Network problem">{net.value}</Notice>}
        {view === "timeline" ? <TimelineView address={address} go={go} />
          : view === "result" ? <ResultView address={address} go={go} />
          : <CreateView go={go} />}
      </main>
    </div>
  );
}

export function App() {
  return <WalletRoot><Shell /></WalletRoot>;
}
