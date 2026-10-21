"""A loopback stand-in for Nexus Core's HTTP service: the same route, bearer check and
(status, JSON) reply, with the real in-process verifier behind it. Core's own aiohttp
server is not a Cairnwork dependency; tests that need the real one run it separately."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from product.adapters.generic_verification import verify_generic_changeset

TOKEN = "test-core-token"


class _Handler(BaseHTTPRequestHandler):
    tamper = None  # optional hook: response dict -> response dict, set per server

    def do_POST(self):  # noqa: N802 - http.server API
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._reply(401, {"error": {"code": "UNAUTHORIZED"}})
        if self.path != "/v1/changesets/verify":
            return self._reply(404, {"error": {"code": "NOT_FOUND"}})
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, response = verify_generic_changeset(json.loads(body))
        if self.tamper is not None:
            response = self.tamper(response)
        return self._reply(status, response)

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@contextmanager
def core_http_server(tmp_path: Path, tamper=None):
    handler = type("Handler", (_Handler,), {"tamper": staticmethod(tamper) if tamper else None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "core.token"
    token_file.write_text(TOKEN + "\n")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", token_file
    finally:
        server.shutdown()
        server.server_close()
