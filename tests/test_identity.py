"""Portable identity (ADR-0009 §3): remote-URL normalization, project keys, host, profile."""

from __future__ import annotations

import os
import subprocess

import pytest

from src import identity


@pytest.mark.parametrize(
    "url, expected",
    [
        ("git@github.com:luiscota99/engram.git", "github.com/luiscota99/engram"),
        ("https://github.com/luiscota99/engram.git", "github.com/luiscota99/engram"),
        ("https://user:pw@GitHub.com/luiscota99/engram", "github.com/luiscota99/engram"),
        ("ssh://git@github.com:22/luiscota99/engram.git", "github.com/luiscota99/engram"),
        ("git@gitlab.example.com:group/sub/repo.git", "gitlab.example.com/group/sub/repo"),
        ("file:///Users/me/repo", None),
        ("/Users/me/repo", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_remote_url(url, expected):
    assert identity.normalize_remote_url(url) == expected


def test_project_key_uses_git_remote(tmp_path, monkeypatch):
    monkeypatch.delenv(identity.PROJECT_KEY_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:acme/widgets.git"],
        check=True,
    )
    assert identity.project_key_for(str(repo)) == "github.com/acme/widgets"
    # a worktree-style subdirectory resolves to the same key
    sub = repo / "src"
    sub.mkdir()
    assert identity.project_key_for(str(sub)) == "github.com/acme/widgets"


def test_project_key_falls_back_to_host_and_realpath(tmp_path, monkeypatch):
    monkeypatch.delenv(identity.PROJECT_KEY_ENV, raising=False)
    monkeypatch.setenv(identity.HOST_ENV, "mac1")
    plain = tmp_path / "plain"
    plain.mkdir()
    key = identity.project_key_for(str(plain))
    assert key == f"mac1:{os.path.realpath(str(plain))}"


def test_env_overrides_and_name_cleaning(monkeypatch):
    monkeypatch.setenv(identity.PROJECT_KEY_ENV, "custom/key")
    assert identity.project_key_for("/nowhere") == "custom/key"
    monkeypatch.setenv(identity.PROFILE_ENV, "Work Laptop!")
    assert identity.profile_name() == "work-laptop"
    monkeypatch.delenv(identity.PROFILE_ENV)
    assert identity.profile_name() == "default"
    monkeypatch.setenv(identity.HOST_ENV, "My.Mac")
    assert identity.host_name() == "my.mac"
