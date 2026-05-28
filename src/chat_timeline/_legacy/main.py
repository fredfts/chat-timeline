"""
Chat history exporter, session & timeline generator.
Supports Cursor, Claude Code, and Codex.

Usage:
  python main.py                     All sources: export chats + session + timeline
  python main.py cursor              Cursor only: export chats + session + timeline
  python main.py claude              Claude Code only
  python main.py codex               Codex only
  python main.py cursor 1,4,6        Select specific chats
  python main.py -c                  Export chats only (all sources)
  python main.py cursor -c 1,4,6     Export specific Cursor chats only
  python main.py -s                  Generate session.md only
  python main.py -t                  Update timeline (incremental append)
  python main.py -t -rt              Rebuild timeline from scratch
  python main.py -t -rc              Timeline ignoring used-state dedup
  python main.py -t -r               Full reset (timeline + used-state)
  python main.py -o                  Enable old timeline rotation in normal runs
  python main.py -c -s               Combine: chats + session
  python main.py -p                  Pre-commit standalone: auto-select modified chats
"""

import os
import sys
import json
import hashlib
import shutil
import subprocess
import argparse
import re
import platform
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Paths — resolved from the current project via chat_timeline.paths.
# PROJECT_DIR = git toplevel of cwd (or $TIMELINE_PROJECT_ROOT, or cwd).
# HISTORY_DIR = <project>/timeline by default (or $TIMELINE_HOME).
# All downstream constants derive from HISTORY_DIR.
# ---------------------------------------------------------------------------

from chat_timeline.paths import find_project_root, find_timeline_home

PROJECT_DIR = find_project_root()
HISTORY_DIR = find_timeline_home(PROJECT_DIR)
SCRIPT_DIR = HISTORY_DIR  # legacy alias — modules expect this name
try:
    HISTORY_DIR_NAME = HISTORY_DIR.resolve().relative_to(
        PROJECT_DIR.resolve()).as_posix()
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


# ---------------------------------------------------------------------------
# Pure helpers — extracted to chat_timeline.markdown in v0.2.0.
# This module keeps thin wrappers that bind PROJECT_DIR so internal callers
# (and the source modules that still import from here) don't change.
# ---------------------------------------------------------------------------

from chat_timeline import markdown as _md
from chat_timeline.markdown import (  # noqa: F401  (re-exported for callers)
    epoch_ms_to_dt,
    fenced_literal_block,
    fmt_dt,
    fmt_dt_filename,
    iso_to_dt,
    parse_chat_export,
    parse_selection,
    sanitize_filename,
    sanitize_markdown_content,
    strip_redacted,
)


def relative_path(p):
    return _md.relative_path(p, PROJECT_DIR)


def format_tool_call_detail(tc):
    return _md.format_tool_call_detail(tc, project_root=PROJECT_DIR)


def export_chat_markdown(meta, turns, include_tool_params=False):
    return _md.export_chat_markdown(meta, turns, include_tool_params, project_root=PROJECT_DIR)


# ---------------------------------------------------------------------------
# Git helpers — extracted to chat_timeline.git_utils in v0.2.0.
# Wrappers bind PROJECT_DIR so internal callers don't change shape.
# ---------------------------------------------------------------------------

from chat_timeline import git_utils as _gu


def git_run(*args, cwd=None):
    return _gu.git_run(*args, cwd=Path(cwd) if cwd else PROJECT_DIR)


def get_staged_diff():
    return _gu.get_staged_diff(PROJECT_DIR)


def get_unstaged_diff():
    return _gu.get_unstaged_diff(PROJECT_DIR)


def get_staged_files():
    return _gu.get_staged_files(PROJECT_DIR)


def get_unstaged_files():
    return _gu.get_unstaged_files(PROJECT_DIR)


def get_untracked_files():
    return _gu.get_untracked_files(PROJECT_DIR)


def get_head_hash():
    return _gu.get_head_hash(PROJECT_DIR)


def get_head_short():
    return _gu.get_head_short(PROJECT_DIR)


def get_current_branch():
    return _gu.get_current_branch(PROJECT_DIR)


def get_head_message():
    return _gu.get_head_message(PROJECT_DIR)


def get_head_date():
    return _gu.get_head_date(PROJECT_DIR)


def _git_mv(src: Path, dst: Path):
    return _gu.git_mv(src, dst, cwd=PROJECT_DIR)


# ---------------------------------------------------------------------------
# Pre-commit state helpers
# ---------------------------------------------------------------------------

def _load_precommit_state():
    """Load the pre-commit state file.

    Returns dict with 'enabled', 'last_run_ts', and 'tracked_chats'.
    tracked_chats maps chat key -> {"excluded_fingerprints": [...]}.
    """
    default = {
        "enabled": False,
        "last_run_ts": 0,
        "tracked_chats": {},
        "hot_only": False,
    }
    if not PRECOMMIT_STATE.exists():
        return default
    try:
        data = json.loads(PRECOMMIT_STATE.read_text(encoding="utf-8"))
        data.setdefault("tracked_chats", {})
        return {**default, **data}
    except Exception:
        return default


def _save_precommit_state(state):
    """Save the pre-commit state file."""
    PRECOMMIT_STATE.write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _is_amend_precommit():
    """Detect `git commit --amend` from env var or process ancestry.

    Priority:
    1. TIMELINE_AMEND env var (set by the shell hook on systems with /proc).
       HISTORY_AMEND is accepted as a legacy fallback.
    2. On Windows, walk the process tree via wmic looking for git + --amend.
    """
    if os.environ.get("TIMELINE_AMEND") or os.environ.get("HISTORY_AMEND"):
        return True
    if sys.platform == "win32":
        try:
            return _win32_ancestor_has_amend()
        except Exception:
            return False
    return False


def _win32_ancestor_has_amend():
    """Walk up the process tree on Windows looking for git --amend."""
    pid = os.getpid()
    for _ in range(6):
        try:
            out = subprocess.check_output(
                f"wmic process where ProcessId={pid}"
                " get CommandLine,ParentProcessId /format:list",
                shell=True, text=True, timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return False

        cmdline = ppid_str = ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("CommandLine="):
                cmdline = line.split("=", 1)[1]
            elif line.startswith("ParentProcessId="):
                ppid_str = line.split("=", 1)[1]

        if "git" in cmdline.lower() and "--amend" in cmdline:
            return True

        if not ppid_str or ppid_str == str(pid) or ppid_str == "0":
            return False
        pid = int(ppid_str)
    return False


def _install_hook():
    """Install the git pre-commit hook.

    The hook prefers the installed ``timeline`` console script, falls back
    to ``python -m chat_timeline`` (or WSL on Windows hosts).
    """
    hooks_dir = HOOK_PATH.parent
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_body = (
        'TOPLEVEL="$(git rev-parse --show-toplevel)"\n'
        'cd "$TOPLEVEL" || exit 0\n'
        '\n'
        '# Detect --amend from parent process command line\n'
        'if [ -r "/proc/$PPID/cmdline" ]; then\n'
        '  case "$(tr \'\\0\' \' \' < /proc/$PPID/cmdline)" in\n'
        '    *--amend*) export TIMELINE_AMEND=1 ;;\n'
        '  esac\n'
        'fi\n'
        '\n'
        'if command -v timeline >/dev/null 2>&1; then\n'
        '  timeline -p\n'
        'elif command -v python3 >/dev/null 2>&1; then\n'
        '  python3 -m chat_timeline -p\n'
        'elif command -v python >/dev/null 2>&1; then\n'
        '  python -m chat_timeline -p\n'
        'elif command -v wsl.exe >/dev/null 2>&1; then\n'
        '  wsl.exe timeline -p\n'
        'else\n'
        '  echo "pre-commit: chat-timeline not on PATH, skipping hook"\n'
        'fi\n'
    )

    legacy_script_rel = f"{HISTORY_DIR_NAME}/main.py"
    installed_markers = (
        "timeline -p",                 # new entry point
        "python -m chat_timeline -p",  # new module form
        "python3 -m chat_timeline -p",
        f"{legacy_script_rel} -p",     # legacy variants
        f"{legacy_script_rel} -x",
        "history/main.py -x",
        "timeline/main.py -x",
        "timeline/main.py -p",
    )

    # If a hook already exists, check if it's ours (current or legacy)
    if HOOK_PATH.exists():
        content = HOOK_PATH.read_text(encoding="utf-8", errors="replace")
        if any(marker in content for marker in installed_markers):
            return  # already installed (current or legacy variant)
        # Append to existing hook
        HOOK_PATH.write_text(
            content.rstrip("\n") + "\n\n"
            "# --- timeline pre-commit ---\n"
            + hook_body +
            "# --- end timeline pre-commit ---\n",
            encoding="utf-8")
        print("  pre-commit: appended timeline hook to existing hook")
        return

    HOOK_PATH.write_text(
        "#!/bin/sh\n"
        "# chat-timeline pre-commit hook — works in WSL, Git Bash, "
        "and POSIX shells\n"
        + hook_body,
        encoding="utf-8")
    HOOK_PATH.chmod(0o755)
    print("  pre-commit: hook installed")


def _uninstall_hook():
    """Remove the timeline pre-commit hook."""
    if not HOOK_PATH.exists():
        return
    content = HOOK_PATH.read_text(encoding="utf-8", errors="replace")
    # Strip current + legacy section markers from any appended-block install.
    new_content = content
    for marker_open, marker_close in (
        ("# --- timeline pre-commit ---", "# --- end timeline pre-commit ---"),
        ("# --- history pre-commit ---", "# --- end history pre-commit ---"),
    ):
        if marker_open in new_content:
            new_content = re.sub(
                rf"\n*{re.escape(marker_open)}\n.*?{re.escape(marker_close)}\n?",
                "", new_content, flags=re.DOTALL)

    # Standalone-hook detection on the post-strip content. Legacy hooks split
    # `SCRIPT="$TOPLEVEL/timeline/main.py"` from `python3 "$SCRIPT" -x` across
    # lines, so a literal `timeline/main.py -x` never appears — match the path
    # and the flag independently, and also recognise the header comments.
    has_flag = any(
        flag in new_content for flag in (" -p\n", " -x\n", ' -p"', ' -x"')
    )
    is_standalone = (
        "# chat-timeline pre-commit hook" in new_content
        or "# timeline pre-commit hook" in new_content
        or "timeline -p" in new_content
        or "python -m chat_timeline -p" in new_content
        or "python3 -m chat_timeline -p" in new_content
        or ("timeline/main.py" in new_content and has_flag)
        or ("history/main.py" in new_content and has_flag)
    )

    if is_standalone:
        HOOK_PATH.unlink()
        print("  pre-commit: hook removed")
        return

    if new_content != content:
        if new_content.strip():
            HOOK_PATH.write_text(new_content, encoding="utf-8")
            print("  pre-commit: removed timeline hook (other hooks preserved)")
        else:
            HOOK_PATH.unlink()
            print("  pre-commit: hook removed")


def _get_modified_chats(chats, since_ts):
    """Return indices of chats modified after since_ts (epoch seconds)."""
    modified = []
    for i, c in enumerate(chats):
        chat_ts = c.get("lastUpdatedAt", 0) / 1000  # ms -> seconds
        if chat_ts > since_ts:
            modified.append(i)
        elif "_jsonl_path" in c:
            # Claude Code: also check file mtime
            try:
                mtime = c["_jsonl_path"].stat().st_mtime
                if mtime > since_ts:
                    modified.append(i)
            except OSError:
                pass
    return modified


# ---------------------------------------------------------------------------
# Session generation
# ---------------------------------------------------------------------------

def rotate_session():
    """Archive existing session.md as [HEAD_hash].md in /timeline/sessions/."""
    session_path = SESSIONS_DIR / "session.md"
    if not session_path.exists():
        return
    commit_short = get_head_short()
    archive_path = SESSIONS_DIR / f"{commit_short}.md"
    _git_mv(session_path, archive_path)


def _read_frontmatter_field(path, field):
    """Read a single field value from a file's YAML-like frontmatter."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm:
        return None
    for line in fm.group(1).splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip().strip('"')
    return None


def _read_timeline_json_parent(path):
    """Read parent_commit from contents/timeline.json metadata."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data.get("metadata", {}) if isinstance(data, dict) else {}
        parent = meta.get("parent_commit")
        return str(parent).strip() if parent else None
    except Exception:
        return None


def _next_archive_candidate(base_path: Path):
    """Return next free suffixed archive path: name__2.ext, name__3.ext, ..."""
    suffix = 2
    while True:
        candidate = base_path.with_name(f"{base_path.stem}__{suffix}{base_path.suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


def _stamp_commit_field(path, commit_short):
    """Inject a commit field into a file's frontmatter (md) or metadata (json)."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("metadata", {})["commit"] = commit_short
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        except Exception:
            pass
        return

    content = path.read_text(encoding="utf-8", errors="replace")
    fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm:
        return
    fm_text = fm.group(1)
    # Insert commit field after the parent_commit line (or at end of frontmatter)
    if re.search(r'^commit:', fm_text, re.MULTILINE):
        return  # already has commit field
    insert_after = re.search(r'^parent_commit:.*$', fm_text, re.MULTILINE)
    if insert_after:
        pos = insert_after.end()
        new_fm = fm_text[:pos] + f'\ncommit: "{commit_short}"' + fm_text[pos:]
    else:
        new_fm = fm_text + f'\ncommit: "{commit_short}"'
    content = f"---\n{new_fm}\n---{content[fm.end():]}"
    path.write_text(content, encoding="utf-8")


def rotate_timeline(force=False, archive_key_override=None):
    """Archive existing timeline.md and contents/timeline.json.

    Before rotating, stamps the closing commit into the frontmatter.

    The archive key defaults to the current HEAD (the last commit that
    contained this timeline).  Pass archive_key_override to use a
    different hash — e.g. HEAD~1 when amending a commit that never
    touched the timeline.

    If the target archive name already exists, a numbered suffix (__2, __3...)
    is used to preserve prior archives instead of skipping rotation.

    Returns True if timeline files were archived (moved), False if rotation
    was skipped (nothing to do, or HEAD already matches committed parent).
    """
    tl = HISTORY_DIR / "timeline.md"
    js = CONTENTS_DIR / "timeline.json"
    if not tl.exists() and not js.exists():
        return False

    commit_short = get_head_short()
    tl_parent = _read_frontmatter_field(tl, "parent_commit") if tl.exists() else None
    js_parent = _read_timeline_json_parent(js)

    # For the rotation guard, use the COMMITTED parent_commit from the
    # tracked timeline.md.  Both the working-tree md and the gitignored
    # json can be updated by prior runs without being committed, making
    # them unreliable for detecting commit-cycle boundaries.
    committed_tl_parent = None
    raw, rc = git_run("show", f"HEAD:{HISTORY_DIR_NAME}/timeline.md")
    if rc == 0 and raw:
        m = re.match(r"^---\n(.*?)\n---", raw, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("parent_commit:"):
                    committed_tl_parent = line.split(":", 1)[1].strip().strip('"')
                    break
    active_parent = committed_tl_parent or tl_parent or js_parent

    # Guard: skip only when the COMMITTED timeline says it's already on HEAD.
    # If committed parent is missing/unreadable, prefer rotating instead of
    # risking an unintended append onto the current timeline.
    if (not force) and committed_tl_parent and committed_tl_parent == commit_short:
        print(f"  HEAD ({commit_short}) unchanged since committed timeline generation, skipping rotation")
        return False

    archive_key = archive_key_override or commit_short
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    archive_md = TIMELINE_DIR / f"{archive_key}.md"
    archive_json = CONTENTS_DIR / f"{archive_key}.json"

    # Avoid destination collisions.
    # Keep unsuffixed names for whichever side is still free (md/json) and
    # suffix only the colliding side to repair partial archives gracefully.
    md_target = archive_md
    json_target = archive_json
    md_exists = archive_md.exists()
    json_exists = archive_json.exists()
    if md_exists and json_exists:
        base_md = _next_archive_candidate(archive_md)
        base_json = CONTENTS_DIR / f"{base_md.stem}.json"
        while base_json.exists():
            base_md = _next_archive_candidate(base_md)
            base_json = CONTENTS_DIR / f"{base_md.stem}.json"
        md_target = base_md
        json_target = base_json
        print(f"  archive target already exists, using {md_target.name}")
    elif md_exists:
        md_target = _next_archive_candidate(archive_md)
        print(f"  archive md target exists, using {md_target.name}")
    elif json_exists:
        json_target = _next_archive_candidate(archive_json)
        print(f"  archive json target exists, using {json_target.name}")

    # Stamp the closing commit into the open timeline before archiving.
    # git mv only renames the index entry (preserving the blob), so the
    # stamped content must be staged first to travel with the rename.
    if tl.exists():
        _stamp_commit_field(tl, archive_key)
        git_run("add", "--", str(tl))
    if js.exists():
        _stamp_commit_field(js, archive_key)

    if tl.exists():
        _git_mv(tl, md_target)
    if js.exists():
        _git_mv(js, json_target)

    # Solidify pending used-state now that the timeline is closed
    solidify_used_state()
    return True


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
    lines.append(f"type: commit_timeline")
    lines.append(f"commit: \"{commit_hash}\"")
    lines.append(f"commit_short: \"{commit_short}\"")
    lines.append(f"branch: \"{branch}\"")
    lines.append(f"commit_message: \"{commit_msg}\"")
    lines.append(f"commit_date: \"{commit_date}\"")
    lines.append(f"chat_sessions: {len(chat_files)}")
    lines.append(f"generated: \"{fmt_dt(datetime.now(timezone.utc))}\"")
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
        lines.append("*No chat exports found in /timeline/chats/. Run `python main.py <source>` to export.*")
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
                p = p[len(prefix):]
                break
        if re.match(r'^[A-Za-z]:', p):
            rel = os.path.relpath(p, PROJECT_DIR.resolve().as_posix())
            if not rel.startswith(".."):
                p = rel.replace("\\", "/")
        return p

    for cf in chat_files:
        content = cf.read_text(encoding="utf-8")
        # Extract edit_file / Edit tool calls
        for m in re.finditer(r'`(?:edit_file(?:_v2)?|Edit): ([^`]+)`\s*\((\w+)\)\s*([\d-]+\s[\d:]+\s\w+)', content):
            all_edits.append({
                "type": "ai_edit",
                "file": normalize_path(m.group(1)),
                "status": m.group(2),
                "timestamp": m.group(3),
                "source": cf.stem,
            })
        for m in re.finditer(r'`(?:read_file(?:_v2)?|Read): ([^`]+)`\s*\((\w+)\)\s*([\d-]+\s[\d:]+\s\w+)', content):
            all_edits.append({
                "type": "ai_read",
                "file": normalize_path(m.group(1)),
                "status": m.group(2),
                "timestamp": m.group(3),
                "source": cf.stem,
            })

    # Identify files changed in git that are NOT in any AI edit
    ai_edited_files = {e["file"].replace("\\", "/").lower() for e in all_edits if e["type"] == "ai_edit"}
    for status_line in (staged_files + "\n" + unstaged_files).strip().split("\n"):
        if not status_line.strip():
            continue
        parts = status_line.split("\t", 1)
        if len(parts) == 2:
            git_file = parts[1].replace("\\", "/")
            if git_file.lower() not in ai_edited_files:
                all_edits.append({
                    "type": "manual_edit",
                    "file": git_file,
                    "status": parts[0].strip(),
                    "timestamp": "",
                    "source": "git index (manual user edit)",
                })

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

def chat_used_state_path(meta, filepath):
    """State path in /timeline/chats/used keyed by composer_id, fallback to stem."""
    composer_id = sanitize_filename(meta.get("composer_id", "").strip())
    fallback = sanitize_filename(filepath.stem)
    key = composer_id or fallback or "unknown_chat"
    return USED_DIR / f"{key}.json"


def load_used_state(path):
    """Load per-chat used-state JSON safely.

    Merges hashes from solidified (.json) and pending (.pending.json)
    so that entries added in the latest run are visible even before
    the pending state is solidified by the next rotation.
    """
    default = {"version": 1, "seen_entry_hashes": []}
    candidates = [path]
    if path.suffix == ".json" and not path.stem.endswith(".pending"):
        candidates.append(path.with_suffix(".pending.json"))

    merged_hashes = set()
    best_data = None
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        hashes = data.get("seen_entry_hashes", [])
        if not isinstance(hashes, list):
            hashes = []
        merged_hashes.update(hashes)
        if best_data is None:
            best_data = data

    if best_data is None:
        return default
    best_data["seen_entry_hashes"] = sorted(merged_hashes)
    return best_data


def save_used_state(path, payload):
    """Persist per-chat used-state as pending (not yet solidified)."""
    USED_DIR.mkdir(parents=True, exist_ok=True)
    pending_path = path.with_suffix(".pending.json")
    pending_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def solidify_used_state():
    """Promote all pending used-state files to final.

    Called during timeline rotation — makes fingerprints permanent.
    """
    if not USED_DIR.exists():
        return
    for pending in USED_DIR.glob("*.pending.json"):
        final = pending.with_name(pending.name.replace(".pending.json", ".json"))
        shutil.move(str(pending), str(final))


def entry_fingerprint(meta, turn):
    """Stable SHA-256 fingerprint for a turn (redaction-clean)."""
    payload = dict(
        user_model=turn.get("user_model", ""),
        user_timestamp=turn.get("user_timestamp", ""),
        user_text=strip_redacted((turn.get("user_text") or "").strip()),
        thinking_blocks=[
            dict(
                timestamp=blk.get("timestamp", ""),
                duration_s=float(blk.get("duration_s", 0.0)),
                text=strip_redacted((blk.get("text") or "").strip()),
            )
            for blk in turn.get("thinking_blocks", [])
        ],
        tool_calls=[
            dict(
                detail=strip_redacted((tc.get("detail") or "").strip()),
                status=tc.get("status", ""),
                timestamp=tc.get("timestamp", ""),
            )
            for tc in turn.get("tool_calls", [])
        ],
        response_timestamp=turn.get("response_timestamp", ""),
        response_text=strip_redacted((turn.get("response_text") or "").strip()),
    )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _format_turn_numbers(numbers):
    """Compact turn numbers into a label like Q12-14,17."""
    nums = sorted({n for n in numbers if isinstance(n, int)})
    if not nums:
        return "Q?"
    ranges = []
    start = end = nums[0]
    for n in nums[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append(f"{start}" if start == end else f"{start}-{end}")
            start = end = n
    ranges.append(f"{start}" if start == end else f"{start}-{end}")
    return "Q" + ",".join(ranges)


def _normalize_identity_value(value):
    """Normalize text used for stable cross-run entry identity."""
    text = strip_redacted((str(value) if value is not None else "").strip())
    return re.sub(r"\s+", " ", text).strip()


def entry_identity_from_turn(chat_name, turn):
    """Stable model-agnostic identity for a parsed turn."""
    try:
        turn_num = int(turn.get("number", 0))
    except Exception:
        turn_num = str(turn.get("number", ""))

    chat = _normalize_identity_value(chat_name)
    prompt = _normalize_identity_value(turn.get("user_text", ""))
    if not chat:
        return None
    return (chat, turn_num, prompt)


def entry_identity_from_entry(entry):
    """Stable model-agnostic identity for a timeline entry dict."""
    try:
        turn_num = int(entry.get("turn", 0))
    except Exception:
        turn_num = str(entry.get("turn", ""))

    chat = _normalize_identity_value(entry.get("chat", ""))
    prompt = _normalize_identity_value(entry.get("prompt", ""))
    if not chat:
        return None
    return (chat, turn_num, prompt)


def _entry_id_num(eid):
    """Numeric part of an entry id like E0042; -1 on parse failure."""
    try:
        return int(str(eid).lstrip("E"))
    except Exception:
        return -1


# Tool-name substrings that mark a file-mutating call. "Edit" catches both
# Claude's Edit and NotebookEdit; "edit_file" catches Cursor and Codex
# variants; Write covers Claude's Write; apply_patch covers Codex; write_file
# is a Codex/other alias seen in some sessions.
_FILE_CHANGE_MARKERS = (
    "edit_file", "Edit", "Write", "apply_patch", "write_file",
)


def _is_file_change_tool(name):
    return any(m in name for m in _FILE_CHANGE_MARKERS)


def _turn_has_file_changes(turn):
    """True iff any tool call in the turn modifies a file on disk.

    Used by hot-only mode to filter out conversation turns that didn't
    actually touch any code (planning, debugging dialogue, lints, etc.).
    """
    for tc in turn.get("tool_calls", []):
        detail = tc.get("detail", "")
        name = detail.split(":", 1)[0].strip()
        if _is_file_change_tool(name):
            return True
    return False


def _build_timeline_entry_payload(eid, chat_name, turn, turn_hash):
    """Build one timeline entry plus its referenced content records."""
    prompt = strip_redacted((turn.get("user_text") or "").strip())
    content_items = []

    th_refs = []
    th_total_s = 0.0
    for i, blk in enumerate(turn.get("thinking_blocks", [])):
        th_id = f"TH-{eid}-{i}"
        th_refs.append(th_id)
        duration_s = float(blk.get("duration_s", 0.0) or 0.0)
        th_total_s += duration_s
        blk_text = strip_redacted((blk.get("text") or "").strip())
        content_items.append(dict(
            id=th_id, type="thinking", entry=eid,
            chat=chat_name, turn=turn.get("number", 0), block=i,
            timestamp=blk.get("timestamp", ""),
            duration_s=duration_s,
            text=blk_text,
            word_count=len(blk_text.split())))

    tool_summary = {}
    edits_list = []
    reads_list = []
    for tc in turn.get("tool_calls", []):
        detail = tc.get("detail", "")
        name = detail.split(":", 1)[0].strip()
        tool_summary[name] = tool_summary.get(name, 0) + 1
        file_part = detail.split(":", 1)[1].strip() if ":" in detail else ""
        if _is_file_change_tool(name):
            if file_part:
                edits_list.append(file_part)
        elif any(x in name for x in ("read_file", "Read")):
            if file_part:
                reads_list.append(file_part)

    tc_ref = None
    if turn.get("tool_calls"):
        tc_ref = f"TC-{eid}"
        content_items.append(dict(
            id=tc_ref, type="tool_calls", entry=eid,
            chat=chat_name, turn=turn.get("number", 0),
            count=len(turn.get("tool_calls", [])),
            summary=tool_summary,
            calls=[dict(
                name=tc.get("detail", "").split(":", 1)[0].strip(),
                detail=tc.get("detail", ""),
                status=tc.get("status", ""),
                timestamp=tc.get("timestamp", ""))
                for tc in turn.get("tool_calls", [])],
            edits=edits_list, reads=reads_list))

    resp_text = strip_redacted((turn.get("response_text") or "").strip())
    resp_ref = None
    resp_words = len(resp_text.split()) if resp_text else 0
    if resp_text:
        resp_ref = f"R-{eid}"
        content_items.append(dict(
            id=resp_ref, type="response", entry=eid,
            chat=chat_name, turn=turn.get("number", 0),
            model=turn.get("response_model", ""),
            timestamp=turn.get("response_timestamp", ""),
            text=resp_text, word_count=resp_words))

    entry = dict(
        id=eid, timestamp=turn.get("user_timestamp", ""),
        chat=chat_name, turn=turn.get("number", 0),
        model=turn.get("user_model", ""), prompt=prompt,
        fingerprint=turn_hash,
        thinking_refs=th_refs,
        thinking_count=len(turn.get("thinking_blocks", [])),
        thinking_s=th_total_s,
        tc_ref=tc_ref,
        tool_count=len(turn.get("tool_calls", [])),
        tool_summary=tool_summary,
        edits=edits_list, reads=reads_list,
        resp_ref=resp_ref,
        resp_model=turn.get("response_model", ""),
        resp_words=resp_words)

    return entry, content_items


def get_chat_entries(chat, exporters,
                     archive_fingerprints=None,
                     archive_identities=None):
    """Export a chat temporarily and return list of entry dicts with fingerprints.

    Each entry dict has: number, timestamp, prompt (first 80 chars),
    fingerprint, is_used (already in used-state), number_label, count.
    Duplicate turns (same fingerprint) are collapsed into one selector row.
    Returns (entries_list, used_state_path) or ([], None) on failure.

    is_used is True when:
    - the fingerprint is in the per-chat used-state, OR
    - the fingerprint matches one in any prior-cycle archive, OR
    - the (chat, turn, prompt) identity matches an archive entry.

    The archive identity check catches turns whose response is still
    streaming after the pre-commit ran: the response_text changes, so the
    fingerprint shifts, but the identity stays stable and the entry is
    really already shipped (will just refresh on the next run).
    """
    src = chat.get("_source")
    export_fn = exporters.get(src)
    if not export_fn:
        return [], None

    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    path = export_fn(chat, include_tool_params=False)
    if not path:
        return [], None

    try:
        meta, turns = parse_chat_export(path)
        used_path = chat_used_state_path(meta, path)
        used_state = load_used_state(used_path)
        seen_hashes = set(used_state.get("seen_entry_hashes", []))

        chat_title = meta.get("title", path.stem)
        source = meta.get("source", "").strip()
        chat_name = chat_title
        if source and not chat_name.endswith(f" - {source}"):
            chat_name = f"{chat_name} - {source}"

        entries = []
        by_fingerprint = {}
        for turn in turns:
            fp = entry_fingerprint(meta, turn)
            prompt = strip_redacted((turn.get("user_text") or "").strip())
            first_line = prompt.split("\n")[0][:80]
            existing = by_fingerprint.get(fp)
            if existing:
                existing["numbers"].append(turn["number"])
                existing["count"] += 1
                continue

            identity = entry_identity_from_turn(chat_name, turn)
            is_used = fp in seen_hashes
            if not is_used and archive_fingerprints and fp in archive_fingerprints:
                is_used = True
            if (not is_used and archive_identities
                    and identity is not None
                    and identity in archive_identities):
                is_used = True

            entry = {
                "number": turn["number"],
                "numbers": [turn["number"]],
                "number_label": f"Q{turn['number']}",
                "count": 1,
                "timestamp": turn.get("user_timestamp", ""),
                "prompt": first_line,
                "fingerprint": fp,
                "is_used": is_used,
                "has_file_changes": _turn_has_file_changes(turn),
            }
            entries.append(entry)
            by_fingerprint[fp] = entry

        for entry in entries:
            entry["number_label"] = _format_turn_numbers(
                entry.get("numbers", [entry.get("number", 0)]))

        return entries, used_path
    finally:
        # Selector expansion uses temporary exports; don't let preview files
        # leak into staged output, which would add unintended chats.
        try:
            if path.exists() and path.parent.resolve() == STAGED_DIR.resolve():
                path.unlink()
        except OSError:
            pass


def _compute_timeline_stats(entries, content):
    """Compute cumulative stats from merged entries and content."""
    models = set()
    edits = set()
    reads = set()
    for e in entries:
        models.add(e.get("model", ""))
        if e.get("resp_model"):
            models.add(e["resp_model"])
        edits.update(e.get("edits", []))
        reads.update(e.get("reads", []))
    models.discard("")

    return dict(
        chats=len(set(e["chat"] for e in entries)) if entries else 0,
        entries=len(entries),
        thinking_blocks=sum(1 for c in content if c["type"] == "thinking"),
        thinking_s=sum(c.get("duration_s", 0) for c in content
                       if c["type"] == "thinking"),
        tool_calls=sum(c.get("count", 0) for c in content
                       if c["type"] == "tool_calls"),
        edits=edits,
        reads=reads,
        response_words=sum(e.get("resp_words", 0) for e in entries),
        models=models,
    )


def _compute_per_chat_stats(entries):
    """Compute per-chat stats from merged entries."""
    chat_map = {}
    for e in entries:
        cn = e["chat"]
        if cn not in chat_map:
            chat_map[cn] = dict(name=cn, prompts=0, thinking=0,
                                thinking_s=0.0, tools=0, edits=0, words=0)
        cs = chat_map[cn]
        cs["prompts"] += 1
        cs["thinking"] += e.get("thinking_count", 0)
        cs["thinking_s"] += e.get("thinking_s", 0)
        cs["tools"] += e.get("tool_count", 0)
        cs["edits"] += len(e.get("edits", []))
        cs["words"] += e.get("resp_words", 0)
    return list(chat_map.values())


def _render_timeline_md(all_entries, all_content, stats, chat_stats,
                        commit_short, branch, now_iso):
    """Render timeline.md from merged data."""
    lines = []

    lines.append("---")
    lines.append("type: timeline")
    lines.append(f'parent_commit: "{commit_short}"')
    lines.append(f'branch: "{branch}"')
    lines.append(f'generated: "{now_iso}"')
    lines.append(f'chats: {stats["chats"]}')
    lines.append(f'entries: {stats["entries"]}')
    lines.append(f'content_items: {len(all_content)}')
    lines.append("---")
    lines.append("")

    lines.append("# Timeline")
    lines.append("")

    lines.append("## Stats")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f'| Chats | {stats["chats"]} |')
    lines.append(f'| Entries | {stats["entries"]} |')
    lines.append(f'| Thinking blocks | {stats["thinking_blocks"]}'
                 f' ({stats["thinking_s"]:.0f}s) |')
    lines.append(f'| Tool calls | {stats["tool_calls"]} |')
    lines.append(f'| Files edited | {len(stats["edits"])} |')
    lines.append(f'| Files read | {len(stats["reads"])} |')
    lines.append(f'| Response words | {stats["response_words"]:,} |')
    lines.append(f'| Models | {", ".join(sorted(stats["models"]))} |')
    lines.append("")

    if chat_stats:
        lines.append("**Per chat:**")
        lines.append("")
        lines.append("| Chat | Entries | Thinking | Tools | Edits | Words |")
        lines.append("|---|---|---|---|---|---|")
        for cs in chat_stats:
            lines.append(
                f'| {cs["name"][:60]} | {cs["prompts"]}'
                f' | {cs["thinking"]} ({cs["thinking_s"]:.0f}s)'
                f' | {cs["tools"]} | {cs["edits"]}'
                f' | {cs["words"]:,} |')
        lines.append("")

    lines.append("## Entries")
    lines.append("")

    if not all_entries:
        lines.append("*No entries.*")
        lines.append("")
    else:
        for entry in all_entries:
            lines.append(f'**{entry["id"]}** [{entry["timestamp"]}]'
                         f' Q{entry["turn"]} — {entry["chat"]}')
            lines.append("")
            lines.append(f'Model: {entry["model"]}')
            lines.append("")

            if entry["prompt"]:
                for pline in entry["prompt"].split("\n"):
                    if pline.strip():
                        lines.append(f"> {pline}")
                lines.append("")

            if entry["thinking_count"] > 0:
                refs = ", ".join(entry["thinking_refs"])
                lines.append(f'- Thinking: {entry["thinking_count"]} blocks,'
                             f' {entry["thinking_s"]:.1f}s [{refs}]')
            if entry["tool_count"] > 0:
                parts = [f"{n} x{c}" for n, c in entry["tool_summary"].items()]
                lines.append(f'- Tools ({entry["tool_count"]}):'
                             f' {", ".join(parts)} [{entry["tc_ref"]}]')
            if entry["edits"]:
                files = ", ".join(f'`{f}`' for f in entry["edits"])
                lines.append(f"- Edits: {files}")
            if entry["reads"]:
                flist = entry["reads"][:5]
                files = ", ".join(f'`{f}`' for f in flist)
                if len(entry["reads"]) > 5:
                    files += f' +{len(entry["reads"]) - 5} more'
                lines.append(f"- Reads: {files}")
            if entry["resp_ref"]:
                lines.append(f'- Response: {entry["resp_words"]:,} words'
                             f' [{entry["resp_ref"]}]')

            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def _collect_archive_dedup_data(include_open=False):
    """Aggregate fingerprints + identities from all closed-cycle archives.

    Returns (fingerprints, identities). Used as a defensive dedup source
    when a fresh cycle starts in strict-dedup mode (pre-commit just rotated).
    The per-chat used-state under chats/used/ is the primary source, but it
    can be incomplete — e.g., a prior run's solidify lost a .pending.json,
    or a chat fork shifted composer_id. Archives are canonical for what has
    already shipped in any prior commit's timeline.

    include_open: also pull from the currently-open contents/timeline.json.
    The TUI uses this to mark entries already in the open cycle as "used"
    in the selector, even before the next rotation seals them.
    generate_timeline keeps it off because it loads timeline.json
    separately as existing_entries (it would otherwise double-count).
    """
    fingerprints = set()
    identities = set()
    if not CONTENTS_DIR.exists():
        return fingerprints, identities
    for jf in CONTENTS_DIR.glob("*.json"):
        if jf.name == "timeline.json" and not include_open:
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in data.get("entries", []):
            fp = e.get("fingerprint")
            if fp:
                fingerprints.add(fp)
            ident = entry_identity_from_entry(e)
            if ident is not None:
                identities.add(ident)
    return fingerprints, identities


# Placeholder eid stored in existing_identity_to_eid for identities loaded
# from prior archives — non-empty so the identity-first branch fires, and
# absent from existing_entry_index so the refresh path is skipped (turn is
# treated as already-shipped and dropped from the new cycle).
_ARCHIVED_EID_PLACEHOLDER = "_archived"


def generate_timeline(reset_chats=False, reset_timeline=False,
                      excluded_fingerprints=None,
                      force_add_fingerprints=None,
                      clean_mode_chat_keys=None,
                      strict_dedup=False,
                      hot_only=False,
                      explicit_select_fingerprints=None):
    """Build/update timeline.md + contents/timeline.json.

    Incremental by default: appends new entries to existing timeline.
    Two-layer dedup prevents duplicates:
      Layer 1 — within-timeline: fingerprints already in timeline.json
      Layer 2 — cross-timeline: solidified used-state (.json files)

    reset_chats:    ignore solidified used-state (layer 2)
    reset_timeline: wipe existing timeline data and start fresh (layer 1)
    strict_dedup:   keep Layer 2 active even on fresh timeline cycles
                    (used by pre-commit to stay strictly incremental).
                    Combined with reset_timeline, also pulls fingerprints
                    from prior archives so cross-cycle dedup survives any
                    gap in chats/used/ state.
    hot_only:       skip emitting entries for turns that did not touch any
                    file (no Edit/Write/apply_patch tool call). Cold turns
                    still bypass dedup/watermark for this turn, so they can
                    surface in a later non-hot-only run. Combine with
                    reset_timeline to get a pure hot-only rebuild.
                    Force-add ('t') and explicit Space cherry-pick override
                    this filter — the user said "include this one".
    explicit_select_fingerprints: per-entry Space cherry-picks from the TUI.
                    Treated as an explicit "include" signal that bypasses
                    hot_only (in addition to is_forced).
    """
    json_path = CONTENTS_DIR / "timeline.json"
    has_staged = STAGED_DIR.exists() and list(STAGED_DIR.glob("*.md"))

    # Load existing timeline data (unless resetting)
    existing_entries = []
    existing_content = []
    existing_fingerprints = set()
    existing_identity_to_eid = {}
    existing_entry_index = {}

    if not reset_timeline and json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            existing_entries = data.get("entries", [])
            existing_content = data.get("content", [])
        except Exception:
            pass

    # Self-heal previously duplicated entries using a model-agnostic identity.
    deduped_existing = 0
    if existing_entries:
        keep_by_identity = {}
        keep_ids = set()
        for entry in existing_entries:
            eid = entry.get("id")
            if not eid:
                continue
            identity = entry_identity_from_entry(entry)
            if identity is None:
                keep_ids.add(eid)
                continue
            prev_id = keep_by_identity.get(identity)
            if prev_id is None:
                keep_by_identity[identity] = eid
            elif _entry_id_num(eid) >= _entry_id_num(prev_id):
                keep_by_identity[identity] = eid
                deduped_existing += 1
            else:
                deduped_existing += 1
        keep_ids.update(keep_by_identity.values())
        if deduped_existing:
            existing_entries = [
                e for e in existing_entries
                if (not e.get("id")) or (e.get("id") in keep_ids)
            ]
            existing_content = [
                c for c in existing_content
                if (not c.get("entry")) or (c.get("entry") in keep_ids)
            ]
            print(f"  Cleaned {deduped_existing} duplicate existing entries")

    for i, entry in enumerate(existing_entries):
        fp = entry.get("fingerprint")
        if fp:
            existing_fingerprints.add(fp)
        eid = entry.get("id")
        identity = entry_identity_from_entry(entry)
        if eid:
            existing_entry_index[eid] = i
            if identity is not None:
                existing_identity_to_eid[identity] = eid

    # Pre-commit just rotated and is opening a fresh cycle: also block
    # anything that already shipped in a prior cycle's archive. Per-chat
    # used-state alone has lost entries in the past (e.g., a .pending.json
    # that never reached solidify).
    if strict_dedup and reset_timeline:
        archive_fps, archive_idents = _collect_archive_dedup_data()
        before = len(existing_fingerprints)
        existing_fingerprints.update(archive_fps)
        for ident in archive_idents:
            existing_identity_to_eid.setdefault(
                ident, _ARCHIVED_EID_PLACEHOLDER)
        added = len(existing_fingerprints) - before
        if added:
            print(f"  Loaded {added} archived fingerprints for cross-cycle dedup")

    if not has_staged and not existing_entries:
        print("  No staged chats and no existing timeline data.")
        return None, None, 0

    # Determine next entry number
    next_entry_num = 1
    if existing_entries:
        try:
            last_num = int(existing_entries[-1]["id"][1:])
            next_entry_num = last_num + 1
        except (ValueError, KeyError):
            next_entry_num = len(existing_entries) + 1

    # Process staged chats
    new_entries = []
    new_content = []
    entry_num = next_entry_num
    now_iso = fmt_dt(datetime.now(timezone.utc))

    # Fresh timeline cycle: no existing entries were loaded (either the file
    # was rotated away, reset_timeline was True, or the file was empty).
    # In this case, skip Layer 2 (used-state) dedup — the user is starting
    # a new timeline and explicitly staged chats should have their entries
    # included regardless of what appeared in prior timelines.
    # Exception: strict_dedup keeps Layer 2 active even on fresh cycles
    # (pre-commit uses this to stay strictly incremental).
    fresh_cycle = (not existing_entries) and (not strict_dedup)

    seen_chat_names = {}
    for fp in sorted(STAGED_DIR.glob("*.md")) if has_staged else []:
        meta, turns = parse_chat_export(fp)
        chat_name = meta.get("title", fp.stem)
        source = meta.get("source", "").strip()
        if source and not chat_name.endswith(f" - {source}"):
            chat_name = f"{chat_name} - {source}"

        base_chat_name = chat_name
        if base_chat_name in seen_chat_names:
            seen_chat_names[base_chat_name] += 1
            chat_name = f"{base_chat_name} ({seen_chat_names[base_chat_name]})"
        else:
            seen_chat_names[base_chat_name] = 1

        used_path = chat_used_state_path(meta, fp)
        used_state = load_used_state(used_path)
        seen_hashes = set(used_state.get("seen_entry_hashes", []))

        is_clean = False
        if clean_mode_chat_keys:
            cid = meta.get("composer_id", "").strip()
            source = meta.get("source", "").strip()
            title = meta.get("title", fp.stem)
            candidates = set()
            if cid:
                candidates.add(cid)
            if source and title:
                candidates.add(f"{source}:{title}")
                candidates.add(f"{source.lower()}:{title}")
            is_clean = bool(candidates & clean_mode_chat_keys)

        turn_hashes = [entry_fingerprint(meta, t) for t in turns]
        turn_identities = [entry_identity_from_turn(chat_name, t) for t in turns]
        all_turn_hashes = list(turn_hashes)
        last_added_idx = -1
        checkpoint_auto_skip_hashes = set()

        # In checkpoint mode, non-used entries up to the last used checkpoint
        # are default-skipped unless force-added.
        if (not reset_chats) and (not is_clean):
            last_used_idx = -1
            used_flags = []
            for i, th in enumerate(turn_hashes):
                used = th in seen_hashes
                used_flags.append(used)
                if used:
                    last_used_idx = i
            if last_used_idx >= 0:
                checkpoint_auto_skip_hashes = {
                    th for i, th in enumerate(turn_hashes)
                    if i <= last_used_idx and not used_flags[i]
                }

        for ti, turn in enumerate(turns):
            turn_hash = turn_hashes[ti]
            turn_identity = turn_identities[ti]

            # Identity-first dedup/refresh:
            # same chat+turn+timestamp+prompt should never produce a new entry,
            # even if metadata (e.g., model) changed between runs.
            if turn_identity is not None:
                existing_eid = existing_identity_to_eid.get(turn_identity)
                if existing_eid:
                    existing_idx = existing_entry_index.get(existing_eid)
                    if existing_idx is not None:
                        existing_entry = existing_entries[existing_idx]
                        existing_fp = existing_entry.get("fingerprint")
                        if existing_fp != turn_hash:
                            refreshed_entry, refreshed_content = (
                                _build_timeline_entry_payload(
                                    existing_eid, chat_name, turn, turn_hash))
                            existing_entries[existing_idx] = refreshed_entry
                            existing_content = [
                                c for c in existing_content
                                if c.get("entry") != existing_eid
                            ]
                            existing_content.extend(refreshed_content)
                            if existing_fp:
                                if not any(
                                    e.get("fingerprint") == existing_fp
                                    for e in existing_entries
                                ):
                                    existing_fingerprints.discard(existing_fp)
                            existing_fingerprints.add(turn_hash)
                    last_added_idx = max(last_added_idx, ti)
                    continue

            # Layer 1: within-timeline dedup (includes entries added this run)
            if turn_hash in existing_fingerprints:
                last_added_idx = max(last_added_idx, ti)
                continue

            is_forced = (force_add_fingerprints
                         and turn_hash in force_add_fingerprints)

            # Layer 2: cross-timeline dedup (solidified used-state)
            # Skipped on fresh cycle, clean-mode chats, or forced entries.
            if (not reset_chats) and (not is_clean) and (not fresh_cycle) and turn_hash in seen_hashes and not is_forced:
                continue

            # Layer 3: checkpoint auto-skip
            if checkpoint_auto_skip_hashes and turn_hash in checkpoint_auto_skip_hashes and not is_forced:
                continue

            # Layer 4: user-excluded entries (from interactive tracking)
            if excluded_fingerprints and turn_hash in excluded_fingerprints:
                continue

            # Hot-only filter: drop turns without any file change unless the
            # user explicitly opted them in via 't' force-add or Space
            # cherry-pick. Do NOT advance last_added_idx or
            # existing_fingerprints here, so the turn stays eligible for a
            # later non-hot-only run (the watermark may still mark it seen
            # if a later hot turn lands in this chat).
            is_explicit_pick = bool(
                explicit_select_fingerprints
                and turn_hash in explicit_select_fingerprints)
            if (hot_only
                    and not _turn_has_file_changes(turn)
                    and not is_forced
                    and not is_explicit_pick):
                continue

            existing_fingerprints.add(turn_hash)
            last_added_idx = max(last_added_idx, ti)

            eid = f"E{entry_num:04d}"
            entry_num += 1
            entry_payload, content_payload = _build_timeline_entry_payload(
                eid, chat_name, turn, turn_hash)
            new_entries.append(entry_payload)
            new_content.extend(content_payload)
            if turn_identity is not None:
                existing_identity_to_eid[turn_identity] = eid

        # Save pending used-state: watermark approach — save all hashes
        # up to the last processed timeline-aligned entry so earlier entries
        # don't reappear, while entries beyond the watermark remain available
        # for future runs.
        if last_added_idx >= 0:
            watermark_hashes = set(all_turn_hashes[:last_added_idx + 1])
            updated_hashes = seen_hashes | watermark_hashes
        else:
            # No entries added this run — keep existing baseline
            updated_hashes = set(seen_hashes)

        save_used_state(used_path, dict(
            version=1,
            composer_id=meta.get("composer_id", ""),
            chat_title=chat_name,
            source_file=fp.name,
            updated_at=now_iso,
            seen_entry_hashes=sorted(updated_hashes),
        ))

    # Merge existing + new
    all_entries = existing_entries + new_entries
    all_content = existing_content + new_content
    all_entries.sort(key=lambda e: e["timestamp"])

    # Compute cumulative stats
    stats = _compute_timeline_stats(all_entries, all_content)
    chat_stats = _compute_per_chat_stats(all_entries)

    # Render timeline.md
    commit_short = get_head_short()
    branch = get_current_branch()
    tl_md = _render_timeline_md(
        all_entries, all_content, stats, chat_stats,
        commit_short, branch, now_iso)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    tl_path = HISTORY_DIR / "timeline.md"
    tl_path.write_text(tl_md, encoding="utf-8")

    # Write timeline.json (with entries for incremental append)
    content_json = dict(
        metadata=dict(
            generated=now_iso,
            parent_commit=commit_short,
            entries=len(all_entries),
            content_items=len(all_content)),
        entries=all_entries,
        content=all_content)
    CONTENTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(content_json, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    added = len(new_entries)
    if added:
        print(f"  Added {added} new entries (total: {len(all_entries)})")
    elif existing_entries:
        print(f"  No new entries (total: {len(all_entries)})")

    return tl_path, json_path, len(tl_md)


# ---------------------------------------------------------------------------
# Interactive UI (cross-platform)
# ---------------------------------------------------------------------------

PAGE_SIZE = 5


def compact_selection(indices):
    """Turn a set of 0-based indices into '1-3,6-7,10' (1-based, ranges collapsed)."""
    if not indices:
        return ""
    nums = sorted(i + 1 for i in indices)
    parts = []
    start = end = nums[0]
    for n in nums[1:]:
        if n == end + 1:
            end = n
        else:
            parts.append(f"{start}" if start == end else f"{start}-{end}")
            start = end = n
    parts.append(f"{start}" if start == end else f"{start}-{end}")
    return ",".join(parts)


def parse_selection_string(s, total):
    """Parse '1-3,7' into a set of 0-based indices."""
    indices = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                for x in range(int(a), int(b) + 1):
                    if 1 <= x <= total:
                        indices.add(x - 1)
            elif part.isdigit():
                x = int(part)
                if 1 <= x <= total:
                    indices.add(x - 1)
        except ValueError:
            pass
    return indices


import time as _time
import select as _select

HOLD_SECONDS = 3.0

if platform.system() == "Windows":
    import msvcrt

    def _read_key():
        """Read one logical keypress via msvcrt. Returns a string identifier."""
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {
                "H": "up", "P": "down",
                "I": "pgup", "Q": "pgdn",
                "G": "home", "O": "end",
                "K": "left", "M": "right",
            }.get(code, "")
        if ch == "\r":
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x08":
            return "backspace"
        if ch == " ":
            return "space"
        return ch

    def _check_hold(char, duration=HOLD_SECONDS):
        """Check if `char` is held for `duration` seconds (Windows)."""
        deadline = _time.monotonic() + duration
        while _time.monotonic() < deadline:
            if not msvcrt.kbhit():
                _time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            if ch == char:
                continue  # auto-repeat of same key
            return False  # different key pressed
        return True
else:
    import tty
    import termios

    def _read_key():
        """Read one logical keypress via termios. Returns a string identifier."""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 in "ABCDHF":
                        return {"A": "up", "B": "down",
                                "C": "right", "D": "left",
                                "H": "home", "F": "end"}.get(ch3, "")
                    if ch3 in "56":
                        sys.stdin.read(1)  # consume the ~
                        return {"5": "pgup", "6": "pgdn"}.get(ch3, "")
                    if ch3 in "14":
                        sys.stdin.read(1)  # consume the ~
                        return {"1": "home", "4": "end"}.get(ch3, "")
                return "esc"
            if ch in ("\r", "\n"):
                return "enter"
            if ch in ("\x7f", "\x08"):
                return "backspace"
            if ch == " ":
                return "space"
            if ch == "\t":
                return "\t"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _check_hold(char, duration=HOLD_SECONDS):
        """Check if `char` is held for `duration` seconds (Linux/WSL)."""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            deadline = _time.monotonic() + duration
            while _time.monotonic() < deadline:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = _select.select([fd], [], [], min(0.05, remaining))
                if ready:
                    ch = sys.stdin.read(1)
                    if ch == char:
                        continue  # auto-repeat
                    return False  # different key
            return True
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _flush_stdin():
    """Drain any buffered characters from stdin."""
    if platform.system() == "Windows":
        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ready, _, _ = _select.select([fd], [], [], 0.0)
                if ready:
                    sys.stdin.read(1)
                else:
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _check_hold_with_feedback(char, render_cb, duration=HOLD_SECONDS):
    """Check for a held key with visual feedback via render_cb(elapsed).

    render_cb is called every ~100ms with the elapsed time.
    Returns True if held for the full duration, False if released early.
    After returning, stdin is flushed to prevent buffered auto-repeat
    chars from being processed as new keypresses.
    """
    start = _time.monotonic()
    result = False
    while True:
        elapsed = _time.monotonic() - start
        if elapsed >= duration:
            result = True
            break
        render_cb(elapsed)
        if platform.system() == "Windows":
            _time.sleep(0.1)
            if not msvcrt.kbhit():
                break
            ch = msvcrt.getwch()
            if ch != char:
                break
        else:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ready, _, _ = _select.select([fd], [], [], 0.1)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch != char:
                        break
                else:
                    break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    _flush_stdin()
    return result


def _build_row_map(chats, expanded, entry_cache):
    """Build a flat list of virtual rows from chats + expanded entries.

    Each row is a dict: {"type": "chat", "chat_idx": i} or
    {"type": "entry", "chat_idx": i, "entry_idx": j, "entry": entry_dict}.
    """
    rows = []
    for i in range(len(chats)):
        rows.append({"type": "chat", "chat_idx": i})
        if i in expanded and i in entry_cache:
            for j, e in enumerate(entry_cache[i]):
                rows.append({"type": "entry", "chat_idx": i,
                             "entry_idx": j, "entry": e})
    return rows


def _render(chats, rows, cursor, window_start, selected,
            selected_entries, input_mode, input_buf,
            precommit_on, tracking_modes, excluded_fps, force_add_fps,
            expanded, auto_skip_fps, reset_mode=False,
            hold_key=None, hold_elapsed=0.0, old_enabled=False,
            hot_only=False):
    """Draw the selector UI in-place and return updated window_start."""
    total = len(chats)
    total_rows = len(rows)
    cols, term_rows = shutil.get_terminal_size((120, 30))
    info_rows = 6
    max_rows = max(3, term_rows - info_rows)
    draw_rows = min(total_rows, max_rows)
    max_start = max(0, total_rows - draw_rows)

    if cursor < window_start:
        window_start = cursor
    elif cursor >= window_start + draw_rows:
        window_start = cursor - draw_rows + 1
    window_start = max(0, min(window_start, max_start))
    window_end = min(total_rows, window_start + draw_rows)

    lines = []
    sel_str = compact_selection(selected) or "none"
    pc_label = (f" | pre-commit ON ({len(tracking_modes)} tracked)"
                if precommit_on else "")
    reset_label = " | -r mode" if reset_mode else ""
    old_label = " | rotate ON" if old_enabled else " | rotate off"
    hot_label = " | hot ON" if hot_only else " | hot off"
    lines.append(
        f"  selected {len(selected)}/{total}"
        f"{pc_label}{reset_label}{old_label}{hot_label}")

    # Show current row info
    cur_row = rows[cursor] if cursor < total_rows else None
    if cur_row and cur_row["type"] == "entry":
        cur_entry = cur_row["entry"]
        entry_label = cur_entry.get(
            "number_label", f"Q{cur_entry.get('number', '?')}")
        entry_count = cur_entry.get("count", 1)
        count_suffix = f" (x{entry_count})" if entry_count > 1 else ""
        lines.append(f"  row {cursor + 1}/{total_rows}"
                     f" | entry {entry_label}{count_suffix}"
                     f" of chat #{cur_row['chat_idx'] + 1}")
    else:
        lines.append(f"  row {cursor + 1}/{total_rows}")
    lines.append("")

    # Hold indicator
    if hold_key:
        bar_len = int((hold_elapsed / HOLD_SECONDS) * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        action = "untrack all" if hold_key == "t" else hold_key
        lines.append(f"  HOLD {hold_key}: [{bar}] {action}")
        lines.append("")

    name_w = max(20, cols - 48)
    for ri in range(window_start, window_end):
        row = rows[ri]
        arrow = ">" if ri == cursor else " "

        if row["type"] == "chat":
            i = row["chat_idx"]
            c = chats[i]
            dt = epoch_ms_to_dt(c["lastUpdatedAt"])
            name = c.get("name", "(unnamed)")
            mode_name = c.get("unifiedMode", "?")
            check = "[x]" if i in selected else "[ ]"
            mode = tracking_modes.get(i)
            trk = " ●" if mode == "tracked" else " ◆" if mode == "tracked-checkpoint" else "  "
            exp = "v" if i in expanded else " "
            lines.append(
                f" {arrow} {check} {i+1:>4}  {fmt_dt(dt):<20} "
                f"{mode_name:<6}{trk} {exp} {name[:name_w]}")
        else:
            # Entry row
            e = row["entry"]
            ci = row["chat_idx"]
            ei = row["entry_idx"]
            fp = e["fingerprint"]

            # Selection checkbox
            if reset_mode:
                echeck = "[x]"  # always on in reset mode
            else:
                entry_sel = selected_entries.get(ci, set())
                echeck = "[x]" if ei in entry_sel else "[ ]"

            trk_status = ""
            if precommit_on and ci in tracking_modes:
                e_mode = tracking_modes[ci]
                is_excluded = fp in excluded_fps.get(ci, set())
                is_forced = fp in force_add_fps.get(ci, set())
                is_used = e["is_used"] if e_mode == "tracked-checkpoint" else False
                is_auto_skip = (fp in auto_skip_fps.get(ci, set())
                                if e_mode == "tracked-checkpoint" else False)
                # When hot-only is on, cold turns are silently filtered by
                # generate_timeline. Mirror that in the per-entry label so
                # the TUI doesn't promise "+add" for entries that won't land.
                is_cold = hot_only and not e.get("has_file_changes", True)
                if is_used:
                    trk_status = " +add" if is_forced else " used"
                elif is_auto_skip:
                    trk_status = " +add" if is_forced else " skip"
                elif is_excluded:
                    trk_status = " skip"
                elif is_cold:
                    trk_status = " +add" if is_forced else " cold"
                else:
                    trk_status = " +add"

            prompt = e["prompt"][:max(10, name_w - 10)]
            q_label = e.get("number_label", f"Q{e['number']}")
            if e.get("count", 1) > 1:
                q_label = f"{q_label} x{e['count']}"
            lines.append(
                f" {arrow}     {echeck} {q_label:<10} "
                f"{e['timestamp']:<20}{trk_status:>5}  {prompt}")

    lines.append("")
    if input_mode:
        lines.append(f"  input: {input_buf}_")
        lines.append("  Type ranges like 1-3,7 | Tab/Enter apply | Esc discard")
    else:
        lines.append(f"  selection: {sel_str}")
        help_parts = [
            "Up/Down", "Space sel",
            "Right expand", "Left collapse",
            "Tab input", "a all", "Enter ok", "Esc cancel",
            "p pre-commit", "t track (●/◆)",
            "o rotate old", "h hot entries only",
        ]
        lines.append("  " + " | ".join(help_parts))

    sys.stdout.write("\033[H\033[J")
    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()
    return window_start


def _compute_deselected_fps(selected, selected_entries, entry_cache, reset_mode):
    """Compute fingerprints of entries the user explicitly deselected.

    In reset_mode, no entries can be deselected so returns empty set.
    For chats with no selected_entries entry, all entries are implicitly selected.
    """
    if reset_mode:
        return set()
    deselected = set()
    for ci in selected:
        if ci not in selected_entries:
            continue  # all entries implicitly selected
        if ci not in entry_cache:
            continue
        sel_ei = selected_entries[ci]
        for ei, e in enumerate(entry_cache[ci]):
            if ei not in sel_ei:
                deselected.add(e["fingerprint"])
    return deselected


def _compute_explicit_select_fps(selected, selected_entries, entry_cache, reset_mode):
    """Fingerprints of entries the user explicitly Space-picked.

    Only counts entries from chats the user expanded and cherry-picked.
    A chat selected without expansion is "implicit all" — those entries
    are not explicit picks and do not bypass hot-only.
    """
    if reset_mode:
        return set()
    explicit = set()
    for ci in selected:
        if ci not in selected_entries:
            continue
        if ci not in entry_cache:
            continue
        sel_ei = selected_entries[ci]
        for ei, e in enumerate(entry_cache[ci]):
            if ei in sel_ei:
                explicit.add(e["fingerprint"])
    return explicit


def _auto_skip_fps_for_entries(entries):
    """Fingerprints of non-used entries that should default to 'skip'.

    In a tracked-checkpoint chat, entries up to the last used checkpoint
    that are not marked used are auto-skipped (toggleable to +add).
    """
    last_used_idx = -1
    for i, e in enumerate(entries):
        if e["is_used"]:
            last_used_idx = i
    if last_used_idx < 0:
        return set()
    return {e["fingerprint"] for i, e in enumerate(entries)
            if i <= last_used_idx and not e["is_used"]}


def _chat_key_for_tracking(chat):
    """Return a stable key for tracking a chat across sessions.

    Priority:
      1) composer_id  (Cursor + exported chats)
      2) _session_id  (Claude Code native sessions)
      3) source:name  (legacy fallback)
    """
    cid = (chat.get("composer_id") or "").strip()
    if cid:
        return cid
    sid = (chat.get("_session_id") or "").strip()
    if sid:
        return sid
    return f"{chat.get('_source', '')}:{chat.get('name', '')}"


def _chat_tracking_lookup_keys(chat):
    """Lookup keys for persisted tracking state (newest -> legacy)."""
    keys = []
    primary = _chat_key_for_tracking(chat)
    if primary:
        keys.append(primary)
    legacy = f"{chat.get('_source', '')}:{chat.get('name', '')}"
    if legacy and legacy not in keys:
        keys.append(legacy)
    return keys


def _chat_last_updated_ms(chat):
    """Return chat lastUpdatedAt as integer milliseconds."""
    try:
        return int(chat.get("lastUpdatedAt", 0) or 0)
    except Exception:
        return 0


def _removed_marker_payload(chat):
    """Build a removed-marker payload with chat version snapshot."""
    return {
        "removed": True,
        "removed_chat_last_updated_at": _chat_last_updated_ms(chat),
    }


def _removed_marker_is_active(td, chat, is_modified=False):
    """Whether a removed marker should still suppress auto-tracking.

    New markers store the chat's lastUpdatedAt at removal time and expire
    automatically when the chat is edited again (timestamp increases).

    Legacy markers (without timestamp) are treated as active only while the
    chat is not currently modified.
    """
    if not isinstance(td, dict) or not td.get("removed", False):
        return False
    marker_ts = td.get("removed_chat_last_updated_at")
    if marker_ts is None:
        return not is_modified
    try:
        marker_ts = int(marker_ts or 0)
    except Exception:
        marker_ts = 0
    return _chat_last_updated_ms(chat) <= marker_ts


def interactive_select(chats, exporters=None, reset_mode=False):
    """Keyboard-driven chat selector with entry expansion and tracking.

    Returns (selected_indices, deselected_fps, old_enabled, hot_only,
             explicit_select_fps).
    deselected_fps: fingerprints the user explicitly deselected with Space.
    old_enabled: whether old timeline rotation was enabled via "o" key.
    hot_only: hot-only mode state (persisted in pc_state; "h" toggles it).
    explicit_select_fps: fingerprints the user explicitly Space-picked from
        an expanded chat. Treated as an "include this anyway" signal that
        bypasses hot-only.
    exporters: dict mapping source name -> export_single_chat function.
    reset_mode: if True, entry-level cherry-picking is disabled (all entries on).
    """
    total = len(chats)
    if total == 0:
        return [], set(), False, False, set()

    os.system("")
    cursor = 0
    window_start = 0
    selected = set()           # set of chat indices
    selected_entries = {}      # chat_idx -> set of entry indices (only if expanded)
    input_mode = False
    input_buf = ""
    old_enabled = False

    # Expansion state
    expanded = set()       # chat indices currently expanded
    entry_cache = {}       # chat_idx -> list of entry dicts

    # Pre-commit state
    pc_state = _load_precommit_state()
    precommit_on = pc_state.get("enabled", False)
    hot_only = pc_state.get("hot_only", False)
    since_ts = pc_state.get("last_run_ts", 0)
    modified_indices = (_get_modified_chats(chats, since_ts)
                        if precommit_on and since_ts > 0 else [])

    tracked_chats_data = pc_state.get("tracked_chats", {})
    tracking_modes = {}  # chat_idx -> "tracked" | "tracked-checkpoint"
    excluded_fps = {}    # chat_idx -> set of fingerprints to skip
    force_add_fps = {}   # chat_idx -> set of fingerprints to re-add despite used
    auto_skip_fps = {}   # chat_idx -> set of used fps before last new entry

    # Archive snapshot used to decide is_used for individual entries.
    # Includes the currently-open timeline.json so entries already in the
    # current cycle show as "used" even before the next rotation seals them.
    # Computed once at TUI start — globbing every contents/*.json on each
    # expand would be slow. Falls back to empty on failure so the TUI still
    # works.
    try:
        archive_fps, archive_idents = _collect_archive_dedup_data(
            include_open=True)
    except Exception:
        archive_fps, archive_idents = set(), set()

    if precommit_on:
        explicit_state_indices = set()
        modified_set = set(modified_indices)
        if not tracked_chats_data:
            for i in modified_indices:
                tracking_modes[i] = "tracked-checkpoint"
        for i in range(total):
            td = None
            for lookup_key in _chat_tracking_lookup_keys(chats[i]):
                if lookup_key in tracked_chats_data:
                    td = tracked_chats_data.get(lookup_key) or {}
                    break
            if td is not None:
                if _removed_marker_is_active(td, chats[i], i in modified_set):
                    explicit_state_indices.add(i)
                    tracking_modes.pop(i, None)
                    continue
                if td.get("removed", False):
                    # Stale removed marker (chat edited again) — ignore.
                    continue
                explicit_state_indices.add(i)
                tracking_modes[i] = td.get("mode", "tracked-checkpoint")
                excl = td.get("excluded_fingerprints", [])
                if excl:
                    excluded_fps[i] = set(excl)
                fadd = td.get("force_add_fingerprints", [])
                if fadd:
                    force_add_fps[i] = set(fadd)
        for i in modified_indices:
            if i not in explicit_state_indices:
                tracking_modes[i] = "tracked-checkpoint"

    def _save_tracking():
        td = {}
        for i, mode in tracking_modes.items():
            ck = _chat_key_for_tracking(chats[i])
            td[ck] = {
                "mode": mode,
                "excluded_fingerprints": sorted(excluded_fps.get(i, set())),
                "force_add_fingerprints": sorted(force_add_fps.get(i, set())),
            }
        if precommit_on:
            for i in set(modified_indices) - set(tracking_modes.keys()):
                ck = _chat_key_for_tracking(chats[i])
                td[ck] = _removed_marker_payload(chats[i])
        pc_state["tracked_chats"] = td
        _save_precommit_state(pc_state)

    def _ensure_entries(ci):
        """Load entry cache for a chat if not already loaded."""
        if ci not in entry_cache and exporters:
            entries, _ = get_chat_entries(
                chats[ci], exporters,
                archive_fingerprints=archive_fps,
                archive_identities=archive_idents)
            entry_cache[ci] = entries
            if tracking_modes.get(ci) == "tracked-checkpoint":
                auto_skip_fps[ci] = _auto_skip_fps_for_entries(entries)

    def _prune_stale_checkpoint_tracking():
        """Auto-untrack stale checkpoint chats with a cheap check.

        Avoid loading entry payloads here: that can be very expensive when
        many chats are auto-tracked. A chat is considered stale when it:
        - is in tracked-checkpoint mode
        - has no manual include/exclude overrides
        - is not currently modified (vs pre-commit baseline)
        - already has used-state checkpoint hashes
        """
        modified_set = set(modified_indices)
        stale = []
        for ci, mode in list(tracking_modes.items()):
            if mode != "tracked-checkpoint":
                continue
            if excluded_fps.get(ci) or force_add_fps.get(ci):
                continue
            if ci in modified_set:
                continue
            if _chat_has_checkpoint_data(ci):
                stale.append(ci)

        if not stale:
            return

        for ci in stale:
            tracking_modes.pop(ci, None)
            excluded_fps.pop(ci, None)
            force_add_fps.pop(ci, None)
            auto_skip_fps.pop(ci, None)

        _save_tracking()

    def _chat_has_checkpoint_data(ci):
        """Check if a chat has a used-state checkpoint with entries."""
        if ci in entry_cache:
            return any(e["is_used"] for e in entry_cache[ci])
        c = chats[ci]
        meta = {
            "composer_id": (
                c.get("composer_id")
                or c.get("_session_id")
                or c.get("id", "")
            )
        }
        dummy = Path(sanitize_filename(c.get("name", "unknown")))
        used_path = chat_used_state_path(meta, dummy)
        state = load_used_state(used_path)
        return bool(state.get("seen_entry_hashes"))

    def _select_chat_all_entries(ci):
        """Select a chat with all its entries on."""
        selected.add(ci)
        if ci in entry_cache:
            selected_entries[ci] = set(range(len(entry_cache[ci])))
        else:
            selected_entries.pop(ci, None)  # no cache = all implicit

    def _deselect_chat(ci):
        selected.discard(ci)
        selected_entries.pop(ci, None)

    def _do_render(hold_key=None, hold_elapsed=0.0):
        nonlocal window_start
        rows = _build_row_map(chats, expanded, entry_cache)
        window_start = _render(
            chats, rows, cursor, window_start, selected,
            selected_entries, input_mode, input_buf,
            precommit_on, tracking_modes, excluded_fps, force_add_fps,
            expanded, auto_skip_fps, reset_mode, hold_key, hold_elapsed,
            old_enabled, hot_only)
        return rows

    _prune_stale_checkpoint_tracking()

    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    try:
        rows = _do_render()

        while True:
            key = _read_key()
            if not key:
                continue

            rows = _build_row_map(chats, expanded, entry_cache)
            total_rows = len(rows)

            if input_mode:
                if key == "enter" or key == "\t":
                    selected = parse_selection_string(input_buf, total)
                    # In reset mode or without entry data, all entries selected
                    for ci in selected:
                        if ci in entry_cache:
                            selected_entries[ci] = set(
                                range(len(entry_cache[ci])))
                    input_mode = False
                elif key == "esc":
                    input_buf = compact_selection(selected)
                    input_mode = False
                elif key == "backspace":
                    input_buf = input_buf[:-1]
                elif len(key) == 1 and (key.isdigit() or key in ",-"):
                    input_buf += key
                rows = _do_render()
                continue

            cur_row = rows[cursor] if cursor < total_rows else None

            # --- t key: tracking (only when precommit is on) ---
            if key == "t" and precommit_on:
                held = _check_hold_with_feedback(
                    "t", lambda elapsed: _do_render(
                        hold_key="t", hold_elapsed=elapsed))

                if held:
                    tracking_modes.clear()
                    excluded_fps.clear()
                    force_add_fps.clear()
                    auto_skip_fps.clear()
                    pc_state["tracked_chats"] = {}
                    _save_precommit_state(pc_state)
                else:
                    if cur_row and cur_row["type"] == "chat":
                        ci = cur_row["chat_idx"]
                        current_mode = tracking_modes.get(ci)
                        has_cp = _chat_has_checkpoint_data(ci)

                        if has_cp:
                            if current_mode is None:
                                tracking_modes[ci] = "tracked-checkpoint"
                            elif current_mode == "tracked-checkpoint":
                                tracking_modes[ci] = "tracked"
                            else:
                                tracking_modes.pop(ci, None)
                                excluded_fps.pop(ci, None)
                                force_add_fps.pop(ci, None)
                        else:
                            if current_mode is None:
                                tracking_modes[ci] = "tracked"
                            else:
                                tracking_modes.pop(ci, None)
                                excluded_fps.pop(ci, None)
                                force_add_fps.pop(ci, None)
                        if tracking_modes.get(ci) == "tracked-checkpoint" and ci in entry_cache:
                            auto_skip_fps[ci] = _auto_skip_fps_for_entries(entry_cache[ci])
                        else:
                            auto_skip_fps.pop(ci, None)
                        _save_tracking()
                    elif (cur_row and cur_row["type"] == "entry"
                          and cur_row["chat_idx"] in tracking_modes):
                        ci = cur_row["chat_idx"]
                        e_mode = tracking_modes[ci]
                        fp = cur_row["entry"]["fingerprint"]
                        is_used = (cur_row["entry"]["is_used"]
                                   if e_mode == "tracked-checkpoint" else False)
                        is_auto_skip = (fp in auto_skip_fps.get(ci, set())
                                        if e_mode == "tracked-checkpoint" else False)
                        if ci not in excluded_fps:
                            excluded_fps[ci] = set()
                        if ci not in force_add_fps:
                            force_add_fps[ci] = set()

                        if is_used or is_auto_skip:
                            if fp in force_add_fps[ci]:
                                force_add_fps[ci].discard(fp)
                            else:
                                force_add_fps[ci].add(fp)
                        else:
                            if fp in excluded_fps[ci]:
                                excluded_fps[ci].discard(fp)
                            else:
                                excluded_fps[ci].add(fp)
                        _save_tracking()

                rows = _do_render()
                continue

            if key == "up":
                if cursor > 0:
                    cursor -= 1
            elif key == "down":
                if cursor < total_rows - 1:
                    cursor += 1
            elif key == "pgup":
                cursor = max(0, cursor - PAGE_SIZE)
            elif key == "pgdn":
                cursor = min(total_rows - 1, cursor + PAGE_SIZE)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(rows) - 1
            elif key == "right":
                if cur_row and cur_row["type"] == "chat":
                    ci = cur_row["chat_idx"]
                    if ci not in expanded:
                        _ensure_entries(ci)
                        expanded.add(ci)
                        # Initialize entry selection if chat is selected
                        if ci in selected and ci in entry_cache:
                            if ci not in selected_entries:
                                selected_entries[ci] = set(
                                    range(len(entry_cache[ci])))
            elif key == "left":
                if cur_row and cur_row["type"] == "chat":
                    expanded.discard(cur_row["chat_idx"])
                elif cur_row and cur_row["type"] == "entry":
                    ci = cur_row["chat_idx"]
                    expanded.discard(ci)
                    rows = _build_row_map(chats, expanded, entry_cache)
                    for ri, r in enumerate(rows):
                        if r["type"] == "chat" and r["chat_idx"] == ci:
                            cursor = ri
                            break
            elif key == "space":
                if cur_row and cur_row["type"] == "chat":
                    ci = cur_row["chat_idx"]
                    if ci in selected:
                        _deselect_chat(ci)
                    else:
                        _select_chat_all_entries(ci)
                    input_buf = compact_selection(selected)
                elif cur_row and cur_row["type"] == "entry" and not reset_mode:
                    # Entry-level selection toggle
                    ci = cur_row["chat_idx"]
                    ei = cur_row["entry_idx"]
                    if ci in selected:
                        # Chat is on — toggle this entry
                        if ci in selected_entries:
                            if ei in selected_entries[ci]:
                                selected_entries[ci].discard(ei)
                                # If last entry deselected, deselect chat
                                if not selected_entries[ci]:
                                    _deselect_chat(ci)
                                    input_buf = compact_selection(selected)
                            else:
                                selected_entries[ci].add(ei)
                        else:
                            # No entry tracking yet — all were implicit,
                            # deselect this one
                            if ci in entry_cache:
                                all_ei = set(range(len(entry_cache[ci])))
                                all_ei.discard(ei)
                                if not all_ei:
                                    _deselect_chat(ci)
                                    input_buf = compact_selection(selected)
                                else:
                                    selected_entries[ci] = all_ei
                    else:
                        # Chat is off — select chat with only this entry
                        selected.add(ci)
                        selected_entries[ci] = {ei}
                        input_buf = compact_selection(selected)
            elif key == "a":
                if len(selected) == total:
                    selected.clear()
                    selected_entries.clear()
                else:
                    selected = set(range(total))
                    # All entries for cached chats
                    for ci in selected:
                        if ci in entry_cache:
                            selected_entries[ci] = set(
                                range(len(entry_cache[ci])))
                input_buf = compact_selection(selected)
            elif key == "p":
                precommit_on = not precommit_on
                pc_state["enabled"] = precommit_on
                if precommit_on and since_ts == 0:
                    since_ts = _time.time()
                    pc_state["last_run_ts"] = since_ts
                _save_precommit_state(pc_state)
                if precommit_on:
                    _install_hook()
                    modified_indices = _get_modified_chats(chats, since_ts)
                    modified_set = set(modified_indices)
                    tracking_modes.clear()
                    tracked_chats_data = pc_state.get("tracked_chats", {})
                    explicit_state_indices = set()
                    for i in range(total):
                        ck = _chat_key_for_tracking(chats[i])
                        legacy_ck = f"{chats[i].get('_source', '')}:{chats[i].get('name', '')}"
                        td = tracked_chats_data.get(ck) or tracked_chats_data.get(legacy_ck)
                        if td:
                            if _removed_marker_is_active(td, chats[i], i in modified_set):
                                explicit_state_indices.add(i)
                                tracking_modes.pop(i, None)
                                continue
                            if td.get("removed", False):
                                # Stale removed marker (chat edited again) — ignore.
                                continue
                            explicit_state_indices.add(i)
                            tracking_modes[i] = td.get("mode", "tracked-checkpoint")
                            excl = td.get("excluded_fingerprints", [])
                            if excl:
                                excluded_fps[i] = set(excl)
                            fadd = td.get("force_add_fingerprints", [])
                            if fadd:
                                force_add_fps[i] = set(fadd)
                    for i in modified_indices:
                        if i not in explicit_state_indices:
                            tracking_modes[i] = "tracked-checkpoint"
                else:
                    _uninstall_hook()
                    tracking_modes.clear()
                    excluded_fps.clear()
                    force_add_fps.clear()
                    auto_skip_fps.clear()
            elif key == "o":
                old_enabled = not old_enabled
            elif key == "h":
                hot_only = not hot_only
                pc_state["hot_only"] = hot_only
                _save_precommit_state(pc_state)
            elif key == "\t":
                input_mode = True
                input_buf = compact_selection(selected)
            elif key == "enter":
                _save_tracking()
                desel_fps = _compute_deselected_fps(
                    selected, selected_entries, entry_cache, reset_mode)
                explicit_fps = _compute_explicit_select_fps(
                    selected, selected_entries, entry_cache, reset_mode)
                return (sorted(selected), desel_fps, old_enabled,
                        hot_only, explicit_fps)
            elif key == "esc":
                _save_tracking()
                return [], set(), old_enabled, hot_only, set()

            rows = _do_render()
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_source(name):
    """Try to import a source module and return (list_chats, export_single_chat).

    Returns None if the source is unavailable (e.g. no workspace found).
    """
    try:
        if name == "cursor":
            from chat_timeline._legacy.cursor import (
                list_chats, export_single_chat)
        elif name == "claude":
            from chat_timeline._legacy.claude import (
                list_chats, export_single_chat)
        elif name == "codex":
            from chat_timeline._legacy.codex import (
                list_chats, export_single_chat)
        else:
            return None
        return list_chats, export_single_chat
    except Exception:
        return None


def _collect_chats(source_names):
    """Collect chats from one or more sources.

    Returns (chats, exporters) where each chat has a ``_source`` field
    and exporters maps source name to its export_single_chat function.
    """
    all_chats = []
    exporters = {}

    for src in source_names:
        funcs = _load_source(src)
        if funcs is None:
            continue
        lc, esc = funcs
        try:
            chats = lc(PROJECT_DIR)
        except SystemExit:
            # Source not available (e.g. no workspace/dir found)
            continue
        for c in chats:
            c["_source"] = src
            if "composerId" in c and "composer_id" not in c:
                c["composer_id"] = c["composerId"]
        all_chats.extend(chats)
        exporters[src] = esc

    # Sort combined list newest-first
    all_chats.sort(key=lambda c: c.get("lastUpdatedAt", 0), reverse=True)
    return all_chats, exporters


def _run_standalone():
    """Pre-commit standalone mode: auto-select modified chats, full pipeline, git add results."""
    pc_state = _load_precommit_state()
    if not pc_state.get("enabled", False):
        return
    since_ts = pc_state.get("last_run_ts", 0)

    # First run: no baseline yet — record current time and skip
    if since_ts == 0:
        print("[pre-commit timeline] First run — setting baseline timestamp, skipping.")
        pc_state["last_run_ts"] = _time.time()
        _save_precommit_state(pc_state)
        return

    source_names = ["cursor", "claude", "codex"]
    chats, exporters = _collect_chats(source_names)

    if not chats or not exporters:
        print("[pre-commit timeline] No chats/sources found, skipping.")
        pc_state["last_run_ts"] = _time.time()
        _save_precommit_state(pc_state)
        return

    modified = _get_modified_chats(chats, since_ts)
    modified_set = set(modified)

    def _chat_has_used_hashes(chat):
        """Cheap check: does this chat already have checkpoint hashes?"""
        meta = {
            "composer_id": (
                chat.get("composer_id")
                or chat.get("_session_id")
                or chat.get("id", "")
            )
        }
        dummy = Path(sanitize_filename(chat.get("name", "unknown")))
        used_path = chat_used_state_path(meta, dummy)
        state = load_used_state(used_path)
        return bool(state.get("seen_entry_hashes"))

    # Build effective set:
    # - honor persisted explicit tracking state (tracked/removed)
    # - also include newly modified chats that have no explicit state
    #   (matches interactive selector behavior)
    tracked_chats_data = pc_state.get("tracked_chats", {})
    tracked = set()
    explicit_state_indices = set()
    all_excluded_fps = set()
    all_force_add_fps = set()
    all_clean_keys = set()
    stale_checkpoint_markers = {}
    for i in range(len(chats)):
        td = None
        for lookup_key in _chat_tracking_lookup_keys(chats[i]):
            if lookup_key in tracked_chats_data:
                td = tracked_chats_data.get(lookup_key) or {}
                break
        if td is not None:
            mode = td.get("mode", "tracked-checkpoint")
            removed_active = _removed_marker_is_active(
                td, chats[i], i in modified_set)
            if removed_active:
                explicit_state_indices.add(i)
                tracked.discard(i)
                continue
            if td.get("removed", False):
                # Stale removed marker (chat edited again) — ignore it.
                continue

            explicit_state_indices.add(i)
            is_removed = False
            has_overrides = bool(
                td.get("excluded_fingerprints")
                or td.get("force_add_fingerprints")
            )
            # Auto-prune stale checkpoint tracking (same intent as selector
            # cleanup) so pre-commit does not keep reprocessing fully consumed,
            # unmodified chats.
            if (
                (not is_removed)
                and mode == "tracked-checkpoint"
                and i not in modified_set
                and (not has_overrides)
                and _chat_has_used_hashes(chats[i])
            ):
                is_removed = True
                stale_checkpoint_markers[
                    _chat_key_for_tracking(chats[i])
                ] = _removed_marker_payload(chats[i])

            if is_removed:
                tracked.discard(i)
            else:
                tracked.add(i)
            for fp in td.get("excluded_fingerprints", []):
                all_excluded_fps.add(fp)
            for fp in td.get("force_add_fingerprints", []):
                all_force_add_fps.add(fp)
            if (not is_removed) and mode == "tracked":
                ck = _chat_key_for_tracking(chats[i])
                all_clean_keys.add(ck)

    for i in modified:
        if i not in explicit_state_indices:
            tracked.add(i)

    effective = sorted(tracked)
    if not effective:
        print("[pre-commit timeline] No effective tracked chats, skipping.")
        if tracked_chats_data:
            next_tracked = {}
            for ck, td in tracked_chats_data.items():
                if td.get("removed", False):
                    next_tracked[ck] = dict(td)
            next_tracked.update(stale_checkpoint_markers)
            pc_state["tracked_chats"] = next_tracked
        pc_state["last_run_ts"] = _time.time()
        _save_precommit_state(pc_state)
        return

    print(f"[pre-commit timeline] {len(effective)} effective tracked chat(s)")

    # Clear staged exports so this run reflects only the effective chat set.
    # This prevents stale exports from leaking extra entries into timeline/session.
    if STAGED_DIR.exists():
        for old in STAGED_DIR.glob("*.md"):
            old.unlink()
    STAGED_DIR.mkdir(parents=True, exist_ok=True)

    # Export tracked modified chats (overwrites same-named files from prior runs)
    for idx in effective:
        c = chats[idx]
        export_fn = exporters[c["_source"]]
        path = export_fn(c, include_tool_params=False)
        if path:
            print(f"  exported: {path.name}")

    # Session — skip rotation, rebuild in place
    print("[pre-commit timeline] Generating session...")
    generate_session()

    # Rotate BEFORE generating.
    #
    # On amend commits, keep the current open timeline in place and avoid
    # creating a new archived cycle for the same commit.
    if _is_amend_precommit():
        # If the commit being amended never touched timeline.md (e.g. the
        # original commit had no tracked chats and the hook returned early),
        # the existing timeline belongs to a prior cycle — rotate normally.
        diff_path = f"{HISTORY_DIR_NAME}/timeline.md"
        tl_diff, tl_rc = git_run(
            "diff-tree", "--no-commit-id", "-r", "HEAD", "--", diff_path)
        # Diagnostic dump — written every amend so we can debug a future
        # false-empty diff-tree (which silently rotates a healthy timeline).
        head_short, _ = git_run("rev-parse", "--short", "HEAD")
        print(f"[pre-commit timeline] amend diff-tree: rc={tl_rc}, "
              f"HEAD={head_short.strip()}, path={diff_path}, "
              f"stdout_len={len(tl_diff)}, "
              f"stdout_preview={tl_diff[:160]!r}")
        if tl_rc == 0 and tl_diff.strip():
            print("[pre-commit timeline] Amend detected; skipping rotation")
            rotated = False
        else:
            # HEAD will become a dangling ref after the amend.  Use HEAD~1
            # as archive key — the parent that would have been HEAD had
            # rotation occurred during the original (empty) commit.
            parent_short, _ = git_run("rev-parse", "--short", "HEAD~1")
            # Last-line guard: if HEAD's blob actually contains a tracked
            # timeline.md (verified by cat-file), the prior cycle clearly
            # touched it and we should NOT rotate, regardless of what
            # diff-tree just said.  This protects against the historical
            # false-empty failure where diff-tree returned nothing despite
            # a real blob delta.
            head_blob, head_blob_rc = git_run(
                "rev-parse", f"HEAD:{diff_path}")
            if head_blob_rc == 0 and head_blob.strip():
                print(f"[pre-commit timeline] Amend detected; diff-tree "
                      f"reported no timeline.md change but HEAD tracks "
                      f"the blob ({head_blob.strip()[:12]}). Skipping "
                      f"rotation as a safeguard.")
                rotated = False
            else:
                print("[pre-commit timeline] Amend detected; prior commit had no timeline changes, rotating...")
                rotated = rotate_timeline(
                    force=False,
                    archive_key_override=parent_short.strip() or None)
    else:
        print("[pre-commit timeline] Rotating timeline...")
        rotated = rotate_timeline(force=False)

    print("[pre-commit timeline] Generating timeline...")
    hot_only_setting = pc_state.get("hot_only", False)
    if hot_only_setting:
        print("[pre-commit timeline] hot-only ON — dropping turns without "
              "file changes")
    generate_timeline(reset_chats=False, reset_timeline=rotated,
                      excluded_fingerprints=all_excluded_fps,
                      force_add_fingerprints=all_force_add_fps or None,
                      clean_mode_chat_keys=all_clean_keys or None,
                      strict_dedup=True,
                      hot_only=hot_only_setting)

    # Don't move staged to archive in standalone mode

    # Add the generated timeline.  Rotated archives are already staged
    # by git mv inside rotate_timeline(); gitignored dirs (sessions/,
    # contents/, chats/) need no action.
    result = subprocess.run(
        ["git", "add", "--", f"{HISTORY_DIR_NAME}/timeline.md"],
        cwd=str(PROJECT_DIR),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        print(f"  warning: git add {HISTORY_DIR_NAME}/timeline.md failed"
              f" (rc={result.returncode}): {err}")
    else:
        print("[pre-commit timeline] Timeline added to commit")

    # Clear processed tracked flags after standalone run (so chats return
    # to untracked in the selector), but keep explicit-state mode via
    # removed markers when manual tracking data existed.
    if tracked_chats_data:
        next_tracked = {}
        for ck, td in tracked_chats_data.items():
            if td.get("removed", False):
                next_tracked[ck] = dict(td)
        next_tracked.update(stale_checkpoint_markers)
        for idx in effective:
            ck = _chat_key_for_tracking(chats[idx])
            next_tracked[ck] = _removed_marker_payload(chats[idx])
        pc_state["tracked_chats"] = next_tracked
    else:
        pc_state["tracked_chats"] = {}

    pc_state["last_run_ts"] = _time.time()
    _save_precommit_state(pc_state)
    print("[pre-commit timeline] Done.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Chat history exporter & session/timeline generator")
    parser.add_argument("source", nargs="?", default=None,
                        choices=["cursor", "claude", "codex"],
                        help="Data source (omit for all)")
    parser.add_argument("-c", "--chats", action="store_true",
                        help="Export chats to /timeline/chats/")
    parser.add_argument("-s", "--session", action="store_true",
                        help="Generate session.md")
    parser.add_argument("-t", "--timeline", action="store_true",
                        help="Generate timeline.md + contents/timeline.json")
    parser.add_argument("-r", "--reset", action="store_true",
                        help="Reset both chats used-state and timeline")
    parser.add_argument("-rc", "--reset-chats", action="store_true",
                        help="Reset chats used-state for currently staged chats")
    parser.add_argument("-rt", "--reset-timeline", action="store_true",
                        help="Reset timeline (rebuild instead of incremental append)")
    parser.add_argument("-p", "--standalone", action="store_true",
                        help="Pre-commit mode: auto-select modified chats, no TUI")
    parser.add_argument("-o", "--old", action="store_true",
                        help="Enable old timeline rotation in normal runs")
    parser.add_argument("--tool-params", action="store_true",
                        help="Include full tool call parameters")
    args, remaining = parser.parse_known_args()
    args.selection = remaining[0] if remaining else None

    # -r implies both -rc and -rt
    if args.reset:
        args.reset_chats = True
        args.reset_timeline = True

    # -p standalone mode: auto-detect modified chats, run full pipeline
    if args.standalone:
        _run_standalone()
        return

    # Determine which sources to use
    source_names = [args.source] if args.source else ["cursor", "claude", "codex"]

    run_all = not (args.chats or args.session or args.timeline)
    do_c = args.chats or run_all
    do_s = args.session or run_all
    do_t = args.timeline or run_all
    old_from_ui = False
    explicit_select_fps = set()

    # Clear staged directory before a fresh export
    if do_c and STAGED_DIR.exists():
        for old in STAGED_DIR.glob("*.md"):
            old.unlink()

    if do_c:
        chats, exporters = _collect_chats(source_names)
        multi_source = len(exporters) > 1

        if not chats:
            print("No chats found for this workspace.")
            return
        if not exporters:
            print("No sources available.")
            return

        # When showing multiple sources, put source name in the mode column
        if multi_source:
            for c in chats:
                c["unifiedMode"] = c["_source"][:6]

        deselected_fps = set()
        if args.selection:
            selected = parse_selection(args.selection, len(chats))
        else:
            is_reset = getattr(args, 'reset_chats', False)
            (selected, deselected_fps, old_from_ui,
             _hot_unused, explicit_select_fps) = interactive_select(
                chats, exporters, reset_mode=is_reset)
        if not selected:
            print("Nothing selected.")
            return

        print(f"\nExporting {len(selected)} chat(s) to {STAGED_DIR}/")
        for idx in selected:
            c = chats[idx]
            export_fn = exporters[c["_source"]]
            path = export_fn(c, include_tool_params=args.tool_params)
            if path:
                print(f"  [{idx+1}] {path.name}")
            else:
                print(f"  [{idx+1}] FAILED: {c.get('name', '(unnamed)')}")
        print(f"Done. {len(selected)} chat(s) staged.")

    if do_s:
        print("\nRotating old session...")
        rotate_session()
        print("Generating session from staged chats...")
        out_path, size = generate_session()
        print(f"  Wrote {out_path} ({size:,} chars)")

    if do_t:
        old_enabled_for_run = bool(args.old or old_from_ui)
        if old_enabled_for_run:
            print("\nRotating timeline...")
            # When old mode is enabled, manual runs that exported chats in this
            # invocation start a fresh timeline file rather than appending.
            rotated = rotate_timeline(force=do_c)
        else:
            print("\nOld timeline rotation disabled (use 'o' in UI or -o/--old).")
            rotated = False
        # Normal manual export cycles (do_c=True) should still generate a new
        # open timeline even when old rotation is disabled.
        effective_reset_timeline = args.reset_timeline or rotated or do_c
        print("Generating timeline from staged chats...")
        flags = []
        if args.reset_chats:
            flags.append("reset-chats")
        if effective_reset_timeline:
            flags.append("reset-timeline")
        if flags:
            print(f"  reset mode: {', '.join(flags)}")
        # Collect excluded and force-add fingerprints from:
        # 1. Precommit tracking state
        tl_pc_state = _load_precommit_state()
        tl_excluded = set()
        tl_force_add = set()
        tl_clean_keys = set()
        for ck, td in tl_pc_state.get("tracked_chats", {}).items():
            for fp in td.get("excluded_fingerprints", []):
                tl_excluded.add(fp)
            for fp in td.get("force_add_fingerprints", []):
                tl_force_add.add(fp)
            if td.get("mode") == "tracked":
                tl_clean_keys.add(ck)
        # 2. Entry-level selection cherry-picks (deselected entries)
        if do_c and deselected_fps:
            tl_excluded.update(deselected_fps)
        hot_only_setting = tl_pc_state.get("hot_only", False)
        result = generate_timeline(
            reset_chats=args.reset_chats,
            reset_timeline=effective_reset_timeline,
            excluded_fingerprints=tl_excluded or None,
            force_add_fingerprints=tl_force_add or None,
            clean_mode_chat_keys=tl_clean_keys or None,
            hot_only=hot_only_setting,
            explicit_select_fingerprints=explicit_select_fps or None)
        if hot_only_setting:
            print("  hot-only: dropping turns without file changes "
                  "(force-add and Space cherry-picks still pass through)")
        if result[0]:
            print(f"  Wrote {result[0]} ({result[2]:,} chars)")
            print(f"  Wrote {result[1]}")

        # Clear tracked chats — their entries have been processed
        tl_pc_state["tracked_chats"] = {}
        tl_pc_state["last_run_ts"] = _time.time()
        _save_precommit_state(tl_pc_state)

    # Move staged chats to /chats/ archive
    if STAGED_DIR.exists():
        staged_files = list(STAGED_DIR.glob("*.md"))
        if staged_files:
            CHATS_DIR.mkdir(parents=True, exist_ok=True)
            print(f"\nMoving {len(staged_files)} chat(s) from staged to chats/")
            for f in staged_files:
                dest = CHATS_DIR / f.name
                shutil.move(str(f), str(dest))
            try:
                STAGED_DIR.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    main()
