// Screenshots the built explorer (app/dist-explorer) served locally, at 1920x1080, against the live public devnet RPC, plus the
// snapshot fallback with the RPC blocked.   python3 -m http.server 5180 -d dist-explorer  &&  node scripts/shots-explorer.mjs
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const out = resolve(dirname(fileURLToPath(import.meta.url)), "../../out/ui/shots");
const SITE = process.argv[2] ?? "http://127.0.0.1:5180";
const CHEAT = "EDN2pWCD2DJ8Yo9YxP5P2SrasK11oF7UxtbhF9tc3C4y", HONEST = "FUbhyAU19SKaFB4tNfKU4URWwksVAL9jJorfeTVo5d2j";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (m) => console.log(`[${new Date().toLocaleTimeString("en-GB")}] ${m}`);
async function until(fn, what, timeout = 90_000) {
  const t0 = Date.now();
  for (;;) { if (await fn().catch(() => null)) return; if (Date.now() - t0 > timeout) throw new Error(`timed out waiting for ${what}`); await sleep(300); }
}
const has = (p, re) => () => p.locator("body").innerText().then((t) => re.test(t));
const shot = async (p, name) => { await p.screenshot({ path: `${out}/${name}.png` }); log(`screenshot ${name}`); };

const browser = await launch();
try {
  // 1. opens on the cheating request, live from devnet
  let p = await newPage(browser);
  const reqs = [];
  p.on("request", (r) => { if (!r.url().startsWith(SITE) && !r.url().startsWith("data:")) reqs.push(new URL(r.url()).host); });
  await p.goto(`${SITE}/`);
  await until(has(p, /Slashed/), "the cheat timeline");
  await until(has(p, /Frames\s+20/), "the facts");
  await sleep(1200);
  const body = await p.locator("body").innerText();
  const checks = {
    banner: /TrueFrame is live on Solana devnet/.test(body),
    programLink: (await p.locator("a[href*='explorer.solana.com/address/7jhnpAu8H9rtDhEyXRmFCicjGLUepXoafE4SiE7EvDLw?cluster=devnet']").count()) === 1,
    noCreate: !/Create request/.test(body), noWallet: !/Connect wallet|No wallet|Disconnect/.test(body), noRefund: !/Refund/.test(body),
    defaultCheat: /Cheating node, slashed/.test(await p.locator("select").innerText()) && (await p.locator("select").inputValue()) === CHEAT,
    explorerLinks: await p.locator("a[href*='explorer.solana.com/tx/']").evaluateAll((as) => as.length > 0 && as.every((a) => a.href.endsWith("?cluster=devnet"))),
  };
  log(`checks ${JSON.stringify(checks)}`);
  if (!Object.values(checks).every(Boolean)) throw new Error("explorer checks failed");
  await shot(p, "explorer_cheat_timeline");
  log(`hosts contacted: ${[...new Set(reqs)].join(", ")}`);

  // 2. result views, with a hard reload on the hash route
  await p.goto(`${SITE}/#/result/${CHEAT}`); await p.reload();
  await until(has(p, /Slashed/), "the cheat result"); await until(async () => (await p.locator(".strip .cell").count()) > 0, "the strip");
  await until(async () => (await p.locator(".strip img").evaluateAll((is) => is.every((i) => i.complete && i.naturalWidth > 0))), "images");
  await sleep(800);
  if (/Download dataset/.test(await p.locator("body").innerText())) throw new Error("slashed request must not offer a download");
  await shot(p, "explorer_cheat_result");
  await p.goto(`${SITE}/#/timeline/${HONEST}`);
  await until(has(p, /Paid/), "the honest timeline"); await until(has(p, /Frames\s+20/), "facts"); await sleep(1200);
  await shot(p, "explorer_honest_timeline");

  // 3. honest result and the dataset download, verified against the live on-chain root
  await p.goto(`${SITE}/#/result/${HONEST}`);
  await until(has(p, /Paid/), "the honest result"); await until(async () => (await p.getByRole("button", { name: "Download dataset" }).count()) > 0, "the download button");
  await until(async () => (await p.locator(".strip img").evaluateAll((is) => is.length > 0 && is.every((i) => i.complete && i.naturalWidth > 0))), "images");
  await shot(p, "explorer_honest_result");
  const dl = p.waitForEvent("download", { timeout: 120_000 });
  await p.getByRole("button", { name: "Download dataset" }).click();
  const file = await dl;
  await until(has(p, /20 of 20 frames verified/), "20 of 20", 120_000);
  await sleep(500);
  await p.locator(".dataset").scrollIntoViewIfNeeded();
  await shot(p, "explorer_download_complete");
  await file.saveAs(`${out}/../explorer_${file.suggestedFilename()}`);
  log(`saved ${file.suggestedFilename()}; success line: ${(await p.locator(".dlsaved").innerText())}`);
  await p.context().close();

  // 4. snapshot fallback: the public RPC is unreachable
  p = await newPage(browser);
  await p.route(/api\.devnet\.solana\.com/, (route) => route.abort());
  await p.goto(`${SITE}/#/timeline/${CHEAT}`);
  await until(has(p, /Devnet no longer has this account/), "the snapshot notice");
  await until(has(p, /Slashed/), "snapshot timeline"); await sleep(1200);
  await shot(p, "explorer_snapshot_fallback");
  await p.goto(`${SITE}/#/result/${HONEST}`);
  await until(has(p, /Paid/), "snapshot result"); await until(async () => (await p.getByRole("button", { name: "Download dataset" }).count()) > 0, "button");
  await p.getByRole("button", { name: "Download dataset" }).click();
  await until(has(p, /20 of 20 frames verified/), "20 of 20 against the snapshot root", 120_000); await sleep(500);
  await p.locator(".dataset").scrollIntoViewIfNeeded();
  await shot(p, "explorer_snapshot_download");
  log(`snapshot success line: ${(await p.locator(".dlsaved").innerText())}`);
} finally { await browser.close(); }
