import { listRequests } from "../chain";
import { shortHash } from "../format";
import { EXPLORER, EXPLORER_REQUESTS, SLOW_POLL_MS } from "../config";
import { usePoll } from "../hooks";

export function RequestPicker({ address, onPick }: { address: string; onPick: (a: string) => void }) {
  if (EXPLORER) {
    return (
      <label className="picker">
        <span className="dim">Request</span>
        <select value={address} onChange={(e) => onPick(e.target.value)} aria-label="Request">
          {EXPLORER_REQUESTS.map((r) => <option key={r.address} value={r.address}>{r.label}  {shortHash(r.address, 6, 4)}</option>)}
        </select>
      </label>
    );
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const { value } = usePoll(listRequests, SLOW_POLL_MS / (SLOW_POLL_MS > 4000 ? 1 : 2), []);
  const rows = value ?? [];
  return (
    <label className="picker">
      <span className="dim">Request</span>
      <select value={address} onChange={(e) => onPick(e.target.value)} aria-label="Request">
        {!rows.some((r) => r.address === address) && <option value={address}>{address ? shortHash(address, 8, 6) : "Choose a request"}</option>}
        {rows.map((r) => <option key={r.address} value={r.address}>{shortHash(r.address, 8, 6)}  {r.numFrames} frames  {r.status}</option>)}
      </select>
    </label>
  );
}
