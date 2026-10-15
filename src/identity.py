"""Portable identity for checkpoints and remote mode (ADR-0009 §3).

A local realpath cannot key work across machines (``/Users/…`` on the Mac,
``/home/…`` on Linux, ``/mnt/c/…`` under WSL). The portable key is the git
``origin`` remote, normalized so every spelling of the same repository
collapses to one string (``github.com/owner/repo``). Hosts and profiles
complete the identity: ``host`` is the machine a session runs on, ``profile``
a namespace (``default``, ``work``, the voice agent's own) that recall and
resume are scoped to.

Everything here is cheap and side-effect free: one ``git config`` subprocess
at most, no database access.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from urllib.parse import urlsplit

PROFILE_ENV = "ENGRAM_PROFILE"
HOST_ENV = "ENGRAM_HOST"
PROJECT_KEY_ENV = "ENGRAM_PROJECT_KEY"
DEFAULT_PROFILE = "default"

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SCP_LIKE_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[A-Za-z0-9._-]+):(?P<path>[^/].*)$")


def _clean_name(value: str, fallback: str) -> str:
    cleaned = _NAME_RE.sub("-", value.strip()).strip("-").lower()
    return cleaned[:64] or fallback


def profile_name() -> str:
    """The active profile: ``ENGRAM_PROFILE`` or ``default``."""
    return _clean_name(os.environ.get(PROFILE_ENV, ""), DEFAULT_PROFILE)


def host_name() -> str:
    """This machine's short hostname (``ENGRAM_HOST`` overrides)."""
    override = os.environ.get(HOST_ENV, "").strip()
    if override:
        return _clean_name(override, "unknown")
    try:
        raw = socket.gethostname().split(".")[0]
    except OSError:
        raw = ""
    return _clean_name(raw, "unknown")


def normalize_remote_url(url: str | None) -> str | None:
    """Collapse every spelling of a git remote to ``host/owner/repo``.

    Handles ``https://user:pw@github.com/owner/repo.git``,
    ``ssh://git@github.com:22/owner/repo``, ``git@github.com:owner/repo.git``.
    Local paths and ``file://`` remotes return None — they are not portable.
    """
    if not url:
        return None
    url = url.strip()
    host = path = ""
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme == "file" or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    else:
        m = _SCP_LIKE_RE.match(url)
        if not m:
            return None
        host, path = m.group("host"), m.group("path")
    host = host.lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not host or not path or "/" not in path and not path:
        return None
    return f"{host}/{path}"


def git_remote_url(path: str) -> str:
    """``origin``'s URL for the repo containing *path*, or '' (never raises)."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def project_key_for(path: str | None = None) -> str:
    """Portable project key for *path* (default: cwd).

    Precedence: ``ENGRAM_PROJECT_KEY`` → normalized ``origin`` remote →
    ``host:realpath`` (not portable, but unique and stable on this machine).
    """
    override = os.environ.get(PROJECT_KEY_ENV, "").strip()
    if override:
        return override
    real = os.path.realpath(os.path.expanduser(path or os.getcwd()))
    key = normalize_remote_url(git_remote_url(real))
    return key or f"{host_name()}:{real}"
