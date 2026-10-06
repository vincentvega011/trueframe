// Turn any failure into {name, code?, message, logs?}. Program error names come from the logs
// Anchor emits, or from the IDL's own `errors` table at runtime; no error number is hardcoded here.
import { AnchorError, Idl, LangErrorCode } from "@anchor-lang/core";
import { CliError } from "./util";

export interface ErrorJson {
  name: string;
  code?: number;
  message: string;
  logs?: string[];
  kind: "program" | "input" | "rpc" | "other";
}

const ANCHOR_LOG = /Error Code: (\w+)\. Error Number: (\d+)\. Error Message: (.*?)\.?$/;
const CUSTOM_CODE = /custom program error: 0x([0-9a-fA-F]+)/;
const CUSTOM_JSON = /"Custom":\s*(\d+)/;

function nameForCode(idl: Idl, code: number): { name: string; message?: string } | undefined {
  const own = (idl.errors ?? []).find((e) => e.code === code);
  if (own) return { name: own.name, message: own.msg };
  // Anchor's own table is name -> number, so search it by value
  const lang = Object.entries(LangErrorCode as unknown as Record<string, number>).find(([, n]) => n === code);
  return lang ? { name: lang[0] } : undefined;
}

async function collectLogs(e: any): Promise<string[] | undefined> {
  const direct = e?.logs ?? e?.transactionLogs ?? e?.error?.logs ?? e?.simulationResponse?.logs;
  if (Array.isArray(direct)) return direct as string[];
  return undefined;
}

export async function normalizeError(e: unknown, idl: Idl | undefined): Promise<ErrorJson> {
  if (e instanceof CliError) {
    const rpc = e.kind === "RpcError";
    return { name: e.kind, message: e.message, kind: rpc ? "rpc" : e.kind === "InvalidInput" ? "input" : "other" };
  }
  const any = e as any;
  const logs = await collectLogs(any);
  const message = String(any?.message ?? e);

  // 1. Anchor already decoded it
  if (e instanceof AnchorError || any?.error?.errorCode?.code) {
    return {
      name: String(any.error.errorCode.code),
      code: Number(any.error.errorCode.number),
      message: String(any.error.errorMessage ?? message),
      logs,
      kind: "program",
    };
  }
  // 2. Anchor's log line carries the name and number
  for (const line of logs ?? []) {
    const m = ANCHOR_LOG.exec(line);
    if (m) return { name: m[1], code: Number(m[2]), message: m[3], logs, kind: "program" };
  }
  // 3. a bare custom error code: look the name up in the IDL, then in Anchor's own table
  const hay = [message, ...(logs ?? [])].join("\n");
  const hex = CUSTOM_CODE.exec(hay);
  const dec = CUSTOM_JSON.exec(hay);
  const code = hex ? parseInt(hex[1], 16) : dec ? Number(dec[1]) : undefined;
  if (code !== undefined && idl) {
    const found = nameForCode(idl, code);
    if (found) return { name: found.name, code, message: found.message ?? message, logs, kind: "program" };
    return { name: "UnknownProgramError", code, message, logs, kind: "program" };
  }
  if (/fetch failed|ECONNREFUSED|failed to get|Unable to connect/i.test(message)) {
    return { name: "RpcError", message, kind: "rpc" };
  }
  return { name: logs ? "TransactionFailed" : "Error", message, logs, kind: "other" };
}
