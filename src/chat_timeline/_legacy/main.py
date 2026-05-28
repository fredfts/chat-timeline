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

    Sources that accept ``cache_dir`` get one under
    ``<HISTORY_DIR>/.cache/sessions`` so warm scans skip re-parsing
    matched sessions. Sources that don't accept it (legacy shims) keep
    working unchanged.
    """
    import inspect

    cache_root = HISTORY_DIR / ".cache" / "sessions"

    all_chats = []
    exporters = {}

    for src in source_names:
        funcs = _load_source(src)
        if funcs is None:
            continue
        lc, esc = funcs
        try:
            # Only pass cache_dir if the source's list_chats accepts it.
            kwargs = {}
            try:
                sig = inspect.signature(lc)
                if "cache_dir" in sig.parameters:
                    kwargs["cache_dir"] = cache_root
            except (TypeError, ValueError):
                pass
            chats = lc(PROJECT_DIR, **kwargs)
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


# ---------------------------------------------------------------------------
# Phase 5 re-exports — the bodies of these functions now live in
# chat_timeline.{precommit, session, timeline}. Imported at the bottom so
# the path globals above are defined by the time the new modules load
# (they read STAGED_DIR, PROJECT_DIR, etc. from this module).
# ---------------------------------------------------------------------------

from chat_timeline.precommit import (  # noqa: E402, F401
    _get_modified_chats,
    _install_hook,
    _is_amend_precommit,
    _load_precommit_state,
    _run_standalone,
    _save_precommit_state,
    _uninstall_hook,
    _win32_ancestor_has_amend,
)
from chat_timeline.session import (  # noqa: E402, F401
    generate_session,
    rotate_session,
)
from chat_timeline.timeline import (  # noqa: E402, F401
    _ARCHIVED_EID_PLACEHOLDER,
    _build_timeline_entry_payload,
    _collect_archive_dedup_data,
    _compute_per_chat_stats,
    _compute_timeline_stats,
    _entry_id_num,
    _FILE_CHANGE_MARKERS,
    _format_turn_numbers,
    _is_file_change_tool,
    _next_archive_candidate,
    _normalize_identity_value,
    _read_frontmatter_field,
    _read_timeline_json_parent,
    _render_timeline_md,
    _stamp_commit_field,
    _turn_has_file_changes,
    chat_used_state_path,
    entry_fingerprint,
    entry_identity_from_entry,
    entry_identity_from_turn,
    generate_timeline,
    get_chat_entries,
    load_used_state,
    rotate_timeline,
    save_used_state,
    solidify_used_state,
)
