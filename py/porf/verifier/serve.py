"""Read-only HTTP view of the verifier's output directory, for the UI (docs/PROTOCOL.md section 17.7).

    python -m porf.verifier.serve [--out out/verifier] [--port 8788]

GET /                          -> JSON list of the requests that have a report
GET /<request>/report.json     -> the report
GET /<request>/frames/<i>/<name>.png
Stateless: every request reads the files on disk, so it does not matter whether the verifier is running, was restarted, or
wrote the report minutes or days ago. Only report.json, report.txt and frames/<int>/<name>.png are ever served. CORS is open
because the reports hold nothing secret (the same data is public on chain and from the node's reveal endpoint).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

log = logging.getLogger("porf.verifier.serve")
DEFAULT_PORT = 8788
_ADDRESS = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
_ROUTE = re.compile(rf"^/({_ADDRESS})/(report\.json|report\.txt|frames/\d{{1,6}}/[a-z_]{{1,32}}\.png)$")
_TYPES = {".json": "application/json", ".txt": "text/plain; charset=utf-8", ".png": "image/png"}


def list_reports(out_dir: Path) -> list[dict]:
    rows = []
    for rep in sorted(Path(out_dir).glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(rep.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({"request": rep.parent.name, "verdict": data.get("verdict"), "updated_at": data.get("updated_at"),
                     "outcome": (data.get("outcome") or {}).get("action")})
    return rows


class ReportServer:
    def __init__(self, out_dir: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        self.out_dir = Path(out_dir).resolve()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, obj) -> None:
                self._send(status, json.dumps(obj).encode(), "application/json")

            def do_OPTIONS(self):
                self._send(204, b"", "text/plain")

            def do_GET(self):
                path = unquote(urlparse(self.path).path)
                if path in ("/", ""):
                    return self._json(200, {"reports": list_reports(outer.out_dir)})
                m = _ROUTE.match(path)
                if not m:
                    return self._json(404, {"error": f"no such path: {path}"})
                target = (outer.out_dir / path.lstrip("/")).resolve()
                if outer.out_dir not in target.parents or not target.is_file():
                    return self._json(404, {"error": "not found (the verifier has not written this yet)"})
                try:
                    return self._send(200, target.read_bytes(), _TYPES[target.suffix])
                except OSError as e:
                    return self._json(500, {"error": str(e)})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "ReportServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="report-server", daemon=True)
        self._thread.start()
        log.info("report server listening on %s (serving %s)", self.url, self.out_dir)
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve verifier reports and frame images (read-only)")
    ap.add_argument("--out", default="out/verifier")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = ReportServer(Path(args.out), port=args.port).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
