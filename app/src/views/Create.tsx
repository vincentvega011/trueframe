import { useEffect, useMemo, useState } from "react";
import { createRequest, getStakeBps, TxError, usdcBalance } from "../chain";
import { DEFAULTS, DEMO_FRAMES, DEMO_OCCLUSION, DEMO_SEED, IS_LOCALNET, MAX_FRAMES, POLL_MS, USDC_DECIMALS } from "../config";
import { usdc } from "../format";
import { usePoll } from "../hooks";
import { Hash, Notice, TxLink } from "../ui/bits";
import { useAppWallet } from "../wallet";
import { setLast } from "../App";

const sampleCount = (n: number) => (n >= 7 ? 7 : Math.min(5, n));
const randomSeed = () => String(BigInt(Math.floor(Math.random() * 2 ** 32)) * 4096n + BigInt(Math.floor(Math.random() * 4096)));

export function CreateView({ go }: { go: (to: string) => void }) {
  const { wallet } = useAppWallet();
  const [frames, setFrames] = useState(String(DEMO_FRAMES));
  const [occlusion, setOcclusion] = useState(DEMO_OCCLUSION);
  const [price, setPrice] = useState("1");
  const [seed, setSeed] = useState(DEMO_SEED);
  const [claimSecs, setClaimSecs] = useState(String(DEFAULTS.claimDuration));
  const [commitSecs, setCommitSecs] = useState(String(DEFAULTS.commitDuration));
  const [windowSecs, setWindowSecs] = useState(String(DEFAULTS.challengeWindowSecs));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ signature: string; request: string } | null>(null);

  const stake = usePoll(getStakeBps, 120_000, []);
  const balance = usePoll(async () => (wallet ? usdcBalance(wallet.publicKey)   : undefined), POLL_MS * 3, [wallet?.publicKey.toBase58(), done?.request]);

  const parsed = useMemo(() => {
    const problems: string[] = [];
    const n = Number(frames);
    if (!Number.isInteger(n) || n < 1) problems.push("Frames must be a whole number of at least 1.");
    else if (n > MAX_FRAMES) problems.push(`Frames can be at most ${MAX_FRAMES}.`);
    let unit = 0n;
    if (!/^\d+(\.\d{1,6})?$/.test(price) || Number(price) <= 0) problems.push(`Price per frame must be a positive amount with up to ${USDC_DECIMALS} decimals.`);
    else {
      const [w, f = ""] = price.split(".");
      unit = BigInt(w) * 10n ** BigInt(USDC_DECIMALS) + BigInt(f.padEnd(USDC_DECIMALS, "0"));
    }
    const secs = [["Claim deadline", claimSecs], ["Commit deadline", commitSecs], ["Challenge window", windowSecs]] as const;
    for (const [label, v] of secs) if (!/^\d+$/.test(v) || Number(v) < 1 || Number(v) > 604_800) problems.push(`${label} must be a whole number of seconds between 1 and 604800.`);
    let s = 0n;
    if (!/^\d+$/.test(seed) || BigInt(seed) >= 2n ** 64n) problems.push("Seed must be a whole number below 2^64.");
    else s = BigInt(seed);
    const escrow = unit * BigInt(Number.isInteger(n) && n > 0 ? n : 0);
    const stakeAmount = stake.value !== undefined ? (escrow * BigInt(stake.value)) / 10_000n : null;
    return { n, unit, seed: s, escrow, stakeAmount, problems };
  }, [frames, price, seed, stake.value, claimSecs, commitSecs, windowSecs]);

  const short = balance.value !== undefined && balance.value !== null && balance.value < parsed.escrow;
  const noAccount = balance.value === null;

  useEffect(() => { setDone(null); setError(null); }, [frames, occlusion, price, seed, claimSecs, commitSecs, windowSecs]);

  async function submit() {
    if (!wallet || parsed.problems.length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await createRequest(wallet, { claimSecs: Number(claimSecs), commitSecs: Number(commitSecs), windowSecs: Number(windowSecs), seed: parsed.seed, numFrames: parsed.n, occlusionLevel: occlusion, pricePerFrame: parsed.unit });
      setDone(out);
      setLast(out.request);
    } catch (e) {
      setError(e instanceof TxError ? e.explained.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const disabled = busy || !wallet || parsed.problems.length > 0 || short || noAccount;

  return (
    <div className="view">
      <header className="viewhead stacked"><h1>Create request</h1><p className="dim">Order a synthetic dataset. A node renders it, a verifier spot-checks it, and you only pay if it's real.</p></header>
      {!IS_LOCALNET && wallet?.kind === "extension" && (
        <Notice tone="wait" title="Phantom: switch on Testnet Mode">In Phantom open Settings, Developer Settings, Testnet Mode, and choose Solana Devnet. Without it Phantom treats this as mainnet and may warn about the transaction or refuse to sign it.</Notice>
      )}
      <div className="split">
        <form className="form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
          <label className="field">
            <span>Frames</span>
            <input inputMode="numeric" value={frames} onChange={(e) => setFrames(e.target.value)} className="mono" />
          </label>
          <fieldset className="field">
            <legend>Occlusion level</legend>
            <div className="segmented" role="radiogroup" aria-label="Occlusion level">
              {[0, 1, 2, 3].map((l) => (
                <button type="button" key={l} role="radio" aria-checked={occlusion === l} className={occlusion === l ? "on" : ""} onClick={() => setOcclusion(l)}>{l}</button>
              ))}
            </div>
            <small className="dim">{occlusion === 3 ? "Heavy: many objects sit behind shelving or other objects." : occlusion === 0 ? "None: objects stand in the open." : "Some objects are partly hidden."}</small>
          </fieldset>
          <label className="field">
            <span>Price per frame (USDC)</span>
            <input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} className="mono" />
          </label>
          <div className="field">
            <label htmlFor="seed">Seed</label>
            <input id="seed" inputMode="numeric" value={seed} onChange={(e) => setSeed(e.target.value)} className="mono" />
            <div className="chips">
              <button type="button" className={`chip ${seed === DEMO_SEED ? "on" : ""}`} onClick={() => setSeed(DEMO_SEED)}>Demo seed {DEMO_SEED}</button>
              <button type="button" className="chip" onClick={() => setSeed(randomSeed())}>Random seed</button>
            </div>
          </div>
          <details className="timing">
            <summary>Deadlines</summary>
            <div className="timingfields">
              <label className="field"><span>Claim deadline (s)</span><input inputMode="numeric" value={claimSecs} onChange={(e) => setClaimSecs(e.target.value)} className="mono" /></label>
              <label className="field"><span>Commit deadline (s)</span><input inputMode="numeric" value={commitSecs} onChange={(e) => setCommitSecs(e.target.value)} className="mono" /></label>
              <label className="field"><span>Challenge window (s)</span><input inputMode="numeric" value={windowSecs} onChange={(e) => setWindowSecs(e.target.value)} className="mono" /></label>
            </div>
            <small className="dim">Short deadlines let you watch a refund happen.</small>
          </details>
          <button className="btn primary" disabled={disabled} type="submit">{busy ? "Waiting for the network…" : "Create request"}</button>
          {!wallet && <p className="errtext">No wallet connected. Connect one in the left panel to create a request.</p>}
          {parsed.problems.map((p) => <p key={p} className="errtext">{p}</p>)}
          {short && <p className="errtext">Not enough USDC: this request escrows {usdc(parsed.escrow)} and the wallet holds {usdc(balance.value!)}.</p>}
          {noAccount && wallet && <p className="errtext">This wallet has no token account for the request mint, so it cannot pay the escrow.</p>}
        </form>

        <section className="panel summary" aria-label="Summary">
          <h2>What gets locked</h2>
          <dl className="facts">
            <div><dt>Escrow</dt><dd className="mono big">{usdc(parsed.escrow)} <small>USDC</small></dd></div>
            <div><dt>Node stake</dt><dd className="mono">{parsed.stakeAmount !== null ? usdc(parsed.stakeAmount) : "…"} <small>USDC, put up by the node</small></dd></div>
            <div><dt>Frames the verifier samples</dt><dd className="mono">{Number.isInteger(parsed.n) && parsed.n > 0 ? sampleCount(parsed.n) : "…"} <small>of {Number.isInteger(parsed.n) ? parsed.n : "…"}</small></dd></div>
            <div><dt>Wallet balance</dt><dd className="mono">{balance.value != null ? usdc(balance.value) : "…"} <small>USDC</small></dd></div>
            <div><dt>Deadlines</dt><dd className="mono">claim {claimSecs} s, commit {commitSecs} s, challenge window {windowSecs} s</dd></div>
          </dl>
          <p className="dim small">{IS_LOCALNET ? "Local validator, mock USDC." : "Devnet, mock USDC (no real value)."}</p>
        </section>
      </div>

      {error && <Notice tone="fail" title="The request was not created">{error}</Notice>}
      {done && (
        <Notice tone="info" title="Request created">
          <div className="rowline"><span className="dim">Transaction</span><TxLink sig={done.signature} /></div>
          <div className="rowline"><span className="dim">Request account</span><Hash value={done.request} /></div>
          <button className="btn" onClick={() => go(`/timeline/${done.request}`)}>Follow this request</button>
        </Notice>
      )}
    </div>
  );
}
