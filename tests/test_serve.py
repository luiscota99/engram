"""``engram serve`` — ADR-0009 Phase 1 server: endpoints, auth, ledger, bind policy."""

from __future__ import annotations

import http.client
import json

import pytest

from src import serve as serve_mod
from src.database import SCHEMA_VERSION

CAP = serve_mod.DEFAULT_CAP_NAME


def _grant(*actions: str) -> dict:
    return {serve_mod.CAPS_HEADER: json.dumps({CAP: [{"actions": list(actions)}]})}


@pytest.fixture
def no_embed(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")


@pytest.fixture
def server(test_db, no_embed):
    httpd = serve_mod.make_server("127.0.0.1", 0, auth="none")
    serve_mod.serve_in_thread(httpd)
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def caps_server(test_db, no_embed):
    httpd = serve_mod.make_server("127.0.0.1", 0, auth="caps")
    serve_mod.serve_in_thread(httpd)
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _call(httpd, method: str, path: str, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {"__raw__": raw}


MISTAKE = {
    "type": "mistake",
    "date": "2026-10-15",
    "context": "verifying the engram test suite",
    "mistake": "ran pytest quiet mode and piped grep for passed so the check was blind",
    "fix": "capture the exit code directly and read the summary line",
    "tags": ["testing"],
}


# --- endpoints -----------------------------------------------------------------


def test_health_reports_schema_and_version(server):
    status, payload = _call(server, "GET", "/v1/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert isinstance(payload["vec_dimension"], int)


def test_add_then_search_round_trip(server):
    status, payload = _call(server, "POST", "/v1/add", MISTAKE)
    assert status == 200, payload
    assert "result" in payload

    status, payload = _call(server, "POST", "/v1/search", {"query": "pytest quiet grep blind", "limit": 5})
    assert status == 200, payload
    assert payload["results"], "the item just added must be searchable"
    assert payload["results"][0]["item_type"] == "mistake"


def test_guard_probe_warns_on_matching_action(server):
    _call(server, "POST", "/v1/add", MISTAKE)
    status, payload = _call(
        server, "POST", "/v1/guard", {"action_text": "ran pytest quiet mode and piped grep for passed"}
    )
    assert status == 200, payload
    assert any(w.startswith("[MISTAKE #") for w in payload["warnings"]), payload


def test_guard_probe_silent_on_unrelated_action(server):
    _call(server, "POST", "/v1/add", MISTAKE)
    status, payload = _call(server, "POST", "/v1/guard", {"action_text": "git status"})
    assert status == 200
    assert payload["warnings"] == []


def test_recall_returns_context_string(server):
    _call(server, "POST", "/v1/add", MISTAKE)
    status, payload = _call(
        server, "POST", "/v1/recall", {"prompt": "why was the pytest check blind?", "session_id": "s1"}
    )
    assert status == 200, payload
    assert isinstance(payload["context"], str)


def test_inbox_lists_open_items(server):
    status, payload = _call(server, "GET", "/v1/inbox")
    assert status == 200
    assert payload["items"] == []
    status, payload = _call(server, "GET", "/v1/inbox?status=DROP%20TABLE")
    assert status == 400


def test_route_requires_task(server):
    status, payload = _call(server, "POST", "/v1/route", {})
    assert status == 400
    assert "task" in payload["error"]


# --- protocol errors -----------------------------------------------------------


def test_unknown_route_404_and_wrong_method_405(server):
    assert _call(server, "GET", "/v1/nope")[0] == 404
    assert _call(server, "GET", "/not-api")[0] == 404
    assert _call(server, "GET", "/v1/search")[0] == 405
    assert _call(server, "POST", "/v1/health")[0] == 405


def test_bad_json_and_missing_fields_are_400(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("POST", "/v1/search", body=b"{not json", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400
    resp.read()
    conn.close()
    status, payload = _call(server, "POST", "/v1/search", {"limit": 3})
    assert status == 400 and "query" in payload["error"]
    status, payload = _call(server, "POST", "/v1/add", {"type": "mistake"})
    assert status == 400 and payload["error"].startswith("Error:")


# --- authorization (ADR-0009 §6) ----------------------------------------------


def test_caps_mode_requires_grant_header(caps_server):
    status, payload = _call(caps_server, "GET", "/v1/health")
    assert status == 401
    assert serve_mod.CAPS_HEADER in payload["error"]


def test_caps_mode_read_grant_cannot_write(caps_server):
    assert _call(caps_server, "GET", "/v1/health", headers=_grant("read"))[0] == 200
    assert _call(caps_server, "POST", "/v1/search", {"query": "x"}, headers=_grant("read"))[0] == 200
    status, payload = _call(caps_server, "POST", "/v1/add", MISTAKE, headers=_grant("read"))
    assert status == 403
    assert "add" in payload["error"]


def test_caps_mode_add_only_and_write_grants(caps_server):
    assert _call(caps_server, "POST", "/v1/add", MISTAKE, headers=_grant("add"))[0] == 200
    assert _call(caps_server, "GET", "/v1/health", headers=_grant("add"))[0] == 403
    assert _call(caps_server, "GET", "/v1/health", headers=_grant("write"))[0] == 200
    assert _call(caps_server, "GET", "/v1/health", headers=_grant("*"))[0] == 200


def test_parse_capabilities_shapes():
    hdr = json.dumps({CAP: [{"actions": ["read"]}, "write", {"actions": "add"}], "other/cap": ["*"]})
    assert serve_mod.parse_capabilities(hdr, CAP) == {"read", "write", "add"}
    assert serve_mod.parse_capabilities("not json", CAP) == set()
    assert serve_mod.parse_capabilities(None, CAP) == set()
    assert serve_mod.parse_capabilities(json.dumps({"other/cap": ["*"]}), CAP) == set()


# --- bind policy and ledger ------------------------------------------------------


def test_non_loopback_bind_is_refused_by_default():
    with pytest.raises(ValueError, match="ADR-0009"):
        serve_mod.make_server("0.0.0.0", 0, auth="none")
    with pytest.raises(ValueError, match="auth"):
        serve_mod.make_server("127.0.0.1", 0, auth="token")


def test_every_call_writes_a_remote_call_ledger_row(server, monkeypatch, tmp_path):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))
    _call(server, "GET", "/v1/health", headers={serve_mod.LOGIN_HEADER: "luis@example.com"})
    _call(server, "GET", "/v1/nope")
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    remote = [r for r in rows if r["source"] == "remote_call"]
    assert [r["outcome"] for r in remote] == ["ok", "client_error"]
    assert remote[0]["endpoint"] == "health" and remote[0]["caller"] == "luis@example.com"
    assert remote[0]["server_ms"] >= 0
