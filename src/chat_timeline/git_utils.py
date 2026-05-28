"""Thin git command wrappers.

Extracted from ``_legacy/main.py`` in v0.2.0. Every function takes an
explicit ``cwd`` parameter — the new modules pass it from a ``Paths``
context; the legacy main module wraps these to bind ``PROJECT_DIR``.

All helpers return stripped stdout (and `(stdout, returncode)` from
``git_run``). Failures surface as empty strings — callers that care about
the exit code should use ``git_run`` directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git_run(*args: str, cwd: Path) -> tuple[str, int]:
    """Run ``git <args>`` in ``cwd``. Returns ``(stdout_stripped, returncode)``."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip(), result.returncode


def get_staged_diff(cwd: Path) -> str:
    diff, _ = git_run("diff", "--cached", cwd=cwd)
    return diff


def get_unstaged_diff(cwd: Path) -> str:
    diff, _ = git_run("diff", cwd=cwd)
    return diff


def get_staged_files(cwd: Path) -> str:
    out, _ = git_run("diff", "--cached", "--name-status", cwd=cwd)
    return out


def get_unstaged_files(cwd: Path) -> str:
    out, _ = git_run("diff", "--name-status", cwd=cwd)
    return out


def get_untracked_files(cwd: Path) -> str:
    out, _ = git_run("ls-files", "--others", "--exclude-standard", cwd=cwd)
    return out


def get_head_hash(cwd: Path) -> str:
    out, _ = git_run("rev-parse", "HEAD", cwd=cwd)
    return out


def get_head_short(cwd: Path) -> str:
    out, _ = git_run("rev-parse", "--short", "HEAD", cwd=cwd)
    return out


def get_current_branch(cwd: Path) -> str:
    out, _ = git_run("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    return out


def get_head_message(cwd: Path) -> str:
    out, _ = git_run("log", "-1", "--format=%B", cwd=cwd)
    return out.strip()


def get_head_date(cwd: Path) -> str:
    out, _ = git_run("log", "-1", "--format=%aI", cwd=cwd)
    return out.strip()


def git_mv(src: Path, dst: Path, cwd: Path) -> None:
    """Move a file using ``git mv``, falling back to ``shutil.move`` if untracked."""
    result = subprocess.run(
        ["git", "mv", str(src), str(dst)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  git mv {src.name} -> {dst.name}")
    else:
        shutil.move(str(src), str(dst))
        print(f"  mv {src.name} -> {dst.name}  (not tracked by git)")
