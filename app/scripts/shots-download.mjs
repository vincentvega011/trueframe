// Gate run for the dataset download on localnet: a fresh honest request reaches Paid, the browser downloads and verifies all
// frames, then one served file is changed on disk and the browser must reject exactly that frame. The file is restored after.
//   (localnet up, `npm run dev` running, verifier running with --serve)   node scripts/shots-download.mjs
import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync, openSync, readdirSync, rmSync, statSync, writeFileSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const shots = join(root, "out/ui/shots"), dl = join(root, "out/ui/download");
mkdirSync(shots, { recursive: true }); mkdirSync(dl, { recursive: true });
const APP = "http://127.0.0.1:5173";
const TAMPER = 7;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (m) => console.log(`[${new Date().toLocaleTimeString("en-GB")}] ${m}`);
const bodyHas = (p, re) => () => p.locator("body").innerText().then((t) => re.test(t));
async function until(fn, what, timeout = 600_000, every = 250) {
  const t0 = Date.now();
  for (;;) { if (await fn().catch(() => null)) return; if (Date.now() - t0 > timeout) throw new Error(`timed out waiting for ${what}`); await sleep(every); }
}
const findFile = (dir, rel) => {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) { const hit = findFile(p, rel); if (hit) return hit; }
    else if (p.endsWith(rel)) return p;
  }
  return null;
};

const browser = await launch();
let node;
try {
  const p = await newPage(browser);
  await p.goto(`${APP}/#/create`);
  await until(bodyHas(p, /Dev wallet/), "the dev wallet");
  await p.getByRole("radio", { name: "3" }).click();
  await p.locator("#seed").fill("5150");
  await until(bodyHas(p, /Wallet balance\s*\d/), "the wallet balance");
  await p.getByRole("button", { name: "Create request" }).click();
  await until(bodyHas(p, /Request created/), "the created notice", 60_000);
  const address = (await p.locator(".notice .hash").first().innerText()).trim();
  log(`request ${address}`);

  const nodeOut = "out/ui/node_dl";
  rmSync(join(root, nodeOut), { recursive: true, force: true });
  node = spawn(`${root}/py/.venv/bin/python`, ["-m", "porf.node.client", "--keypair", "keys/node.json", "--out", nodeOut, "--poll", "1", "--port", "8787", "--only-request", address],
    { cwd: root, stdio: ["ignore", openSync(`${root}/out/ui/node_dl.log`, "w"), openSync(`${root}/out/ui/node_dl.log`, "w")] });

  await p.goto(`${APP}/#/result/${address}`);
  await until(bodyHas(p, /\bPaid\b/), "Paid", 900_000, 500);
  await until(async () => (await p.getByRole("button", { name: "Download dataset" }).count()) > 0, "the download button");
  log("Paid; download button visible");

  // slow each frame a little so the in-progress state can be photographed
  await p.route("**/frame/**", async (route) => { await sleep(450); await route.continue(); });
  const dl1 = p.waitForEvent("download");
  await p.getByRole("button", { name: "Download dataset" }).click();
  await until(bodyHas(p, /frame \d+ of 20 verified/), "progress");
  await sleep(1300);
  await p.locator(".dataset").scrollIntoViewIfNeeded();
  await p.screenshot({ path: `${shots}/download_in_progress.png` }); log("screenshot download_in_progress");
  const file1 = await dl1;
  const zip1 = join(dl, file1.suggestedFilename());
  await file1.saveAs(zip1);
  await until(bodyHas(p, /20 of 20 frames verified/), "completion");
  await sleep(500);
  await p.screenshot({ path: `${shots}/download_complete.png` }); log(`screenshot download_complete (${file1.suggestedFilename()})`);

  // tamper one served file
  const target = findFile(join(root, nodeOut), `frame_${TAMPER}/rgb.png`);
  if (!target) throw new Error("cannot find the served rgb.png to tamper with");
  const backup = join(dl, "rgb.png.backup");
  copyFileSync(target, backup);
  const original = readFileSync(target);
  const changed = Buffer.from(original); changed[changed.length - 1] ^= 0x01; // flip one bit of the last byte
  writeFileSync(target, changed);
  log(`tampered ${target}`);
  const dl2 = p.waitForEvent("download");
  await p.getByRole("button", { name: "Download again" }).click();
  await until(bodyHas(p, /19 of 20 frames verified/), "the tampered run", 120_000);
  await sleep(500);
  await p.locator(".dataset").scrollIntoViewIfNeeded();
  await p.screenshot({ path: `${shots}/download_tampered.png` }); log("screenshot download_tampered");
  const file2 = await dl2;
  await file2.saveAs(join(dl, "tampered_" + file2.suggestedFilename()));
  const rejects = await p.locator(".dlrejects li").allInnerTexts();
  log(`rejections shown: ${JSON.stringify(rejects)}`);

  // restore and confirm it verifies again
  copyFileSync(backup, target);
  if (!readFileSync(target).equals(original)) throw new Error("restore failed");
  log("restored");
  const dl3 = p.waitForEvent("download");
  await p.getByRole("button", { name: "Download again" }).click();
  await until(bodyHas(p, /20 of 20 frames verified/), "the restored run", 120_000);
  await (await dl3).saveAs(join(dl, "restored_" + file1.suggestedFilename()));
  log("restored run: 20 of 20");
  console.log(JSON.stringify({ address, rejects }));
} finally {
  node?.kill("SIGTERM");
  await browser.close();
}
