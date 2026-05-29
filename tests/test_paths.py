"""Tests for chat_timeline.paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

from chat_timeline.paths import _git_toplevel, find_project_root, find_timeline_home


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


def test_git_toplevel_is_cached_per_start(tmp_path, monkeypatch):
    """The expensive `git rev-parse` should run once per unique start path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True)

    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(tuple(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    for _ in range(3):
        assert _git_toplevel(repo.resolve()) == repo.resolve()

    git_calls = [c for c in calls if c[:2] == ("git", "rev-parse")]
    assert len(git_calls) == 1, f"expected one git rev-parse, saw {len(git_calls)}: {git_calls}"


def test_find_project_root_skips_redundant_git_call(tmp_path, monkeypatch):
    """The cli guard + legacy import double-call pattern should hit the cache."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True)

    monkeypatch.delenv("TIMELINE_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(repo)

    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(tuple(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    a = find_project_root()
    b = find_project_root()

    assert a == b == repo.resolve()
    git_calls = [c for c in calls if c[:2] == ("git", "rev-parse")]
    assert len(git_calls) == 1, f"expected cached second call, saw {len(git_calls)} git invocations"
