"""``engram serve`` — the remote-mode server (ADR-0009, Phase 1).

A small JSON-over-HTTP API that hosts the *existing* modules unchanged against
this host's SQLite. The seam is above the modules, not under them: each
endpoint is a thin mapping over a function the local CLI/MCP already calls.

Security shape (ADR-0009 §6): bind ``127.0.0.1`` only and publish on the
tailnet with ``tailscale serve``, which terminates TLS, strips spoofed
identity headers, and injects ``Tailscale-User-Login`` / ``Tailscale-User-Name``
(users only) and ``Tailscale-App-Capabilities`` (users *and* tagged nodes).
Authorization keys on the capability grant, identity headers are attribution.
Binding to a non-loopback address is refused unless explicitly allowed.

Every call writes one ledger row (``source: remote_call``) so ``engram roi``
can report per-endpoint latency and the unreachable/denied rates —
measured, not vibes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7437
HOST_ENV = "ENGRAM_SERVE_HOST"
PORT_ENV = "ENGRAM_SERVE_PORT"
AUTH_ENV = "ENGRAM_SERVE_AUTH"  # caps (default) | none
ALLOW_NONLOCAL_ENV = "ENGRAM_SERVE_ALLOW_NONLOCAL"
# Tailscale app-capability name whose grant values authorize callers. Grant
# values are app-defined JSON; ours is {"actions": ["read" | "write" | "add" | "*"]}.
CAP_NAME_ENV = "ENGRAM_SERVE_CAP_NAME"
DEFAULT_CAP_NAME = "engram.dev/cap/engram"

CAPS_HEADER = "Tailscale-App-Capabilities"
LOGIN_HEADER = "Tailscale-User-Login"

MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_SEARCH_LIMIT = 50
API_PREFIX = "/v1/"

# endpoint → action the caller's grant must carry
_REQUIRED_ACTION: dict[str, str] = {
    "health": "read",
    "search": "read",
    "recall": "read",
    "guard": "read",
    "route": "read",
    "inbox": "read",
    "add": "add",  # "write" satisfies it too (see _authorized)
    "extract": "write",
    "checkpoint": "write",
    "resume": "read",
}

_CITATION_RE = re.compile(r"\[(\w+) #(\d+)\]")


class ServeError(Exception):
    """An HTTP-mapped failure: ``status`` + a JSON-safe ``message``."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return socket.inet_pton(socket.AF_INET, host)[0] == 127
    except OSError:
        return False


def parse_capabilities(header_value: str | None, cap_name: str) -> set[str]:
    """Return the set of actions the ``Tailscale-App-Capabilities`` header grants.

    The header is JSON keyed by capability name; each key holds a list of
    app-defined grant values. We accept ``{"actions": [...]}`` objects, bare
    strings, and ``"*"``. Anything unparseable grants nothing.
    """
    if not header_value:
        return set()
    try:
        data = json.loads(header_value)
    except ValueError:
        return set()
    grants = data.get(cap_name) if isinstance(data, dict) else None
    actions: set[str] = set()
    for g in grants or []:
        if isinstance(g, str):
            actions.add(g)
        elif isinstance(g, dict):
            acts = g.get("actions")
            if isinstance(acts, str):
                actions.add(acts)
            elif isinstance(acts, list):
                actions.update(str(a) for a in acts)
    return actions


def _authorized(actions: set[str], required: str) -> bool:
    if "*" in actions or required in actions:
        return True
    # write implies read and add; add is add-only.
    if required in ("read", "add") and "write" in actions:
        return True
    return False


# ---------------------------------------------------------------------------
# Endpoint implementations (thin mappings; no business logic here)
# ---------------------------------------------------------------------------


def _require(body: Mapping[str, Any], key: str) -> str:
    val = body.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ServeError(400, f"'{key}' is required")
    return val.strip()


def _int(body: Mapping[str, Any], key: str, default: int, *, lo: int = 1, hi: int = MAX_SEARCH_LIMIT) -> int:
    raw = body.get(key, default)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ServeError(400, f"'{key}' must be an integer") from None
    return max(lo, min(hi, n))


def ep_health(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.database import SCHEMA_VERSION, get_schema_meta, get_vec_dimension
    from src.mcp.constants import get_server_version

    return {
        "ok": True,
        "server_version": get_server_version(),
        "schema_version": SCHEMA_VERSION,
        "vec_dimension": get_vec_dimension(),
        "instance_id": get_schema_meta("instance_id"),
    }


def ep_search(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.search import search

    q = _require(body, "query")
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, list):
        raise ServeError(400, "'tags' must be a list")
    results = search(
        q,
        item_type=body.get("item_type") or None,
        tags=tags or None,
        limit=_int(body, "limit", 10),
        project_path=None,
        audit_source="remote",
    )
    return {"results": list(results)}


def ep_recall(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.hooks import build_recall_context

    prompt = _require(body, "prompt")
    context = build_recall_context(
        prompt,
        project_path=None,  # project affinity by project_key lands with the v29 identity work
        session_id=body.get("session_id") or None,
    )
    return {"context": context}


def ep_guard(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.hooks import _probe_guard_warnings
    from src.search_audit import append_injection_audit

    action_text = _require(body, "action_text")
    warnings = _probe_guard_warnings(action_text, limit=_int(body, "limit", 3, hi=10))
    # Same ledger row the local guard hook writes (COST side; echo detection joins on items).
    refs = [
        {"item_type": m.group(1).lower(), "item_id": int(m.group(2))}
        for w in warnings
        for m in [_CITATION_RE.match(w)]
        if m
    ]
    append_injection_audit(
        "guard",
        tokens_est=sum(len(w) for w in warnings) // 4,
        kept=len(warnings),
        items=refs,
        session_id=body.get("session_id") or None,
    )
    return {"warnings": warnings}


def _handler_result(text: str) -> dict:
    if text.startswith("Error:"):
        raise ServeError(400, text)
    return {"result": text}


def ep_add(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.mcp.handlers import handle_memory_add

    _require(body, "type")
    # Code-set origin stamp (ADR-0009 §3): the server decides, never the caller.
    stamped = dict(body)
    stamped["provenance"] = f"remote:{str(body.get('host') or '').strip() or 'unknown'}"
    return _handler_result(handle_memory_add(stamped))


def ep_checkpoint(body: Mapping[str, Any], query: Mapping[str, str]) -> None:
    """Upsert a checkpoint the client assembled (ADR-0009 §4) and run echo
    detection over the assistant text it sent. Answers 204: a Stop hook that
    returns context would keep Claude working."""
    from src.checkpoint import record_echoes_from_text, upsert_checkpoint

    project_key = _require(body, "project_key")
    session_id = _require(body, "session_id")
    last_summary = str(body.get("last_summary") or "")
    upsert_checkpoint(
        str(body.get("cwd") or project_key),
        session_id,
        last_prompt=str(body.get("last_prompt") or ""),
        last_summary=last_summary,
        git_head=str(body.get("git_head") or ""),
        git_branch=str(body.get("git_branch") or ""),
        project_key=project_key,
        host=str(body.get("host") or "").strip() or "unknown",
        profile=str(body.get("profile") or "").strip() or "default",
    )
    if last_summary:
        record_echoes_from_text(session_id, last_summary)
    return None


def ep_resume(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.checkpoint import build_resume_report, get_checkpoints

    project_key = (query.get("project_key") or "").strip()
    if not project_key:
        raise ServeError(400, "'project_key' is required")
    profile = (query.get("profile") or "").strip() or "default"
    host = (query.get("host") or "").strip() or None
    all_profiles = query.get("all_profiles", "") in ("1", "true", "yes")
    limit = _int(query, "limit", 1, hi=10)
    return {
        "checkpoints": get_checkpoints(
            project_key, limit=limit, project_key=project_key, profile=profile,
            host=host, all_profiles=all_profiles,
        ),
        "report": build_resume_report(
            project_key, limit=limit, project_key=project_key, profile=profile,
            host=host, all_profiles=all_profiles,
        ),
    }


def ep_route(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.mcp.handlers import handle_memory_route

    _require(body, "task")
    return _handler_result(handle_memory_route(body))


def ep_extract(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.mcp.handlers import handle_memory_auto_extract

    return _handler_result(handle_memory_auto_extract(body))


def ep_inbox(body: Mapping[str, Any], query: Mapping[str, str]) -> dict:
    from src.inbox import list_items

    status = query.get("status", "open")
    if not re.fullmatch(r"[a-z_]{1,24}", status):
        raise ServeError(400, "invalid inbox status")
    return {"items": list_items(status=status)}


Endpoint = Callable[[Mapping[str, Any], Mapping[str, str]], "dict | None"]

ROUTES: dict[tuple[str, str], Endpoint] = {
    ("GET", "health"): ep_health,
    ("POST", "search"): ep_search,
    ("POST", "recall"): ep_recall,
    ("POST", "guard"): ep_guard,
    ("POST", "add"): ep_add,
    ("POST", "route"): ep_route,
    ("POST", "extract"): ep_extract,
    ("GET", "inbox"): ep_inbox,
    ("POST", "checkpoint"): ep_checkpoint,
    ("GET", "resume"): ep_resume,
}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


class EngramHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, auth: str, cap_name: str) -> None:
        self.auth = auth
        self.cap_name = cap_name
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: EngramHTTPServer  # narrowed from socketserver.BaseServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("http %s", format % args)

    # -- helpers -------------------------------------------------------------

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ServeError(413, "body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ServeError(400, "body must be a JSON object") from None
        if not isinstance(body, dict):
            raise ServeError(400, "body must be a JSON object")
        return body

    def _caller(self) -> str:
        return self.headers.get(LOGIN_HEADER) or f"node:{self.client_address[0]}"

    def _check_auth(self, endpoint: str) -> None:
        if self.server.auth == "none":
            return
        header = self.headers.get(CAPS_HEADER)
        if header is None:
            raise ServeError(401, f"missing {CAPS_HEADER} (is the server behind tailscale serve?)")
        actions = parse_capabilities(header, self.server.cap_name)
        if not _authorized(actions, _REQUIRED_ACTION[endpoint]):
            raise ServeError(403, f"grant lacks '{_REQUIRED_ACTION[endpoint]}' for /v1/{endpoint}")

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        t0 = time.perf_counter()
        parts = urlsplit(self.path)
        endpoint = parts.path[len(API_PREFIX):].strip("/") if parts.path.startswith(API_PREFIX) else ""
        body: dict = {}
        payload: dict | None
        try:
            if not endpoint or not any(ep == endpoint for _, ep in ROUTES):
                raise ServeError(404, "unknown endpoint")
            fn = ROUTES.get((method, endpoint))
            if fn is None:
                raise ServeError(405, f"{method} not allowed on /v1/{endpoint}")
            self._check_auth(endpoint)
            body = self._read_body() if method == "POST" else {}
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            payload = fn(body, query)
            status, outcome = (204 if payload is None else 200), "ok"
        except ServeError as e:
            status = e.status
            outcome = "denied" if status in (401, 403) else "client_error" if status < 500 else "error"
            payload = {"error": e.message}
        except Exception as e:  # never leak a traceback to a hook; log it here
            logger.exception("remote call failed: %s /v1/%s", method, endpoint)
            status, outcome = 500, "error"
            payload = {"error": f"internal error: {type(e).__name__}"}
        # Ledger BEFORE the response: a caller that reads the ledger right after
        # the reply (tests, `engram roi` on the same host) must see this call.
        self._ledger(endpoint or parts.path, method, status, outcome, t0, body)
        if payload is None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_json(status, payload)

    def _ledger(self, endpoint: str, method: str, status: int, outcome: str, t0: float, body: Mapping[str, Any]) -> None:
        try:
            from src.search_audit import append_remote_call_audit

            append_remote_call_audit(
                endpoint=endpoint,
                method=method,
                status=status,
                outcome=outcome,
                server_ms=(time.perf_counter() - t0) * 1000.0,
                caller=self._caller(),
                host=str(body.get("host") or ""),
                profile=str(body.get("profile") or ""),
                session_id=str(body.get("session_id") or ""),
            )
        except Exception:
            logger.debug("remote-call ledger write failed", exc_info=True)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("POST")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    auth: str = "caps",
    cap_name: str | None = None,
    allow_nonlocal: bool = False,
) -> EngramHTTPServer:
    """Build (but do not run) the server. Refuses non-loopback binds by default."""
    if auth not in ("caps", "none"):
        raise ValueError("auth must be 'caps' or 'none'")
    if not _is_loopback(host) and not allow_nonlocal:
        raise ValueError(
            f"refusing to bind {host}: ADR-0009 §6 keeps engram serve on 127.0.0.1 behind "
            f"`tailscale serve`; set {ALLOW_NONLOCAL_ENV}=1 / --allow-nonlocal to override"
        )
    return EngramHTTPServer((host, port), auth=auth, cap_name=cap_name or os.environ.get(CAP_NAME_ENV) or DEFAULT_CAP_NAME)


def serve(
    host: str | None = None,
    port: int | None = None,
    *,
    auth: str | None = None,
    allow_nonlocal: bool | None = None,
) -> None:
    """Run the server until interrupted (SIGINT/SIGTERM via KeyboardInterrupt/SystemExit)."""
    from src.database import init_db

    host = host or os.environ.get(HOST_ENV) or DEFAULT_HOST
    port = int(port or os.environ.get(PORT_ENV) or DEFAULT_PORT)
    auth = auth or os.environ.get(AUTH_ENV) or "caps"
    if allow_nonlocal is None:
        allow_nonlocal = os.environ.get(ALLOW_NONLOCAL_ENV, "").strip() in ("1", "true", "yes")
    init_db()
    httpd = make_server(host, port, auth=auth, allow_nonlocal=allow_nonlocal)
    logger.info("engram serve listening on http://%s:%s (auth=%s)", host, httpd.server_address[1], auth)
    if auth == "none":
        logger.warning("auth=none: every caller is trusted; only for local tests")
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        httpd.server_close()
        logger.info("engram serve stopped")


def serve_in_thread(httpd: EngramHTTPServer) -> threading.Thread:
    """Test helper: run ``httpd`` on a daemon thread and return it."""
    t = threading.Thread(target=httpd.serve_forever, name="engram-serve", daemon=True)
    t.start()
    return t
