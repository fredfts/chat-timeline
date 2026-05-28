"""Tests for chat_timeline.paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

from chat_timeline.paths import find_project_root, find_timeline_home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def test_find_project_root_uses_git_toplevel(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")

    sub = repo / "sub" / "nested"
    sub.mkdir(parents=True)

    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(sub)

    assert find_project_root().resolve() == repo.resolve()


def test_find_project_root_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom"
    target.mkdir()
    monkeypatch.setenv("TIMELINE_PROJECT_ROOT", str(target))

    assert find_project_root().resolve() == target.resolve()


def test_find_project_root_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_project_root().resolve() == tmp_path.resolve()


def test_find_timeline_home_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TIMELINE_HOME", raising=False)
    home = find_timeline_home(tmp_path)
    assert home == (tmp_path / "timeline").resolve()


def test_find_timeline_home_env_override(tmp_path, monkeypatch):
    override = tmp_path / "elsewhere"
    monkeypatch.setenv("TIMELINE_HOME", str(override))
    assert find_timeline_home(tmp_path) == override.resolve()
