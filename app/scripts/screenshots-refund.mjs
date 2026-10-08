// Drives the two refund flows in the browser against a running localnet, with short deadlines (docs/PROTOCOL.md section 18):
//   A. a request nobody claims  -> Refund after the claim deadline -> Expired, escrow back to the requester
//   B. a request the node claims but never commits -> Refund after the commit deadline -> Expired, escrow + stake back
// The claim in B is sent by a short Python call as the node (the node process itself is not needed).
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const out = resolve(root, "out/ui/shots");
mkdirSync(out, { recursive: true });
const APP = "http://127.0.0.1:5173";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (m) => console.log(`[${new Date().toLocaleTimeString("en-GB")}] ${m}`);
async function until(fn, what, timeout = 120_000) {
  const t0 = Date.now();
  for (;;) {
    if (await fn().catch(() => false)) return;
    if (Date.now() - t0 > timeout) throw new Error(`timed out waiting for ${what}`);
    await sleep(250);
  }
}
const text = (p) => p.locator("body").innerText();
const shot = async (p, name) => { await p.screenshot({ path: `${out}/${name}.png` }); log(`screenshot ${name}`); };

async function create(p, claim, commit) {
  await p.goto(`${APP}/#/create`);
  await until(async () => /Wallet balance\s*\d/.test(await text(p)), "the create form");
  await p.locator("summary", { hasText: "Deadlines" }).click();
  const inputs = p.locator(".timingfields input");
  await inputs.nth(0).fill(String(claim));
  await inputs.nth(1).fill(String(commit));
  await p.getByRole("button", { name: "Create request" }).click();
  await until(async () => /Request created/.test(await text(p)), "the created notice", 60_000);
  return (await p.locator(".notice .hash").first().innerText()).trim();
}

const browser = await launch();
try {
  // A: nobody claims
  const a = await newPage(browser);
  const addrA = await create(a, 8, 8);
  log(`unclaimed request ${addrA}`);
  await a.goto(`${APP}/#/timeline/${addrA}`);
  await until(async () => /Waiting for a node to claim/.test(await text(a)), "the waiting state");
  await shot(a, "refund_unclaimed_waiting");
  await a.getByRole("button", { name: "Refund" }).waitFor({ timeout: 60_000 });
  await shot(a, "refund_unclaimed_ready");
  await a.getByRole("button", { name: "Refund" }).click();
  await until(async () => /20\.00 USDC refunded to the requester/.test(await text(a)) && (await a.locator(".timeline li.step.last h2").innerText()).trim() === "Expired", "the refund", 60_000);
  await sleep(1000);
  await shot(a, "refund_unclaimed_expired");
  await a.context().close();

  // B: claimed, never committed
  const b = await newPage(browser);
  const addrB = await create(b, 60, 8);
  log(`uncommitted request ${addrB}`);
  execFileSync(`${root}/py/.venv/bin/python`, ["-c", `from porf.chain.client import ChainClient; ChainClient().stake_and_claim("keys/node.json", request="${addrB}")`], { cwd: root, env: { ...process.env } });
  await b.goto(`${APP}/#/timeline/${addrB}`);
  await until(async () => /The node is rendering/.test(await text(b)), "the claimed state");
  await shot(b, "refund_uncommitted_waiting");
  await b.getByRole("button", { name: "Refund" }).waitFor({ timeout: 60_000 });
  await shot(b, "refund_uncommitted_ready");
  await b.getByRole("button", { name: "Refund" }).click();
  await until(async () => /4\.00 stake slashed to the requester/.test(await text(b)) && (await b.locator(".timeline li.step.last h2").innerText()).trim() === "Expired", "the refund", 60_000);
  await sleep(1000);
  await shot(b, "refund_uncommitted_expired");
  await b.context().close();
} finally {
  await browser.close();
}
log("done");
