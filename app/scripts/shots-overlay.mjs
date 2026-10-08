// Element screenshots of the overlay figure (with its legend) for finished requests: name=ADDRESS [name=ADDRESS ...]
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const out = resolve(dirname(fileURLToPath(import.meta.url)), "../../out/ui/shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await launch();
for (const pair of process.argv.slice(2)) {
  const [name, addr] = pair.split("=");
  const p = await newPage(browser);
  await p.goto(`http://127.0.0.1:5173/#/result/${addr}`);
  await p.locator(".strip .cell").first().waitFor({ timeout: 20000 });
  const bad = p.locator(".strip .cell.bad").first();
  if (await bad.count()) await bad.click();
  await sleep(1500);
  await p.locator(".detail .fig").first().screenshot({ path: `${out}/${name}_overlay.png` });
  await p.context().close();
  console.log("shot", name);
}
await browser.close();
