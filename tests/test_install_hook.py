"""Regression tests for hook-path resolution in ``_install_hook``.

The bug: ``_state.HOOK_PATH`` is frozen at import time, so installing through
that global wrote the pre-commit hook to whatever repo happened to be current
when ``chat_timeline._state`` was first imported (in the suite, often the real
project). ``_install_hook`` now resolves ``<project>/.git/hooks/pre-commit``
fresh per call via ``_resolve_hook_path``, honoring ``TIMELINE_PROJECT_ROOT``.

Imports are deferred to test time so this module's collection can't snapshot
``_state`` against the real repo.
"""

from __future__ import annotations


def test_resolve_hook_path_honors_env(tmp_path, monkeypatch):
    from chat_timeline import precommit

    repo = tmp_path / "proj"
    monkeypatch.setenv("TIMELINE_PROJECT_ROOT", str(repo))

    assert precommit._resolve_hook_path() == repo.resolve() / ".git" / "hooks" / "pre-commit"


def test_install_hook_targets_resolved_repo_not_frozen_global(tmp_path, monkeypatch):
    from chat_timeline import precommit

    repo = tmp_path / "proj"
    monkeypatch.setenv("TIMELINE_PROJECT_ROOT", str(repo))

    # No explicit path -> must resolve dynamically, not via _state.HOOK_PATH.
    precommit._install_hook()

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    assert "timeline -p" in hook.read_text(encoding="utf-8")


def test_install_then_uninstall_round_trip(tmp_path, monkeypatch):
    from chat_timeline import precommit

    repo = tmp_path / "proj"
    monkeypatch.setenv("TIMELINE_PROJECT_ROOT", str(repo))

    precommit._install_hook()
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()

    precommit._uninstall_hook()
    assert not hook.exists()


def test_explicit_hook_path_overrides_resolution(tmp_path, monkeypatch):
    from chat_timeline import precommit

    # Env points one place, explicit arg another: the explicit arg wins.
    monkeypatch.setenv("TIMELINE_PROJECT_ROOT", str(tmp_path / "ignored"))
    target = tmp_path / "hooks" / "pre-commit"
    target.parent.mkdir(parents=True)

    precommit._install_hook(target)

    assert target.is_file()
    assert not (tmp_path / "ignored").exists()
