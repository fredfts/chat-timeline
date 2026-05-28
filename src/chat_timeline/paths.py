"""Project-root and timeline-home resolution.

The legacy code derived these from ``Path(__file__).parent``, which only works
when the source files live inside the target project. Now that the tool is
installed system-wide, we resolve them from the current working directory:

  * ``PROJECT_DIR`` = ``$TIMELINE_PROJECT_ROOT`` if set, else
    ``git rev-parse --show-toplevel`` of cwd, else cwd.
  * ``HISTORY_DIR`` = ``$TIMELINE_HOME`` if set, else ``<PROJECT_DIR>/timeline``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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


def find_project_root(start: Path | None = None) -> Path:
    """Resolve the project root for the current invocation."""
    env = os.environ.get("TIMELINE_PROJECT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    start = (start or Path.cwd()).resolve()
    top = _git_toplevel(start)
    if top is not None:
        return top
    return start


def find_timeline_home(project_root: Path) -> Path:
    """Resolve the timeline home directory (where outputs live)."""
    env = os.environ.get("TIMELINE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (project_root / "timeline").resolve()
