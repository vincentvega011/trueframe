// Shared helpers: paths, keypairs, IDL-driven PDAs and argument coercion, JSON conversion.
import * as fs from "fs";
import * as path from "path";
import { BN, Idl } from "@anchor-lang/core";
import { Keypair, PublicKey } from "@solana/web3.js";

// this file is py/porf/chain/cli/src/util.ts -> repo root is five levels up
export const REPO_ROOT = path.resolve(__dirname, "../../../../..");

export class CliError extends Error {
  constructor(public kind: string, message: string, public extra: Record<string, unknown> = {}) {
    super(message);
  }
}

export const rpcUrl = (): string => process.env.PORF_RPC_URL ?? "http://127.0.0.1:8899";
export const idlPath = (): string => process.env.PORF_IDL ?? path.join(REPO_ROOT, "target/idl/proof_of_render.json");

export function loadIdl(): Idl {
  const p = idlPath();
  if (!fs.existsSync(p)) throw new CliError("IdlNotFound", `IDL not found at ${p}; run \`anchor build\` first`);
  return JSON.parse(fs.readFileSync(p, "utf8")) as Idl;
}

/** Relative paths are resolved against the repo root so scripts and Python agree. */
export const resolvePath = (p: string): string => (path.isAbsolute(p) ? p : path.join(REPO_ROOT, p));

export function loadKeypair(p: string): Keypair {
  const full = resolvePath(p);
  if (!fs.existsSync(full)) throw new CliError("KeypairNotFound", `keypair file not found: ${full}`);
  return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(full, "utf8"))));
}

export function pubkey(value: unknown, field: string): PublicKey {
  try {
    return new PublicKey(String(value));
  } catch {
    throw new CliError("InvalidInput", `${field} is not a valid public key: ${String(value)}`);
  }
}

export function need<T = unknown>(args: Record<string, unknown>, field: string): T {
  if (args[field] === undefined || args[field] === null) throw new CliError("InvalidInput", `missing required field: ${field}`);
  return args[field] as T;
}

// ---------------------------------------------------------------- IDL-driven pieces

/** Seed bytes come from the IDL's `constants`, so they cannot drift from the program. */
export function seed(idl: Idl, name: string): Buffer {
  const c = (idl.constants ?? []).find((k) => k.name === name);
  if (!c) throw new CliError("IdlError", `IDL has no constant ${name}`);
  return Buffer.from(JSON.parse(c.value) as number[]);
}

export class Pdas {
  readonly programId: PublicKey;
  constructor(private idl: Idl) {
    this.programId = new PublicKey(idl.address);
  }
  private find = (seeds: Buffer[]) => PublicKey.findProgramAddressSync(seeds, this.programId)[0];
  config = () => this.find([seed(this.idl, "CONFIG_SEED")]);
  request = (requester: PublicKey, requestId: BN) =>
    this.find([seed(this.idl, "REQUEST_SEED"), requester.toBuffer(), requestId.toArrayLike(Buffer, "le", 8)]);
  escrow = (request: PublicKey) => this.find([seed(this.idl, "ESCROW_SEED"), request.toBuffer()]);
  stake = (request: PublicKey) => this.find([seed(this.idl, "STAKE_SEED"), request.toBuffer()]);
  submission = (request: PublicKey) => this.find([seed(this.idl, "SUBMISSION_SEED"), request.toBuffer()]);
}

const toCamel = (s: string) => s.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
const toSnake = (s: string) => s.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase());

/** Coerce a snake_case JSON object into the camelCase, BN-typed struct Anchor expects, using the IDL's own types. */
export function coerceStruct(idl: Idl, typeName: string, input: Record<string, unknown>): Record<string, unknown> {
  const def = (idl.types ?? []).find((t) => t.name === typeName);
  if (!def || def.type.kind !== "struct") throw new CliError("IdlError", `IDL has no struct ${typeName}`);
  const fields = (def.type as unknown as { fields: { name: string; type: unknown }[] }).fields;
  const out: Record<string, unknown> = {};
  for (const f of fields) {
    const raw = input[f.name];
    if (raw === undefined || raw === null) throw new CliError("InvalidInput", `${typeName}.${f.name} is required`);
    out[toCamel(f.name)] = coerceValue(f.type, raw, `${typeName}.${f.name}`);
  }
  const extra = Object.keys(input).filter((k) => !fields.some((f) => f.name === k));
  if (extra.length) throw new CliError("InvalidInput", `unknown field(s) for ${typeName}: ${extra.join(", ")}`);
  return out;
}

function coerceValue(type: unknown, raw: unknown, label: string): unknown {
  const t = String(type);
  try {
    if (["u64", "i64", "u128", "i128"].includes(t)) return new BN(String(raw), 10);
    if (["u8", "u16", "u32", "i8", "i16", "i32"].includes(t)) {
      const n = Number(raw);
      if (!Number.isInteger(n)) throw new Error("not an integer");
      return n;
    }
    if (t === "bool") return Boolean(raw);
    if (t === "pubkey") return new PublicKey(String(raw));
  } catch (e) {
    throw new CliError("InvalidInput", `${label}: ${(e as Error).message}`);
  }
  return raw;
}

export function hexToBytes32(hex: unknown, field: string): number[] {
  const s = String(hex);
  if (!/^[0-9a-fA-F]{64}$/.test(s)) throw new CliError("InvalidInput", `${field} must be 64 hex characters`);
  return Array.from(Buffer.from(s, "hex"));
}

// ---------------------------------------------------------------- JSON output

const pascal = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** PublicKey -> base58, BN -> decimal string, [u8;32] -> hex, {variant:{}} -> "Variant", keys -> snake_case. */
export function toJson(v: unknown): unknown {
  if (v === null || v === undefined) return v ?? null;
  if (v instanceof PublicKey) return v.toBase58();
  if (BN.isBN(v)) return (v as BN).toString(10);
  if (Array.isArray(v)) {
    if (v.length === 32 && v.every((x) => Number.isInteger(x) && x >= 0 && x < 256)) return Buffer.from(v).toString("hex");
    return v.map(toJson);
  }
  if (typeof v === "object") {
    const entries = Object.entries(v as Record<string, unknown>);
    if (entries.length === 1 && typeof entries[0][1] === "object" && entries[0][1] !== null && Object.keys(entries[0][1] as object).length === 0) {
      return pascal(entries[0][0]); // unit enum variant
    }
    return Object.fromEntries(entries.map(([k, val]) => [toSnake(k), toJson(val)]));
  }
  return v;
}
