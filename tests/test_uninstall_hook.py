"""Tests for `_uninstall_hook` — covers the two v0.1.1 hotfixes:

1. A hook file with BOTH a marker-delimited section AND a separate
   standalone block must be removed entirely; pre-fix the marker path
   returned early and left the standalone block behind.
2. The standalone-hook detection must catch the legacy form where
   `SCRIPT="$TOPLEVEL/timeline/main.py"` and `python3 "$SCRIPT" -x`
   live on separate lines, so the literal string `timeline/main.py -x`
   never appears.

The import of ``chat_timeline._legacy.main`` is deferred to fixture time:
that module captures ``HOOK_PATH`` from env/cwd at import time, so a
module-level import here would freeze it before sibling tests have set
up their tmp dirs.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    from chat_timeline import precommit
    from chat_timeline._legacy import main as legacy_mod

    # The body of _uninstall_hook now lives in chat_timeline.precommit and
    # binds HOOK_PATH at module-import time. Patch both so the legacy
    # re-export and the underlying module agree.
    hook = tmp_path / "pre-commit"
    monkeypatch.setattr(legacy_mod, "HOOK_PATH", hook)
    monkeypatch.setattr(precommit, "HOOK_PATH", hook)
    return legacy_mod


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
