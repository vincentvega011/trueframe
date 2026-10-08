import { listRequests } from "../chain";
import { shortHash } from "../format";
import { SLOW_POLL_MS } from "../config";
import { usePoll } from "../hooks";

export function RequestPicker({ address, onPick }: { address: string; onPick: (a: string) => void }) {
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
