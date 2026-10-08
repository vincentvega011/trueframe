/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_CLUSTER?: "localnet" | "devnet";
  readonly VITE_RPC_URL?: string;
  readonly VITE_REPORT_URL?: string;
  readonly VITE_POLL_MS?: string;
}
