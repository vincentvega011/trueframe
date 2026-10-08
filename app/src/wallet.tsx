import { WalletProvider, useWallet } from "@solana/wallet-adapter-react";
import { PublicKey, type Transaction } from "@solana/web3.js";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { AppWallet } from "./chain";
import { IS_LOCALNET } from "./config";

interface WalletCtx {
  wallet: AppWallet | null;
  /** Wallets the person can pick: the dev wallet on localnet, then whatever extensions are installed. */
  options: { id: string; name: string; kind: "burner" | "extension" }[];
  connect: (id: string) => Promise<void>;
  disconnect: () => Promise<void>;
  error: string | null;
}

const Ctx = createContext<WalletCtx>({ wallet: null, options: [], connect: async () => {}, disconnect: async () => {}, error: null });
export const useAppWallet = () => useContext(Ctx);

const BURNER_FLAG = "porf.burner.off";
const remembered = () => { try { return sessionStorage.getItem(BURNER_FLAG) !== "1"; } catch { return true; } };
const remember = (on: boolean) => { try { on ? sessionStorage.removeItem(BURNER_FLAG) : sessionStorage.setItem(BURNER_FLAG, "1"); } catch { /* private window */ } };

function Inner({ children }: { children: ReactNode }) {
  const adapter = useWallet();
  const [burner, setBurner] = useState<AppWallet | null>(null);
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(async (id: string) => {
    setError(null);
    try {
      if (id === "burner") {
        if (!IS_LOCALNET) throw new Error("The dev wallet exists only in localnet builds.");
        // The literal comparison lets the bundler drop this branch, and the burner chunk with it, from a devnet build.
        if (import.meta.env.VITE_CLUSTER !== "devnet") setBurner(await (await import("./burner")).loadBurner());
        remember(true);
        if (adapter.connected) await adapter.disconnect();
      } else {
        setBurner(null);
        adapter.select(id as never);
        await adapter.connect();
      }
    } catch (e) {
      setError((e as Error).message || "Could not connect the wallet.");
    }
  }, [adapter]);

  const disconnect = useCallback(async () => {
    setBurner(null);
    remember(false);
    if (adapter.connected) await adapter.disconnect();
  }, [adapter]);

  useEffect(() => {
    if (IS_LOCALNET && remembered()) void connect("burner");
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const extension: AppWallet | null = useMemo(() => {
    if (!adapter.connected || !adapter.publicKey || !adapter.signTransaction) return null;
    const sign = adapter.signTransaction;
    return { kind: "extension", name: adapter.wallet?.adapter.name ?? "Wallet", publicKey: new PublicKey(adapter.publicKey.toBase58()), signTransaction: (tx: Transaction) => sign(tx) as Promise<Transaction> };
  }, [adapter.connected, adapter.publicKey, adapter.signTransaction, adapter.wallet]);

  const options = useMemo(() => [
    ...(IS_LOCALNET ? [{ id: "burner", name: "Dev wallet", kind: "burner" as const }] : []),
    ...adapter.wallets.filter((w) => w.readyState === "Installed").map((w) => ({ id: w.adapter.name as string, name: w.adapter.name as string, kind: "extension" as const })),
  ], [adapter.wallets]);

  return <Ctx.Provider value={{ wallet: burner ?? extension, options, connect, disconnect, error }}>{children}</Ctx.Provider>;
}

export function WalletRoot({ children }: { children: ReactNode }) {
  return <WalletProvider wallets={[]} autoConnect={!IS_LOCALNET}><Inner>{children}</Inner></WalletProvider>;
}
