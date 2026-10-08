// Drives the app in a real browser at 1920x1080 against a running localnet, node and verifier, and screenshots each view
// in its main states. The node is started here (as a child process) so the run is one command.
//
//   (localnet up, `npm run dev` running, verifier running with --serve)
//   node scripts/screenshots.mjs [--out ../out/ui/shots] [--cheats no_occlusion,fake_boxes] [--no-honest]
import { spawn } from "node:child_process";
import { mkdirSync, openSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, page as newPage } from "./pw.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const outDir = resolve(arg("--out", resolve(root, "out/ui/shots")));
const cheats = (arg("--cheats", "no_occlusion,fake_boxes") || "").split(",").filter(Boolean);
const honest = !process.argv.includes("--no-honest");
const APP = "http://127.0.0.1:5173";
mkdirSync(outDir, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (m) => console.log(`[${new Date().toLocaleTimeString("en-GB")}] ${m}`);

async function until(fn, what, timeout = 600_000, every = 250) {
  const t0 = Date.now();
  for (;;) {
    const v = await fn().catch(() => null);
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error(`timed out waiting for ${what}`);
    await sleep(every);
  }
}
const bodyHas = (p, re) => () => p.locator("body").innerText().then((t) => re.test(t));
const shot = async (p, name) => { await p.screenshot({ path: `${outDir}/${name}.png` }); log(`screenshot ${name}`); };

function startNode(tag, extra = [], address) {
  const logFile = openSync(`${root}/out/ui/node_${tag}.log`, "w");
  const args = ["-m", "porf.node.client", "--keypair", "keys/node.json", "--out", `out/ui/node_${tag}`, "--poll", "1", "--port", "0", "--only-request", address, ...extra];
  log(`node: python ${args.join(" ")}`);
  return spawn(`${root}/py/.venv/bin/python`, args, { cwd: root, stdio: ["ignore", logFile, logFile] });
}

async function createViaUi(p, { seed }) {
  await p.goto(`${APP}/#/create`);
  await until(bodyHas(p, /Dev wallet/), "the dev wallet");
  await p.getByRole("radio", { name: "3" }).click();
  await p.locator("#seed").fill(seed);
  await until(bodyHas(p, /Wallet balance\s*\d/), "the wallet balance");
  await p.getByRole("button", { name: "Create request" }).click();
  await until(bodyHas(p, /Request created/), "the created notice", 60_000);
  return (await p.locator(".notice .hash").first().innerText()).trim();
}

async function scenario(browser, name, address, nodeArgs) {
  const tl = await newPage(browser);
  const rs = await newPage(browser);
  await tl.goto(`${APP}/#/timeline/${address}`);
  await until(bodyHas(tl, /Waiting for a node to claim/), "open state");
  await shot(tl, `${name}_timeline_open`);
  await rs.goto(`${APP}/#/result/${address}`);
  await until(bodyHas(rs, /Checking/), "result view");
  await shot(rs, `${name}_result_waiting`);

  const node = startNode(name, nodeArgs, address);
  await until(bodyHas(tl, /node is rendering/), "claimed state", 120_000);
  await shot(tl, `${name}_timeline_rendering`);
  await until(bodyHas(tl, /checking sampled frames|Verdict (Pass|Fail)/), "challenge in progress", 900_000, 150);
  await shot(tl, `${name}_timeline_challenged`);
  await until(async () => /^(Paid|Slashed)$/.test((await tl.locator(".timeline li.step.last h2").innerText()).trim()), "settlement", 300_000);
  await sleep(1500);
  await shot(tl, `${name}_timeline_settled`);
  await until(async () => (await rs.locator(".strip .cell").count()) > 0 && /\b(Paid|Slashed)\b/.test(await rs.locator(".verdictword").innerText()), "the result", 120_000);
  await sleep(1500);
  await shot(rs, `${name}_result`);
  const bad = rs.locator(".strip .cell.bad").first();
  if (await bad.count()) { await bad.click(); await sleep(800); await rs.evaluate(() => window.scrollTo(0, document.querySelector(".strip").getBoundingClientRect().top + window.scrollY - 20)); await sleep(300); await shot(rs, `${name}_result_failing_frame`); }
  await rs.evaluate(() => window.scrollTo(0, 0));
  node.kill("SIGTERM");
  await tl.context().close(); await rs.context().close();
}

const browser = await launch();
try {
  // states that need no request
  const p0 = await newPage(browser);
  await p0.goto(`${APP}/#/create`);
  await until(bodyHas(p0, /Wallet balance\s*\d/), "the create form");
  await shot(p0, "create_empty");
  await p0.locator("input").first().fill("0");
  await sleep(300);
  await shot(p0, "create_invalid");
  await p0.getByRole("button", { name: "Disconnect" }).click();
  await sleep(500);
  await shot(p0, "create_wallet_disconnected");
  await p0.goto(`${APP}/#/timeline`); await sleep(800); await shot(p0, "timeline_nothing_selected");
  await p0.context().close();

  if (honest) {
    const p = await newPage(browser);
    const addr = await createViaUi(p, { seed: "5150" });
    log(`honest request ${addr}`);
    await shot(p, "honest_create_done");
    await p.context().close();
    await scenario(browser, "honest", addr, []);
  }
  for (const mode of cheats) {
    const p = await newPage(browser);
    const addr = await createViaUi(p, { seed: "5150" });
    log(`${mode} request ${addr}`);
    await p.context().close();
    await scenario(browser, mode, addr, ["--cheat", mode, "--fraction", "0.8"]);
  }
} finally {
  await browser.close();
}
log("done");
