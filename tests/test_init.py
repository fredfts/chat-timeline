"""Smoke tests for `timeline init` / `timeline deinit`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from chat_timeline.init_cmd import (
    GITIGNORE_CLOSE,
    GITIGNORE_OPEN,
    run_deinit,
    run_init,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_init_scaffolds_directories_and_files(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("TIMELINE_HOME", raising=False)

    run_init([])

    home = repo / "timeline"
    assert home.is_dir()
    for sub in ("chats", "chats/staged", "chats/used", "sessions", "contents", "timeline"):
        assert (home / sub).is_dir(), sub

    assert (home / ".gitignore").is_file()
    assert (home / "LLM_INSTRUCTIONS.md").is_file()

    state = json.loads((home / ".precommit_state.json").read_text())
    assert state["enabled"] is False
    assert state["tracked_chats"] == {}

    project_gi = (repo / ".gitignore").read_text(encoding="utf-8")
    assert GITIGNORE_OPEN in project_gi
    assert GITIGNORE_CLOSE in project_gi

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    body = hook.read_text(encoding="utf-8")
    assert "timeline -p" in body


def test_init_is_idempotent(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("TIMELINE_HOME", raising=False)

    run_init([])
    # User-owned files should not be clobbered on re-run.
    (repo / "timeline" / "LLM_INSTRUCTIONS.md").write_text("custom content", encoding="utf-8")
    run_init([])

    assert (repo / "timeline" / "LLM_INSTRUCTIONS.md").read_text() == "custom content"


def test_deinit_removes_hook_and_gitignore_block(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("TIMELINE_HOME", raising=False)

    run_init([])
    run_deinit([])

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert not hook.exists()

    project_gi = repo / ".gitignore"
    if project_gi.exists():
        assert GITIGNORE_OPEN not in project_gi.read_text(encoding="utf-8")

    # Output dirs are preserved.
    assert (repo / "timeline").is_dir()
