"""Tests for ``_uninstall_hook`` — covers the v0.1.1 hotfixes around
marker + standalone hook removal.

``_uninstall_hook`` takes an explicit ``hook_path``, so these tests operate on
a throwaway file under tmp without touching any real repo. The import of
``chat_timeline.precommit`` is still deferred to fixture time as a convention —
it pulls in ``chat_timeline._state``, which snapshots its path globals on
import.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def legacy(tmp_path):
    from chat_timeline import precommit

    return SimpleNamespace(precommit=precommit, hook_path=tmp_path / "pre-commit")


def test_uninstall_removes_marker_plus_standalone(legacy):
    legacy.hook_path.write_text(
        "#!/bin/sh\n"
        "# chat-timeline pre-commit hook\n"
        'TOPLEVEL="$(git rev-parse --show-toplevel)"\n'
        'cd "$TOPLEVEL" || exit 0\n'
        "timeline -p\n"
        "\n"
        "# --- timeline pre-commit ---\n"
        "timeline -p\n"
        "# --- end timeline pre-commit ---\n",
        encoding="utf-8",
    )

    legacy.precommit._uninstall_hook(legacy.hook_path)

    assert not legacy.hook_path.exists(), "hook should be fully removed"


def test_uninstall_detects_legacy_split_script_form(legacy):
    legacy.hook_path.write_text(
        "#!/bin/sh\n"
        "# timeline pre-commit hook\n"
        'TOPLEVEL="$(git rev-parse --show-toplevel)"\n'
        'SCRIPT="$TOPLEVEL/timeline/main.py"\n'
        'python3 "$SCRIPT" -x\n',
        encoding="utf-8",
    )

    legacy.precommit._uninstall_hook(legacy.hook_path)

    assert not legacy.hook_path.exists(), "legacy split-script hook should be removed"


def test_uninstall_preserves_unrelated_hook(legacy):
    legacy.hook_path.write_text(
        "#!/bin/sh\necho 'someone else hook'\n",
        encoding="utf-8",
    )

    legacy.precommit._uninstall_hook(legacy.hook_path)

    assert legacy.hook_path.exists()
    assert "someone else hook" in legacy.hook_path.read_text(encoding="utf-8")


def test_uninstall_marker_only_preserves_user_content(legacy):
    legacy.hook_path.write_text(
        "#!/bin/sh\n"
        "echo 'user hook'\n"
        "\n"
        "# --- timeline pre-commit ---\n"
        "timeline -p\n"
        "# --- end timeline pre-commit ---\n",
        encoding="utf-8",
    )

    legacy.precommit._uninstall_hook(legacy.hook_path)

    assert legacy.hook_path.exists()
    body = legacy.hook_path.read_text(encoding="utf-8")
    assert "user hook" in body
    assert "timeline -p" not in body


def test_uninstall_noop_when_hook_missing(legacy):
    legacy.precommit._uninstall_hook(legacy.hook_path)

    assert not legacy.hook_path.exists()
