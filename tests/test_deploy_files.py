"""The shipped systemd unit and env template stay consistent with ADR-0009 §6."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "engram-serve.service"
ENV = ROOT / "deploy" / "engram.env.example"


def test_unit_runs_engram_serve_as_the_engram_user_loopback_only():
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # systemd keys are case-sensitive
    cp.read_string(UNIT.read_text())
    svc = cp["Service"]
    assert svc["ExecStart"].endswith("engram serve")
    assert svc["User"] == "engram" and svc["Group"] == "engram"
    assert svc["EnvironmentFile"] == "/etc/engram/engram.env"
    assert svc["ProtectSystem"] == "strict" and svc["NoNewPrivileges"] == "true"
    assert "/var/lib/engram" in svc["ReadWritePaths"]
    assert "0.0.0.0" not in UNIT.read_text() and "--allow-nonlocal" not in UNIT.read_text()
    assert "mcp" not in svc["ExecStart"]  # the stdio server must never be the service
    assert cp["Install"]["WantedBy"] == "multi-user.target"


def test_env_template_has_placeholders_not_secrets_and_the_right_defaults():
    values: dict[str, str] = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Z_]+=.*", line), line
        k, v = line.split("=", 1)
        values[k] = v
    assert values["ENGRAM_SERVE_HOST"] == "127.0.0.1"
    assert values["ENGRAM_SERVE_AUTH"] == "caps"
    assert values["ENGRAM_LLM_PROVIDER"] == "anthropic"
    assert values["ENGRAM_DB_PATH"].startswith("/var/lib/engram/")
    assert values["ENGRAM_LLM_API_KEY"] == "sk-ant-REPLACE-ME"
    text = ENV.read_text()
    assert not re.search(r"sk-ant-(?!REPLACE)[A-Za-z0-9_-]{20,}", text), "a real-looking key in the template"
    assert "ENGRAM_SERVE_ALLOW_NONLOCAL" not in text


def test_install_key_script_parses_and_dry_run_never_prints_the_key(tmp_path):
    import os
    import subprocess

    script = ROOT / "deploy" / "install-key.sh"
    assert os.access(script, os.X_OK)
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0

    key_file = tmp_path / "k.txt"
    key = "sk-ant-TESTVALUE-0123456789abcdef"
    key_file.write_text(f"  {key}  \n")
    res = subprocess.run(
        ["bash", str(script), "--dry-run", "--key-file", str(key_file), "--host", "nowhere"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert key not in res.stdout + res.stderr
    assert "nowhere:/etc/engram/engram.env" in res.stdout and f"{len(key)} chars" in res.stdout
    assert key_file.exists()  # dry run deletes nothing

    key_file.write_text("PASTE-YOUR-ANTHROPIC-KEY-HERE-REPLACING-THIS-WHOLE-LINE\n")
    res = subprocess.run(["bash", str(script), "--dry-run", "--key-file", str(key_file)], capture_output=True, text=True)
    assert res.returncode == 2 and "placeholder" in res.stderr

    key_file.write_text("not-an-anthropic-key\n")
    res = subprocess.run(["bash", str(script), "--dry-run", "--key-file", str(key_file)], capture_output=True, text=True)
    assert res.returncode == 2 and "sk-ant" in res.stderr and "not-an-anthropic-key" not in res.stderr

    res = subprocess.run(["bash", str(script), "--dry-run", "--var", "bad-name", "--key-file", str(key_file)], capture_output=True, text=True)
    assert res.returncode == 2
