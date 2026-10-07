// Usage: tsx src/cli.ts <command> ['<json args>']
// stdout: one JSON object. Success {"ok":true,...}; failure {"ok":false,"error":{name,code?,message,logs?,kind}} and exit code 1
// (program/RPC/other errors) or 2 (bad input / unknown command).
// Env: PORF_RPC_URL (default http://127.0.0.1:8899), PORF_IDL (default <repo>/target/idl/proof_of_render.json).
import "./compat"; // first: repairs how Anchor builds SendTransactionError
import { COMMANDS, Ctx } from "./commands";
import { normalizeError } from "./errors";
import { CliError } from "./util";

async function run(): Promise<{ out: unknown; code: number }> {
  const [command, raw] = process.argv.slice(2);
  let ctx: Ctx | undefined;
  try {
    if (!command || !COMMANDS[command]) {
      throw new CliError("InvalidInput", `unknown command ${JSON.stringify(command)}; available: ${Object.keys(COMMANDS).join(", ")}`);
    }
    let args: Record<string, unknown> = {};
    if (raw) {
      try {
        args = JSON.parse(raw);
      } catch (e) {
        throw new CliError("InvalidInput", `arguments must be a JSON object: ${(e as Error).message}`);
      }
      if (typeof args !== "object" || args === null || Array.isArray(args)) throw new CliError("InvalidInput", "arguments must be a JSON object");
    }
    ctx = new Ctx();
    const result = await COMMANDS[command](ctx, args);
    return { out: { ok: true, ...result }, code: 0 };
  } catch (e) {
    const error = await normalizeError(e, ctx?.idl);
    return { out: { ok: false, error }, code: error.kind === "input" ? 2 : 1 };
  }
}

run().then(({ out, code }) => process.stdout.write(JSON.stringify(out) + "\n", () => process.exit(code)));
