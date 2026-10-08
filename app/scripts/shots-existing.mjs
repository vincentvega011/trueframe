// Screenshots the timeline and result views of requests that already finished (their reports are read from the report server).
//   node scripts/shots-existing.mjs name=ADDRESS [name=ADDRESS ...]   -> out/ui/shots/<name>_timeline_settled.png, <name>_result.png, <name>_result_failing_frame.png
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const out = resolve(dirname(fileURLToPath(import.meta.url)), "../../out/ui/shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await launch();
for (const pair of process.argv.slice(2)) {
  const [name, addr] = pair.split("=");
  const p = await newPage(browser);
  await p.goto(`http://127.0.0.1:5173/#/timeline/${addr}`);
  await p.locator(".timeline li.step.last.done").waitFor({ timeout: 20000 });
  await sleep(1200);
  await p.screenshot({ path: `${out}/${name}_timeline_settled.png` });
  await p.goto(`http://127.0.0.1:5173/#/result/${addr}`);
  await p.locator(".strip .cell").first().waitFor({ timeout: 20000 });
  await sleep(2000);
  await p.screenshot({ path: `${out}/${name}_result.png` });
  const bad = p.locator(".strip .cell.bad").first();
  if (await bad.count()) {
    await bad.click();
    await sleep(800);
    await p.evaluate(() => window.scrollTo(0, document.querySelector(".strip").getBoundingClientRect().top + window.scrollY - 20));
    await sleep(300);
    await p.screenshot({ path: `${out}/${name}_result_failing_frame.png` });
  }
  await p.context().close();
  console.log("shot", name);
}
await browser.close();
