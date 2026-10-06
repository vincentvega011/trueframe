"""Reveal server: lets the verifier fetch committed frames and their Merkle proofs. Spec: PROTOCOL.md section 16.4.

    GET /frame/<i>                    frame i of the most recently committed job
    GET /frame/<i>?request=<address>  frame i of that request's job
    GET /health                       {"ok": true, "committed": [<request addresses>]}

Unknown path, non-numeric or out-of-range index, unknown request, or no committed job -> 404 with {"error": ...}.
Plain HTTP, no authentication (LIMITATIONS.md).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .job import FrameNotFound, Job, reveal_payload

log = logging.getLogger("porf.node.server")
_FRAME_PATH = re.compile(r"^/frame/(\d+)$")


def committed_jobs(out_dir: Path) -> list[Job]:
    jobs = [j for j in Job.scan(out_dir) if j.state == "committed"]
    return sorted(jobs, key=lambda j: j.data.get("committed_at", 0))


class RevealServer:
    def __init__(self, out_dir: Path, host: str = "127.0.0.1", port: int = 8787):
        self.out_dir = Path(out_dir)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # route to logging instead of stderr
                log.debug("%s %s", self.address_string(), fmt % args)

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                try:
                    if url.path == "/health":
                        return self._send(200, {"ok": True, "committed": [j.request for j in committed_jobs(outer.out_dir)]})
                    m = _FRAME_PATH.match(url.path)
                    if not m:
                        return self._send(404, {"error": f"no such path: {url.path}"})
                    jobs = committed_jobs(outer.out_dir)
                    wanted = parse_qs(url.query).get("request", [None])[0]
                    if wanted:
                        jobs = [j for j in jobs if j.request == wanted]
                    if not jobs:
                        return self._send(404, {"error": "no committed job" + (f" for request {wanted}" if wanted else "")})
                    return self._send(200, reveal_payload(jobs[-1], int(m.group(1))))
                except FrameNotFound as e:
                    return self._send(404, {"error": str(e)})
                except Exception as e:  # never kill the server thread over one bad request
                    log.exception("reveal request failed")
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self.httpd.server_address[0], self.httpd.server_address[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> "RevealServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="reveal-server", daemon=True)
        self._thread.start()
        log.info("reveal server listening on %s", self.url)
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
