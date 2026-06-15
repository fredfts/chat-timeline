"""Top-level orchestration — argparse, source dispatch, generators wiring.

This module is the post-``init``/``deinit`` body of the CLI: build a
chat list across sources, prompt the user (or auto-pick), then run any
combination of chat-export / session / timeline / precommit.

Lifted from ``_legacy/main.py`` in Phase 6 alongside the deletion of
``_legacy/``.
"""

from __future__ import annotations

import argparse
import inspect
import shutil
import sys
import time as _time

from chat_timeline._state import (
    CHATS_DIR,
    HISTORY_DIR,
    PROJECT_DIR,
    STAGED_DIR,
)
from chat_timeline.markdown import parse_selection
from chat_timeline.precommit import (
    _load_precommit_state,
    _run_standalone,
    _save_precommit_state,
)
from chat_timeline.session import generate_session, rotate_session
from chat_timeline.timeline import generate_timeline, rotate_timeline
from chat_timeline.tui.selector import interactive_select


def _load_source(name: str):
    """Import a source module and return (list_chats, export_single_chat).

    Returns None if the source is unavailable (e.g. no workspace found).
    """
    try:
        if name == "cursor":
            from chat_timeline.sources.cursor import export_single_chat, list_chats
        elif name == "claude":
            from chat_timeline.sources.claude import export_single_chat, list_chats
        elif name == "codex":
            from chat_timeline.sources.codex import export_single_chat, list_chats
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
    ``<HISTORY_DIR>/.cache/sessions`` so warm scans skip re-parsing.
    """
    cache_root = HISTORY_DIR / ".cache" / "sessions"

    all_chats = []
    exporters = {}

    for src in source_names:
        funcs = _load_source(src)
        if funcs is None:
            continue
        lc, esc = funcs
        try:
            kwargs = {}
            try:
                sig = inspect.signature(lc)
                if "cache_dir" in sig.parameters:
                    kwargs["cache_dir"] = cache_root
            except (TypeError, ValueError):
                pass
            chats = lc(PROJECT_DIR, **kwargs)
        except SystemExit:
            continue
        for c in chats:
            c["_source"] = src
            if "composerId" in c and "composer_id" not in c:
                c["composer_id"] = c["composerId"]
        all_chats.extend(chats)
        exporters[src] = esc

    all_chats.sort(key=lambda c: c.get("lastUpdatedAt", 0), reverse=True)
    return all_chats, exporters


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Chat history exporter & session/timeline generator"
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=None,
        choices=["cursor", "claude", "codex"],
        help="Data source (omit for all)",
    )
    parser.add_argument(
        "-c", "--chats", action="store_true", help="Export chats to /timeline/chats/"
    )
    parser.add_argument("-s", "--session", action="store_true", help="Generate session.md")
    parser.add_argument(
        "-t",
        "--timeline",
        action="store_true",
        help="Generate timeline.md + contents/timeline.json",
    )
    parser.add_argument(
        "-r", "--reset", action="store_true", help="Reset both chats used-state and timeline"
    )
    parser.add_argument(
        "-rc",
        "--reset-chats",
        action="store_true",
        help="Reset chats used-state for currently staged chats",
    )
    parser.add_argument(
        "-rt",
        "--reset-timeline",
        action="store_true",
        help="Reset timeline (rebuild instead of incremental append)",
    )
    parser.add_argument(
        "-p",
        "--standalone",
        action="store_true",
        help="Pre-commit mode: auto-select modified chats, no TUI",
    )
    parser.add_argument(
        "-o", "--old", action="store_true", help="Enable old timeline rotation in normal runs"
    )
    parser.add_argument(
        "--tool-params", action="store_true", help="Include full tool call parameters"
    )
    args, remaining = parser.parse_known_args()
    args.selection = remaining[0] if remaining else None

    if args.reset:
        args.reset_chats = True
        args.reset_timeline = True

    if args.standalone:
        _run_standalone()
        return

    source_names = [args.source] if args.source else ["cursor", "claude", "codex"]

    run_all = not (args.chats or args.session or args.timeline)
    do_c = args.chats or run_all
    do_s = args.session or run_all
    do_t = args.timeline or run_all
    old_from_ui = False
    explicit_select_fps: set = set()

    if do_c and STAGED_DIR.exists():
        for old in STAGED_DIR.glob("*.md"):
            old.unlink()

    deselected_fps: set = set()
    if do_c:
        chats, exporters = _collect_chats(source_names)
        multi_source = len(exporters) > 1

        if not chats:
            print("No chats found for this workspace.")
            return
        if not exporters:
            print("No sources available.")
            return

        if multi_source:
            for c in chats:
                c["unifiedMode"] = c["_source"][:6]

        if args.selection:
            selected = parse_selection(args.selection, len(chats))
        else:
            is_reset = getattr(args, "reset_chats", False)
            (
                selected,
                deselected_fps,
                old_from_ui,
                _hot_unused,
                explicit_select_fps,
            ) = interactive_select(chats, exporters, reset_mode=is_reset)
        if not selected:
            print("Nothing selected.")
            return

        print(f"\nExporting {len(selected)} chat(s) to {STAGED_DIR}/")
        for idx in selected:
            c = chats[idx]
            export_fn = exporters[c["_source"]]
            path = export_fn(c, include_tool_params=args.tool_params)
            if path:
                print(f"  [{idx + 1}] {path.name}")
            else:
                print(f"  [{idx + 1}] FAILED: {c.get('name', '(unnamed)')}")
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
            rotated = rotate_timeline(force=do_c)
        else:
            print("\nOld timeline rotation disabled (use 'o' in UI or -o/--old).")
            rotated = False
        effective_reset_timeline = args.reset_timeline or rotated or do_c
        print("Generating timeline from staged chats...")
        flags = []
        if args.reset_chats:
            flags.append("reset-chats")
        if effective_reset_timeline:
            flags.append("reset-timeline")
        if flags:
            print(f"  reset mode: {', '.join(flags)}")

        tl_pc_state = _load_precommit_state()
        tl_excluded: set = set()
        tl_force_add: set = set()
        tl_clean_keys: set = set()
        for ck, td in tl_pc_state.get("tracked_chats", {}).items():
            for fp in td.get("excluded_fingerprints", []):
                tl_excluded.add(fp)
            for fp in td.get("force_add_fingerprints", []):
                tl_force_add.add(fp)
            if td.get("mode") == "tracked":
                tl_clean_keys.add(ck)
        if do_c and deselected_fps:
            tl_excluded.update(deselected_fps)
        hot_mode_setting = tl_pc_state.get("hot_mode", "off")
        result = generate_timeline(
            reset_chats=args.reset_chats,
            reset_timeline=effective_reset_timeline,
            excluded_fingerprints=tl_excluded or None,
            force_add_fingerprints=tl_force_add or None,
            clean_mode_chat_keys=tl_clean_keys or None,
            hot_mode=hot_mode_setting,
            explicit_select_fingerprints=explicit_select_fps or None,
        )
        if hot_mode_setting == "entry":
            print(
                "  hot=entry: dropping turns without file changes "
                "(force-add and Space cherry-picks still pass through)"
            )
        elif hot_mode_setting == "chat":
            print(
                "  hot=chat: dropping chats with no file-changing turn "
                "(force-add and Space cherry-picks still pass through)"
            )
        if result[0]:
            print(f"  Wrote {result[0]} ({result[2]:,} chars)")
            print(f"  Wrote {result[1]}")

        tl_pc_state["tracked_chats"] = {}
        tl_pc_state["last_run_ts"] = _time.time()
        _save_precommit_state(tl_pc_state)

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
