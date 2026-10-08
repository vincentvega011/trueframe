// SHORTCUT (localnet only): the "dev wallet" signs with keys/requester.json, which the Vite dev server hands out at
// /__dev/requester-key. This module is imported only behind a localnet check in wallet.tsx, so a devnet build contains neither
// this code nor the route (and the route exists only on `vite dev`). docs/LIMITATIONS.md.
import { Keypair, type Transaction } from "@solana/web3.js";
import type { AppWallet } from "./chain";

export async function loadBurner(): Promise<AppWallet> {
  const res = await fetch("/__dev/requester-key");
  if (!res.ok) throw new Error("Could not load keys/requester.json: run scripts/setup.sh and start the app with npm run dev.");
  const kp = Keypair.fromSecretKey(Uint8Array.from(await res.json()));
  return {
    kind: "burner",
    name: "Dev wallet (localnet)",
    publicKey: kp.publicKey,
    signTransaction: async (tx: Transaction) => {
      tx.partialSign(kp);
      return tx;
    },
  };
}
