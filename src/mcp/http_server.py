"""Optional streamable-HTTP MCP transport (SOTA R8).

Default Engram MCP remains stdio (``python -m src.mcp_server``). This module
exposes a tiny Bearer-token HTTP wrapper for remote agents without pulling a
heavy ASGI stack: ``POST /mcp`` with JSON-RPC body.

Enable with: ``engram-mcp-http`` or ``python -m src.mcp.http_server``.
Set ``ENGRAM_MCP_HTTP_TOKEN`` to require Authorization: Bearer …
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.database import init_db
from src.mcp.protocol import handle_request


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # quieter default
        return

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"unauthorized")

    def _check_auth(self) -> bool:
        token = os.environ.get("ENGRAM_MCP_HTTP_TOKEN")
        if not token:
            return True
        auth = self.headers.get("Authorization") or ""
        return auth == f"Bearer {token}"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"transport":"http","server":"engram"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/mcp", "/"):
            self.send_response(404)
            self.end_headers()
            return
        if not self._check_auth():
            self._unauthorized()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid json")
            return
        resp = handle_request(msg) or {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    init_db()
    host = os.environ.get("ENGRAM_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("ENGRAM_MCP_HTTP_PORT", "8765"))
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"Engram MCP HTTP listening on {host}:{port} path=/mcp", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
