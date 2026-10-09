import { snapshotDate, type RequestData } from "../explorer";
import { Notice } from "./bits";

/** Amber notice when the explorer is not showing live chain data. Nothing when everything was read live. */
export function SnapshotNotice({ data }: { data: RequestData | null | undefined }) {
  if (!data) return null;
  if (data.accountsFromSnapshot) {
    return <Notice tone="wait" title="Snapshot, not live data">Devnet no longer has this account. Showing a snapshot taken on {snapshotDate(data.accountsFromSnapshot)}; transaction links may not resolve.</Notice>;
  }
  if (data.txsFromSnapshot) {
    return <Notice tone="wait" title="Transaction list from a snapshot">The public RPC returned no transaction history for this request. The transactions below come from a snapshot taken on {snapshotDate(data.txsFromSnapshot)}; links may not resolve.</Notice>;
  }
  return null;
}
