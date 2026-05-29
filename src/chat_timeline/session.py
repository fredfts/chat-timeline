"""Session generation — builds ``timeline/sessions/session.md``.

Extracted from ``_legacy/main.py`` in Phase 5.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from chat_timeline._state import (
    PROJECT_DIR,
    SESSIONS_DIR,
    STAGED_DIR,
)
from chat_timeline.git_utils import (
    get_current_branch as _gu_get_current_branch,
)
from chat_timeline.git_utils import (
    get_head_date as _gu_get_head_date,
)
from chat_timeline.git_utils import (
    get_head_hash as _gu_get_head_hash,
)
from chat_timeline.git_utils import (
    get_head_message as _gu_get_head_message,
)
from chat_timeline.git_utils import (
    get_head_short as _gu_get_head_short,
)
from chat_timeline.git_utils import (
    get_staged_diff as _gu_get_staged_diff,
)
from chat_timeline.git_utils import (
    get_staged_files as _gu_get_staged_files,
)
from chat_timeline.git_utils import (
    get_unstaged_diff as _gu_get_unstaged_diff,
)
from chat_timeline.git_utils import (
    get_unstaged_files as _gu_get_unstaged_files,
)
from chat_timeline.git_utils import (
    get_untracked_files as _gu_get_untracked_files,
)
from chat_timeline.git_utils import (
    git_mv as _gu_git_mv,
)
from chat_timeline.markdown import fenced_literal_block, fmt_dt


def get_head_hash():
    return _gu_get_head_hash(PROJECT_DIR)


def get_head_short():
    return _gu_get_head_short(PROJECT_DIR)


def get_current_branch():
    return _gu_get_current_branch(PROJECT_DIR)


def get_head_message():
    return _gu_get_head_message(PROJECT_DIR)


def get_head_date():
    return _gu_get_head_date(PROJECT_DIR)


def get_staged_diff():
    return _gu_get_staged_diff(PROJECT_DIR)


def get_unstaged_diff():
    return _gu_get_unstaged_diff(PROJECT_DIR)


def get_staged_files():
    return _gu_get_staged_files(PROJECT_DIR)


def get_unstaged_files():
    return _gu_get_unstaged_files(PROJECT_DIR)


def get_untracked_files():
    return _gu_get_untracked_files(PROJECT_DIR)


def _git_mv(src: Path, dst: Path):
    return _gu_git_mv(src, dst, cwd=PROJECT_DIR)


def rotate_session():
    """Archive existing session.md as [HEAD_hash].md in /timeline/sessions/."""
    session_path = SESSIONS_DIR / "session.md"
    if not session_path.exists():
        return
    commit_short = get_head_short()
    archive_path = SESSIONS_DIR / f"{commit_short}.md"
    _git_mv(session_path, archive_path)


def generate_session():
    """Build a structured session from /timeline/chats/ + git index diffs."""
    lines = []

    commit_hash = get_head_hash()
    commit_short = get_head_short()
    branch = get_current_branch()
    commit_msg = get_head_message()
    commit_date = get_head_date()

    # Collect staged chat exports
    chat_files = sorted(STAGED_DIR.glob("*.md")) if STAGED_DIR.exists() else []

    # Diffs
    staged_diff = get_staged_diff()
    unstaged_diff = get_unstaged_diff()
    staged_files = get_staged_files()
    unstaged_files = get_unstaged_files()
    untracked = get_untracked_files()

    # Build output
    lines.append("---")
    lines.append("type: commit_timeline")
    lines.append(f'commit: "{commit_hash}"')
    lines.append(f'commit_short: "{commit_short}"')
    lines.append(f'branch: "{branch}"')
    lines.append(f'commit_message: "{commit_msg}"')
    lines.append(f'commit_date: "{commit_date}"')
    lines.append(f"chat_sessions: {len(chat_files)}")
    lines.append(f'generated: "{fmt_dt(datetime.now(timezone.utc))}"')
    lines.append("---")
    lines.append("")

    lines.append(f"# Commit Timeline: {commit_short}")
    lines.append("")
    lines.append(f"**Branch:** {branch}")
    lines.append(f"**Message:** {commit_msg}")
    lines.append(f"**Date:** {commit_date}")
    lines.append("")

    # Section 1: Chat sessions content
    lines.append("# Chat Sessions")
    lines.append("")
    if not chat_files:
        lines.append(
            "*No chat exports found in /timeline/chats/. Run `python main.py <source>` to export.*"
        )
        lines.append("")
    else:
        for cf in chat_files:
            lines.append(f"## {cf.stem}")
            lines.append("")
            content = cf.read_text(encoding="utf-8")
            lines.append(fenced_literal_block(content, "markdown"))
            lines.append("")
            lines.append("---")
            lines.append("")

    # Section 2: Git diffs
    lines.append("# Git Index State")
    lines.append("")

    if staged_files:
        lines.append("## Staged files")
        lines.append("")
        lines.append("```")
        lines.append(staged_files)
        lines.append("```")
        lines.append("")

    if unstaged_files:
        lines.append("## Unstaged modifications")
        lines.append("")
        lines.append("```")
        lines.append(unstaged_files)
        lines.append("```")
        lines.append("")

    if untracked:
        lines.append("## Untracked files")
        lines.append("")
        lines.append("```")
        lines.append(untracked)
        lines.append("```")
        lines.append("")

    if staged_diff:
        lines.append("## Staged diff")
        lines.append("")
        lines.append("```diff")
        lines.append(staged_diff)
        lines.append("```")
        lines.append("")

    if unstaged_diff:
        lines.append("## Unstaged diff")
        lines.append("")
        lines.append("```diff")
        lines.append(unstaged_diff)
        lines.append("```")
        lines.append("")

    # Section 3: Edition timeline
    lines.append("# Edition Timeline")
    lines.append("")
    lines.append("*Chronological list of all file modifications from chat sessions.*")
    lines.append("*Edits not covered by any chat tool call are marked as manual user edits.*")
    lines.append("")

    # Collect all tool calls with timestamps and files from chat exports
    all_edits = []
    project_prefix = PROJECT_DIR.resolve().as_posix().lower()

    def normalize_path(p):
        """Normalize a file path to be relative to the project root."""
        p = p.replace("\\", "/")
        p_lower = p.lower()
        for prefix in [project_prefix + "/", project_prefix.replace("/", "\\") + "\\"]:
            if p_lower.startswith(prefix.lower()):
                p = p[len(prefix) :]
                break
        if re.match(r"^[A-Za-z]:", p):
            rel = os.path.relpath(p, PROJECT_DIR.resolve().as_posix())
            if not rel.startswith(".."):
                p = rel.replace("\\", "/")
        return p

    for cf in chat_files:
        content = cf.read_text(encoding="utf-8")
        # Extract edit_file / Edit tool calls
        for m in re.finditer(
            r"`(?:edit_file(?:_v2)?|Edit): ([^`]+)`\s*\((\w+)\)\s*([\d-]+\s[\d:]+\s\w+)", content
        ):
            all_edits.append(
                {
                    "type": "ai_edit",
                    "file": normalize_path(m.group(1)),
                    "status": m.group(2),
                    "timestamp": m.group(3),
                    "source": cf.stem,
                }
            )
        for m in re.finditer(
            r"`(?:read_file(?:_v2)?|Read): ([^`]+)`\s*\((\w+)\)\s*([\d-]+\s[\d:]+\s\w+)", content
        ):
            all_edits.append(
                {
                    "type": "ai_read",
                    "file": normalize_path(m.group(1)),
                    "status": m.group(2),
                    "timestamp": m.group(3),
                    "source": cf.stem,
                }
            )

    # Identify files changed in git that are NOT in any AI edit
    ai_edited_files = {
        e["file"].replace("\\", "/").lower() for e in all_edits if e["type"] == "ai_edit"
    }
    for status_line in (staged_files + "\n" + unstaged_files).strip().split("\n"):
        if not status_line.strip():
            continue
        parts = status_line.split("\t", 1)
        if len(parts) == 2:
            git_file = parts[1].replace("\\", "/")
            if git_file.lower() not in ai_edited_files:
                all_edits.append(
                    {
                        "type": "manual_edit",
                        "file": git_file,
                        "status": parts[0].strip(),
                        "timestamp": "",
                        "source": "git index (manual user edit)",
                    }
                )

    all_edits.sort(key=lambda e: e.get("timestamp", ""))

    if all_edits:
        lines.append("| Timestamp | Type | File | Source |")
        lines.append("|---|---|---|---|")
        for e in all_edits:
            lines.append(f"| {e['timestamp']} | {e['type']} | `{e['file']}` | {e['source']} |")
        lines.append("")
    else:
        lines.append("*No edits detected.*")
        lines.append("")

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SESSIONS_DIR / "session.md"
    md_text = "\n".join(lines)
    out_path.write_text(md_text, encoding="utf-8")
    return out_path, len(md_text)


# ---------------------------------------------------------------------------
# Timeline generation
# ---------------------------------------------------------------------------
