import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";

// SHORTCUT (localnet only): lets the dev wallet read keys/requester.json. Registered only when VITE_CLUSTER is
// localnet and only on the dev server, so it is never part of a build. docs/LIMITATIONS.md.
function devKey(): Plugin {
  return {
    name: "porf-dev-key",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/__dev/requester-key", (_req, res) => {
        try {
          const body = readFileSync(resolve(__dirname, "../keys/requester.json"), "utf8");
          res.setHeader("Content-Type", "application/json");
          res.end(body);
        } catch {
          res.statusCode = 404;
          res.end(JSON.stringify({ error: "keys/requester.json not found: run scripts/setup.sh" }));
        }
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, "VITE_");
  const cluster = env.VITE_CLUSTER ?? "localnet";
  return {
    plugins: [react(), ...(cluster === "localnet" ? [devKey()] : [])],
    define: { global: "globalThis" },
    server: { port: 5173, host: "127.0.0.1" },
    test: { environment: "node", include: ["src/**/*.test.ts"] },
  };
});
