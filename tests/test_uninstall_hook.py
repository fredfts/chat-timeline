"""Tests for ``_uninstall_hook`` — covers the v0.1.1 hotfixes around
marker + standalone hook removal.

The import of ``chat_timeline.precommit`` is deferred to fixture time:
that module captures ``HOOK_PATH`` from env/cwd at import time, so a
module-level import here would freeze it before sibling tests have set
up their tmp dirs.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    from chat_timeline import precommit

    # _uninstall_hook reads HOOK_PATH at call time from this module's
    # binding; patching at the module level redirects it to the tmp file.
    monkeypatch.setattr(precommit, "HOOK_PATH", tmp_path / "pre-commit")
    return precommit


def test_uninstall_removes_marker_plus_standalone(legacy):
    legacy.HOOK_PATH.write_text(
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

    legacy._uninstall_hook()

    assert not legacy.HOOK_PATH.exists(), "hook should be fully removed"


def test_uninstall_detects_legacy_split_script_form(legacy):
    legacy.HOOK_PATH.write_text(
        "#!/bin/sh\n"
        "# timeline pre-commit hook\n"
        'TOPLEVEL="$(git rev-parse --show-toplevel)"\n'
        'SCRIPT="$TOPLEVEL/timeline/main.py"\n'
        'python3 "$SCRIPT" -x\n',
        encoding="utf-8",
    )

    legacy._uninstall_hook()

    assert not legacy.HOOK_PATH.exists(), "legacy split-script hook should be removed"


def test_uninstall_preserves_unrelated_hook(legacy):
    legacy.HOOK_PATH.write_text(
        "#!/bin/sh\necho 'someone else hook'\n",
        encoding="utf-8",
    )

    legacy._uninstall_hook()

    assert legacy.HOOK_PATH.exists()
    assert "someone else hook" in legacy.HOOK_PATH.read_text(encoding="utf-8")


def test_uninstall_marker_only_preserves_user_content(legacy):
    legacy.HOOK_PATH.write_text(
        "#!/bin/sh\n"
        "echo 'user hook'\n"
        "\n"
        "# --- timeline pre-commit ---\n"
        "timeline -p\n"
        "# --- end timeline pre-commit ---\n",
        encoding="utf-8",
    )

    legacy._uninstall_hook()

    assert legacy.HOOK_PATH.exists()
    body = legacy.HOOK_PATH.read_text(encoding="utf-8")
    assert "user hook" in body
    assert "timeline -p" not in body


def test_uninstall_noop_when_hook_missing(legacy):
    legacy._uninstall_hook()

    assert not legacy.HOOK_PATH.exists()
