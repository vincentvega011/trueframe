// @anchor-lang/core 1.1.2 wraps a failed transaction in `new SendTransactionError(message, logs)` (provider.js, utils/rpc.js),
// the OLD positional signature. @solana/web3.js 1.99 takes `{action, signature, transactionMessage, logs}`, destructures the
// string, finds no action, and builds the message "Unknown action 'undefined'" with no logs: the program's error name is lost.
// This only happens when the transaction passes the preflight simulation and then fails on chain (for example a deadline
// crossed between the two), so it showed up as an intermittent failure. Accept both forms; this module must be imported before
// anything sends a transaction. It patches the CommonJS export that Anchor reads at call time.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const web3 = require("@solana/web3.js");
const Original = web3.SendTransactionError;

export class CompatSendTransactionError extends Original {
  constructor(first: any, logs?: string[]) {
    super(typeof first === "string" ? { action: "send", signature: "", transactionMessage: first, logs } : first);
  }
}

if (web3.SendTransactionError === Original) web3.SendTransactionError = CompatSendTransactionError;
export const SendTransactionErrorCompat = CompatSendTransactionError;
