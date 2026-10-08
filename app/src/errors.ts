import idl from "./idl/proof_of_render.json";
import { CLUSTER, RPC_URL } from "./config";

export interface Explained {
  /** One plain sentence for the person using the app. */
  message: string;
  /** The program error name when there is one, e.g. DeadlineNotReached. */
  programError?: string;
  code?: number;
  logs?: string[];
}

const table = new Map<number, string>((idl.errors as { code: number; name: string }[]).map((e) => [e.code, e.name]));

/** Name for a program error code, from the loaded IDL (never hardcoded). */
export function errorName(code: number): string | undefined {
  return table.get(code);
}

/** Pulls the program error out of transaction logs or an error message: Anchor's own log line first, then the raw custom code. */
export function programErrorFrom(text: string): { name?: string; code?: number } | null {
  const anchor = /Error Code: (\w+)\. Error Number: (\d+)/.exec(text);
  if (anchor) return { name: anchor[1], code: Number(anchor[2]) };
  const custom = /custom program error: (0x[0-9a-fA-F]+)/.exec(text) ?? /"Custom":\s*(\d+)/.exec(text);
  if (custom) {
    const code = Number(custom[1]);
    return { name: errorName(code), code };
  }
  return null;
}

export function explainError(e: unknown, logs?: string[]): Explained {
  const err = e as { message?: string; name?: string; logs?: string[]; code?: number };
  const text = [err?.message ?? String(e), ...(logs ?? err?.logs ?? [])].join("\n");
  const found = programErrorFrom(text);
  if (found) {
    const label = found.name ?? "UnknownProgramError";
    return { message: `Transaction failed: ${label}${found.code !== undefined ? ` (${found.code})` : ""}`, programError: label, code: found.code, logs: logs ?? err?.logs };
  }
  if (/user rejected|rejected the request|WalletSign/i.test(text)) return { message: "You cancelled the signing request in your wallet." };
  if (/insufficient funds|0x1\b/.test(text) && /lamports|SOL|insufficient funds for fee/i.test(text)) return { message: "This wallet has too little SOL to pay the network fee." };
  if (/failed to fetch|NetworkError|ECONNREFUSED|fetch failed/i.test(text)) return { message: `Can't reach the RPC at ${RPC_URL}. ${CLUSTER === "localnet" ? "Is the local validator running? (scripts/localnet_up.sh)" : "Check your connection."}` };
  if (/blockhash not found|Account does not exist|could not find account/i.test(text)) return { message: `The network rejected the transaction. Check that your wallet is on ${CLUSTER === "localnet" ? "the local validator" : "devnet"}.` };
  return { message: `Transaction failed: ${err?.message ?? String(e)}`, logs: logs ?? err?.logs };
}
