/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_CLUSTER?: "localnet" | "devnet";
  readonly VITE_MODE?: "explorer";
  readonly VITE_RPC_URL?: string;
  readonly VITE_REPORT_URL?: string;
  readonly VITE_POLL_MS?: string;
  readonly VITE_REVEAL_URL?: string;
  readonly VITE_FRAMES_URL?: string;
}
