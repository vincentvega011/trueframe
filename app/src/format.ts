import { USDC_DECIMALS } from "./config";

export function usdc(baseUnits: number | bigint | string): string {
  const n = BigInt(baseUnits);
  const base = 10n ** BigInt(USDC_DECIMALS);
  const whole = n / base;
  const frac = (n % base).toString().padStart(USDC_DECIMALS, "0").replace(/0+$/, "").padEnd(2, "0");
  return `${whole.toString()}.${frac}`;
}

export function shortHash(s: string, head = 8, tail = 6): string {
  return s.length <= head + tail + 1 ? s : `${s.slice(0, head)}…${s.slice(-tail)}`;
}

export function clock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString("en-GB", { hour12: false });
}

export function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
