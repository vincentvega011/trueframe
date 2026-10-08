// Copies the generated IDL (anchor build) into the app so the UI always matches the deployed program.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const from = resolve(here, "../../target/idl/proof_of_render.json");
const to = resolve(here, "../src/idl/proof_of_render.json");
if (!existsSync(from)) {
  if (existsSync(to)) {
    console.warn("target/idl/proof_of_render.json not found; keeping the copy in src/idl (run `anchor build` to refresh it)");
    process.exit(0);
  }
  console.error("target/idl/proof_of_render.json not found: run `anchor build` first");
  process.exit(1);
}
mkdirSync(dirname(to), { recursive: true });
copyFileSync(from, to);
console.log("synced IDL ->", to);
