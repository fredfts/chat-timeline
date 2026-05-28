"""CLI smoke tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import chat_timeline
from chat_timeline.cli import _require_git_repo_or_explicit_root, main


def test_version(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["timeline", "--version"])
    main()
    assert chat_timeline.__version__ in capsys.readouterr().out


def test_guard_exits_when_not_in_git_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)

    with pytest.raises(SystemExit) as exc:
        _require_git_repo_or_explicit_root([])

    assert exc.value.code == 2
    assert "not inside a git repository" in capsys.readouterr().err


def test_guard_allows_no_git_flag_and_strips_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)

    out = _require_git_repo_or_explicit_root(["--no-git", "-t"])

    assert out == ["-t"]


def test_guard_allows_env_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIMELINE_PROJECT_ROOT", str(tmp_path))

    out = _require_git_repo_or_explicit_root(["-t"])

    assert out == ["-t"]


def test_guard_passes_inside_git_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)

    out = _require_git_repo_or_explicit_root(["-t"])

    assert out == ["-t"]


def test_init_subcommand_bypasses_guard(tmp_path, monkeypatch):
    """`timeline init` handles its own --no-git check; the top-level guard
    must not fire before init can run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("TIMELINE_HOME", raising=False)
    monkeypatch.setattr(sys, "argv", ["timeline", "init", "--no-git"])

    main()

    assert (Path(tmp_path) / "timeline").is_dir()
