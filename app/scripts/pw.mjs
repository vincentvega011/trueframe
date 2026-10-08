// Shared Playwright launch: uses the Chromium that is already cached when the exact build Playwright wants is missing.
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";

export async function launch() {
  try {
    return await chromium.launch();
  } catch {
    const base = join(homedir(), "Library/Caches/ms-playwright");
    const dirs = existsSync(base) ? readdirSync(base).filter((d) => d.startsWith("chromium-")).sort().reverse() : [];
    for (const d of dirs) {
      const exe = join(base, d, "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");
      if (existsSync(exe)) return chromium.launch({ executablePath: exe });
    }
    throw new Error("No Chromium available: run `npx playwright install chromium`");
  }
}

export async function page(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => console.log("PAGE ERROR:", e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/404/.test(m.text())) console.log("CONSOLE ERROR:", m.text()); });
  return p;
}
