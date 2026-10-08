// Screenshots of the devnet build (read-only views need no wallet) plus a check of its Explorer links.
//   node scripts/shots-devnet.mjs honest=ADDRESS cheat=ADDRESS   (needs `npm run dev:devnet` and a report server on :8788)
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const out = resolve(dirname(fileURLToPath(import.meta.url)), "../../out/ui/shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await launch();
const create = await newPage(browser);
await create.goto("http://127.0.0.1:5173/#/create");
await sleep(5000);
await create.screenshot({ path: `${out}/devnet_create_no_wallet.png` });
console.log("create view cluster line:", (await create.locator(".cluster").innerText()).replace(/\n/g, " "));
await create.context().close();
for (const pair of process.argv.slice(2)) {
  const [name, addr] = pair.split("=");
  const p = await newPage(browser);
  await p.goto(`http://127.0.0.1:5173/#/timeline/${addr}`);
  await p.locator(".timeline li.step.last.done").waitFor({ timeout: 60000 });
  await sleep(2500);
  const links = await p.locator("a.txlink").evaluateAll((as) => as.map((a) => a.href));
  console.log(name, "explorer links:", links.length, links[0]);
  await p.screenshot({ path: `${out}/devnet_${name}_timeline.png` });
  await p.goto(`http://127.0.0.1:5173/#/result/${addr}`);
  await p.locator(".strip .cell").first().waitFor({ timeout: 60000 });
  await sleep(2500);
  await p.screenshot({ path: `${out}/devnet_${name}_result.png` });
  const bad = p.locator(".strip .cell.bad").first();
  if (await bad.count()) {
    await bad.click(); await sleep(800);
    await p.evaluate(() => window.scrollTo(0, document.querySelector(".strip").getBoundingClientRect().top + window.scrollY - 20));
    await sleep(300);
    await p.screenshot({ path: `${out}/devnet_${name}_result_failing_frame.png` });
  }
  await p.context().close();
}
await browser.close();
