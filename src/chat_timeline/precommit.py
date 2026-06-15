"""Pre-commit hook installer + standalone runner.

Path globals (``PRECOMMIT_STATE``, ``STAGED_DIR``, ``PROJECT_DIR``,
``HISTORY_DIR_NAME``) come from ``chat_timeline._state``, resolved once at
process start. The pre-commit hook path is resolved fresh per call via
``_resolve_hook_path`` instead, so install/uninstall stay correct when the
cwd or ``TIMELINE_PROJECT_ROOT`` changed after import (e.g. ``timeline init``,
or the test suite).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time as _time
from pathlib import Path

from chat_timeline._state import (
    HISTORY_DIR_NAME,
    PRECOMMIT_STATE,
    PROJECT_DIR,
    STAGED_DIR,
)
from chat_timeline.git_utils import git_run as _git_run
from chat_timeline.markdown import sanitize_filename
from chat_timeline.paths import find_project_root


def git_run(*args, cwd=None):
    """Legacy wrapper bound to PROJECT_DIR (mirrors _legacy.main.git_run)."""
    return _git_run(*args, cwd=Path(cwd) if cwd else PROJECT_DIR)


# Tri-state "hot" filter, cycled by 'h' in the selector and persisted as
# ``hot_mode``:
#   off   — emit every turn
#   chat  — keep a whole chat iff at least one of its turns touched a file
#   entry — keep only the individual turns that touched a file
HOT_MODES = ("off", "chat", "entry")


def next_hot_mode(mode):
    """Next value in the off -> chat -> entry -> off cycle."""
    try:
        return HOT_MODES[(HOT_MODES.index(mode) + 1) % len(HOT_MODES)]
    except ValueError:
        return "chat"


def _load_precommit_state():
    """Load the pre-commit state file.

    Returns dict with 'enabled', 'last_run_ts', 'tracked_chats', and
    'hot_mode'. tracked_chats maps chat key -> {"excluded_fingerprints": [...]}.

    The legacy boolean ``hot_only`` is migrated to ``hot_mode`` on load
    (True -> "entry", False -> "off") and dropped, so callers only ever see
    the tri-state key.
    """
    default = {
        "enabled": False,
        "last_run_ts": 0,
        "tracked_chats": {},
        "hot_mode": "off",
    }
    if not PRECOMMIT_STATE.exists():
        return default
    try:
        data = json.loads(PRECOMMIT_STATE.read_text(encoding="utf-8"))
        data.setdefault("tracked_chats", {})
        state = {**default, **data}
        if "hot_mode" not in data:
            state["hot_mode"] = "entry" if data.get("hot_only") else "off"
        if state["hot_mode"] not in HOT_MODES:
            state["hot_mode"] = "off"
        state.pop("hot_only", None)
        return state
    except Exception:
        return default


def _save_precommit_state(state):
    """Save the pre-commit state file."""
    PRECOMMIT_STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


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
                f"wmic process where ProcessId={pid} get CommandLine,ParentProcessId /format:list",
                shell=True,
                text=True,
                timeout=3,
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


def _resolve_hook_path():
    """Resolve ``<project>/.git/hooks/pre-commit`` at call time.

    ``_state.HOOK_PATH`` is frozen at module-import time from whatever cwd/env
    was active then. Resolving the project root fresh here keeps install and
    uninstall pointed at the right repo when the cwd or ``TIMELINE_PROJECT_ROOT``
    changed afterwards — e.g. ``timeline init`` sets the env right before
    installing, and the test suite stands up throwaway repos.
    """
    return find_project_root() / ".git" / "hooks" / "pre-commit"


def _install_hook(hook_path=None):
    """Install the git pre-commit hook.

    The hook prefers the installed ``timeline`` console script, falls back
    to ``python -m chat_timeline`` (or WSL on Windows hosts). ``hook_path``
    defaults to the freshly-resolved ``<project>/.git/hooks/pre-commit``.
    """
    hook_path = hook_path or _resolve_hook_path()
    hooks_dir = hook_path.parent
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_body = (
        'TOPLEVEL="$(git rev-parse --show-toplevel)"\n'
        'cd "$TOPLEVEL" || exit 0\n'
        "\n"
        "# Detect --amend from parent process command line\n"
        'if [ -r "/proc/$PPID/cmdline" ]; then\n'
        "  case \"$(tr '\\0' ' ' < /proc/$PPID/cmdline)\" in\n"
        "    *--amend*) export TIMELINE_AMEND=1 ;;\n"
        "  esac\n"
        "fi\n"
        "\n"
        "if command -v timeline >/dev/null 2>&1; then\n"
        "  timeline -p\n"
        "elif command -v python3 >/dev/null 2>&1; then\n"
        "  python3 -m chat_timeline -p\n"
        "elif command -v python >/dev/null 2>&1; then\n"
        "  python -m chat_timeline -p\n"
        "elif command -v wsl.exe >/dev/null 2>&1; then\n"
        "  wsl.exe timeline -p\n"
        "else\n"
        '  echo "pre-commit: chat-timeline not on PATH, skipping hook"\n'
        "fi\n"
    )

    legacy_script_rel = f"{HISTORY_DIR_NAME}/main.py"
    installed_markers = (
        "timeline -p",  # new entry point
        "python -m chat_timeline -p",  # new module form
        "python3 -m chat_timeline -p",
        f"{legacy_script_rel} -p",  # legacy variants
        f"{legacy_script_rel} -x",
        "history/main.py -x",
        "timeline/main.py -x",
        "timeline/main.py -p",
    )

    # If a hook already exists, check if it's ours (current or legacy)
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if any(marker in content for marker in installed_markers):
            return  # already installed (current or legacy variant)
        # Append to existing hook
        hook_path.write_text(
            content.rstrip("\n") + "\n\n"
            "# --- timeline pre-commit ---\n" + hook_body + "# --- end timeline pre-commit ---\n",
            encoding="utf-8",
        )
        print("  pre-commit: appended timeline hook to existing hook")
        return

    hook_path.write_text(
        "#!/bin/sh\n"
        "# chat-timeline pre-commit hook — works in WSL, Git Bash, "
        "and POSIX shells\n" + hook_body,
        encoding="utf-8",
    )
    hook_path.chmod(0o755)
    print("  pre-commit: hook installed")


def _uninstall_hook(hook_path=None):
    """Remove the timeline pre-commit hook."""
    hook_path = hook_path or _resolve_hook_path()
    if not hook_path.exists():
        return
    content = hook_path.read_text(encoding="utf-8", errors="replace")
    # Strip current + legacy section markers from any appended-block install.
    new_content = content
    for marker_open, marker_close in (
        ("# --- timeline pre-commit ---", "# --- end timeline pre-commit ---"),
        ("# --- history pre-commit ---", "# --- end history pre-commit ---"),
    ):
        if marker_open in new_content:
            new_content = re.sub(
                rf"\n*{re.escape(marker_open)}\n.*?{re.escape(marker_close)}\n?",
                "",
                new_content,
                flags=re.DOTALL,
            )

    # Standalone-hook detection on the post-strip content. Legacy hooks split
    # `SCRIPT="$TOPLEVEL/timeline/main.py"` from `python3 "$SCRIPT" -x` across
    # lines, so a literal `timeline/main.py -x` never appears — match the path
    # and the flag independently, and also recognise the header comments.
    has_flag = any(flag in new_content for flag in (" -p\n", " -x\n", ' -p"', ' -x"'))
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
        hook_path.unlink()
        print("  pre-commit: hook removed")
        return

    if new_content != content:
        if new_content.strip():
            hook_path.write_text(new_content, encoding="utf-8")
            print("  pre-commit: removed timeline hook (other hooks preserved)")
        else:
            hook_path.unlink()
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


def _run_standalone():
    """Pre-commit standalone mode: auto-select modified chats, full pipeline, git add results."""
    # Deferred imports: each of these triggers loading another sibling
    # module. Resolving at call time keeps module-load order simple and
    # mirrors the pattern used by sources/ and tui/.
    from chat_timeline.app import _collect_chats
    from chat_timeline.session import generate_session
    from chat_timeline.timeline import (
        chat_used_state_path,
        generate_timeline,
        load_used_state,
        rotate_timeline,
    )
    from chat_timeline.tui.selector import (
        _chat_key_for_tracking,
        _chat_tracking_lookup_keys,
        _removed_marker_is_active,
        _removed_marker_payload,
    )

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
                chat.get("composer_id") or chat.get("_session_id") or chat.get("id", "")
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
            removed_active = _removed_marker_is_active(td, chats[i], i in modified_set)
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
                td.get("excluded_fingerprints") or td.get("force_add_fingerprints")
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
                stale_checkpoint_markers[_chat_key_for_tracking(chats[i])] = (
                    _removed_marker_payload(chats[i])
                )

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
        tl_diff, tl_rc = git_run("diff-tree", "--no-commit-id", "-r", "HEAD", "--", diff_path)
        # Diagnostic dump — written every amend so we can debug a future
        # false-empty diff-tree (which silently rotates a healthy timeline).
        head_short, _ = git_run("rev-parse", "--short", "HEAD")
        print(
            f"[pre-commit timeline] amend diff-tree: rc={tl_rc}, "
            f"HEAD={head_short.strip()}, path={diff_path}, "
            f"stdout_len={len(tl_diff)}, "
            f"stdout_preview={tl_diff[:160]!r}"
        )
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
            head_blob, head_blob_rc = git_run("rev-parse", f"HEAD:{diff_path}")
            if head_blob_rc == 0 and head_blob.strip():
                print(
                    f"[pre-commit timeline] Amend detected; diff-tree "
                    f"reported no timeline.md change but HEAD tracks "
                    f"the blob ({head_blob.strip()[:12]}). Skipping "
                    f"rotation as a safeguard."
                )
                rotated = False
            else:
                print(
                    "[pre-commit timeline] Amend detected; prior commit had no "
                    "timeline changes, rotating..."
                )
                rotated = rotate_timeline(
                    force=False, archive_key_override=parent_short.strip() or None
                )
    else:
        print("[pre-commit timeline] Rotating timeline...")
        rotated = rotate_timeline(force=False)

    print("[pre-commit timeline] Generating timeline...")
    hot_mode_setting = pc_state.get("hot_mode", "off")
    if hot_mode_setting == "entry":
        print("[pre-commit timeline] hot=entry — dropping turns without file changes")
    elif hot_mode_setting == "chat":
        print("[pre-commit timeline] hot=chat — dropping chats with no file-changing turn")
    generate_timeline(
        reset_chats=False,
        reset_timeline=rotated,
        excluded_fingerprints=all_excluded_fps,
        force_add_fingerprints=all_force_add_fps or None,
        clean_mode_chat_keys=all_clean_keys or None,
        strict_dedup=True,
        hot_mode=hot_mode_setting,
    )

    # Don't move staged to archive in standalone mode

    # Add the generated timeline.  Rotated archives are already staged
    # by git mv inside rotate_timeline(); gitignored dirs (sessions/,
    # contents/, chats/) need no action.
    result = subprocess.run(
        ["git", "add", "--", f"{HISTORY_DIR_NAME}/timeline.md"],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        print(
            f"  warning: git add {HISTORY_DIR_NAME}/timeline.md failed"
            f" (rc={result.returncode}): {err}"
        )
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
