export type Cluster = "localnet" | "devnet";

export const CLUSTER: Cluster = import.meta.env.VITE_CLUSTER === "devnet" ? "devnet" : "localnet";
export const IS_LOCALNET = CLUSTER === "localnet";
export const RPC_URL = import.meta.env.VITE_RPC_URL ?? (IS_LOCALNET ? "http://127.0.0.1:8899" : "https://api.devnet.solana.com");
/** Only the host of the RPC: the full URL of a provider can carry an API key and must never be shown or logged. */
export const RPC_HOST = (() => { try { return new URL(RPC_URL).host; } catch { return "rpc"; } })();
/** How often the views read the chain. The public devnet RPC is rate-limited, so devnet polls gently; override with VITE_POLL_MS. */
export const POLL_MS = Number(import.meta.env.VITE_POLL_MS) > 0 ? Number(import.meta.env.VITE_POLL_MS) : IS_LOCALNET ? 2000 : 5000;
export const SLOW_POLL_MS = IS_LOCALNET ? 4000 : 20_000;
export const REPORT_URL = (import.meta.env.VITE_REPORT_URL ?? "http://127.0.0.1:8788").replace(/\/$/, "");
export const DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";

export const USDC_DECIMALS = 6;
export const DEMO_SEED = "5150";
export const DEMO_FRAMES = 20;
export const DEMO_OCCLUSION = 3;
// Same defaults as the Python client / demo (docs/PROTOCOL.md section 18).
export const DEFAULTS = { claimDuration: 900, commitDuration: 900, challengeWindowSecs: 60, width: 640, height: 480 };
// Limits mirrored from programs/proof-of-render/src/constants.rs, only to give a friendly message before the program does.
export const MAX_FRAMES = 1024;

export function txUrl(sig: string): string {
  return IS_LOCALNET
    ? `https://explorer.solana.com/tx/${sig}?cluster=custom&customUrl=${encodeURIComponent(RPC_URL)}`
    : `https://explorer.solana.com/tx/${sig}?cluster=devnet`;
}

/** The node's reveal server, where the dataset download fetches frames (docs/PROTOCOL.md section 16.4). */
export const REVEAL_URL = (import.meta.env.VITE_REVEAL_URL ?? "http://127.0.0.1:8787").replace(/\/$/, "");
/** If set, frames are read from a static folder of <i>.json files instead (the explorer build). */
export const FRAMES_URL = (import.meta.env.VITE_FRAMES_URL ?? "").replace(/\/$/, "");
