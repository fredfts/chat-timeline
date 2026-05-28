"""Project-root and timeline-home resolution.

The legacy code derived these from ``Path(__file__).parent``, which only works
when the source files live inside the target project. Now that the tool is
installed system-wide, we resolve them from the current working directory:

  * ``PROJECT_DIR`` = ``$TIMELINE_PROJECT_ROOT`` if set, else
    ``git rev-parse --show-toplevel`` of cwd, else cwd.
  * ``HISTORY_DIR`` = ``$TIMELINE_HOME`` if set, else ``<PROJECT_DIR>/timeline``.

``_git_toplevel`` and the project-root resolver are memoized: cwd is fixed for
a process, so the second caller (the legacy import that re-derives project
state at module load) hits the cache instead of re-running ``git rev-parse``.
"""

from __future__ import annotations

import os
import subprocess
from functools import cache
from pathlib import Path


@cache
def _git_toplevel(start: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if out else None


@cache
def _resolve_project_root(start: Path) -> Path:
    top = _git_toplevel(start)
    if top is not None:
        return top
    return start


def find_project_root(start: Path | None = None) -> Path:
    """Resolve the project root for the current invocation.

    Env override (``TIMELINE_PROJECT_ROOT``) is read on every call so callers
    can override it per-test; the underlying git lookup is cached by start
    path.
    """
    env = os.environ.get("TIMELINE_PROJECT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    start = (start or Path.cwd()).resolve()
    return _resolve_project_root(start)


def find_timeline_home(project_root: Path) -> Path:
    """Resolve the timeline home directory (where outputs live)."""
    env = os.environ.get("TIMELINE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (project_root / "timeline").resolve()
