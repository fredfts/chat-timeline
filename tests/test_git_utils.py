"""Tests for chat_timeline.git_utils — extracted from _legacy/main.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chat_timeline.git_utils import (
    get_current_branch,
    get_head_date,
    get_head_hash,
    get_head_message,
    get_head_short,
    get_staged_diff,
    get_staged_files,
    get_unstaged_diff,
    get_unstaged_files,
    get_untracked_files,
    git_mv,
    git_run,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "a.txt").write_text("initial\n", encoding="utf-8")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "initial commit")
    return r


def test_git_run_returns_stdout_and_rc(repo: Path):
    out, rc = git_run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    assert rc == 0
    assert out == "main"


def test_git_run_nonzero_on_bad_command(repo: Path):
    _, rc = git_run("not-a-subcommand", cwd=repo)
    assert rc != 0


def test_get_head_helpers(repo: Path):
    assert get_current_branch(repo) == "main"
    assert len(get_head_hash(repo)) == 40
    assert len(get_head_short(repo)) >= 7
    assert get_head_message(repo) == "initial commit"
    assert "T" in get_head_date(repo)  # ISO-ish timestamp


def test_staged_unstaged_untracked_round_trip(repo: Path):
    (repo / "a.txt").write_text("modified\n", encoding="utf-8")  # unstaged change
    (repo / "b.txt").write_text("new\n", encoding="utf-8")  # untracked
    (repo / "c.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "c.txt")  # staged

    assert "c.txt" in get_staged_files(repo)
    assert "staged" in get_staged_diff(repo)
    assert "a.txt" in get_unstaged_files(repo)
    assert "modified" in get_unstaged_diff(repo)
    assert "b.txt" in get_untracked_files(repo).splitlines()


def test_git_mv_tracked(repo: Path, capsys):
    git_mv(repo / "a.txt", repo / "renamed.txt", cwd=repo)
    captured = capsys.readouterr().out
    assert "git mv" in captured
    assert (repo / "renamed.txt").exists()
    assert not (repo / "a.txt").exists()


def test_git_mv_untracked_falls_back_to_shutil(repo: Path, capsys):
    src = repo / "untracked.txt"
    src.write_text("not tracked\n", encoding="utf-8")
    git_mv(src, repo / "moved.txt", cwd=repo)
    captured = capsys.readouterr().out
    assert "not tracked by git" in captured
    assert (repo / "moved.txt").exists()
