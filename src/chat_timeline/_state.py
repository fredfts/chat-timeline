"""Module-level path globals resolved from the current project.

Replaces the per-import path-global block that used to live at the top of
``_legacy/main.py``. All paths are computed once at module load:

  * ``PROJECT_DIR`` = ``$TIMELINE_PROJECT_ROOT`` if set, else git toplevel
    of cwd, else cwd.
  * ``HISTORY_DIR`` = ``$TIMELINE_HOME`` if set, else ``<PROJECT_DIR>/timeline``.

Importing this module is mildly expensive (one ``git rev-parse``) — the
``functools.cache`` on ``chat_timeline.paths._git_toplevel`` keeps repeated
imports cheap.
"""

from __future__ import annotations

from chat_timeline.paths import find_project_root, find_timeline_home

PROJECT_DIR = find_project_root()
HISTORY_DIR = find_timeline_home(PROJECT_DIR)
SCRIPT_DIR = HISTORY_DIR  # legacy alias kept for any external script

try:
    HISTORY_DIR_NAME = HISTORY_DIR.resolve().relative_to(PROJECT_DIR.resolve()).as_posix()
except ValueError:
    HISTORY_DIR_NAME = HISTORY_DIR.name

TIMELINE_DIR = HISTORY_DIR / "timeline"
CHATS_DIR = HISTORY_DIR / "chats"
STAGED_DIR = CHATS_DIR / "staged"
USED_DIR = CHATS_DIR / "used"
SESSIONS_DIR = HISTORY_DIR / "sessions"
CONTENTS_DIR = HISTORY_DIR / "contents"
PRECOMMIT_STATE = HISTORY_DIR / ".precommit_state.json"
HOOK_PATH = PROJECT_DIR / ".git" / "hooks" / "pre-commit"
