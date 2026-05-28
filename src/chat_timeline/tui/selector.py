"""Interactive chat selector — the meat of ``timeline`` without ``-c``/``-t``/``-s`` flags.

Lifted from ``_legacy/main.py`` wholesale in Phase 4. The functions still
reach into ``_legacy.main`` for path globals (PRECOMMIT_STATE, STAGED_DIR,
CONTENTS_DIR) and pipeline helpers (``get_chat_entries``,
``_collect_archive_dedup_data``, ``_install_hook`` …). Phase 5 will
introduce the ``Paths`` dataclass and lift those out too.
"""

from __future__ import annotations

import os
import shutil
import sys
import time as _time
from pathlib import Path

from chat_timeline.markdown import epoch_ms_to_dt, fmt_dt, sanitize_filename
from chat_timeline.tui.keyboard import (
    HOLD_SECONDS,
    check_hold_with_feedback,
    read_key,
)

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
                rows.append({"type": "entry", "chat_idx": i, "entry_idx": j, "entry": e})
    return rows


def _render(
    chats,
    rows,
    cursor,
    window_start,
    selected,
    selected_entries,
    input_mode,
    input_buf,
    precommit_on,
    tracking_modes,
    excluded_fps,
    force_add_fps,
    expanded,
    auto_skip_fps,
    reset_mode=False,
    hold_key=None,
    hold_elapsed=0.0,
    old_enabled=False,
    hot_only=False,
):
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
    pc_label = f" | pre-commit ON ({len(tracking_modes)} tracked)" if precommit_on else ""
    reset_label = " | -r mode" if reset_mode else ""
    old_label = " | rotate ON" if old_enabled else " | rotate off"
    hot_label = " | hot ON" if hot_only else " | hot off"
    lines.append(
        f"  selected {len(selected)}/{total}{pc_label}{reset_label}{old_label}{hot_label}"
    )

    # Show current row info
    cur_row = rows[cursor] if cursor < total_rows else None
    if cur_row and cur_row["type"] == "entry":
        cur_entry = cur_row["entry"]
        entry_label = cur_entry.get("number_label", f"Q{cur_entry.get('number', '?')}")
        entry_count = cur_entry.get("count", 1)
        count_suffix = f" (x{entry_count})" if entry_count > 1 else ""
        lines.append(
            f"  row {cursor + 1}/{total_rows}"
            f" | entry {entry_label}{count_suffix}"
            f" of chat #{cur_row['chat_idx'] + 1}"
        )
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
                f" {arrow} {check} {i + 1:>4}  {fmt_dt(dt):<20} "
                f"{mode_name:<6}{trk} {exp} {name[:name_w]}"
            )
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
                is_auto_skip = (
                    fp in auto_skip_fps.get(ci, set())
                    if e_mode == "tracked-checkpoint"
                    else False
                )
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

            prompt = e["prompt"][: max(10, name_w - 10)]
            q_label = e.get("number_label", f"Q{e['number']}")
            if e.get("count", 1) > 1:
                q_label = f"{q_label} x{e['count']}"
            lines.append(
                f" {arrow}     {echeck} {q_label:<10} "
                f"{e['timestamp']:<20}{trk_status:>5}  {prompt}"
            )

    lines.append("")
    if input_mode:
        lines.append(f"  input: {input_buf}_")
        lines.append("  Type ranges like 1-3,7 | Tab/Enter apply | Esc discard")
    else:
        lines.append(f"  selection: {sel_str}")
        help_parts = [
            "Up/Down",
            "Space sel",
            "Right expand",
            "Left collapse",
            "Tab input",
            "a all",
            "Enter ok",
            "Esc cancel",
            "p pre-commit",
            "t track (●/◆)",
            "o rotate old",
            "h hot entries only",
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
    return {
        e["fingerprint"] for i, e in enumerate(entries) if i <= last_used_idx and not e["is_used"]
    }


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
    """
    # Pipeline + state helpers still live in _legacy.main for now; defer
    # their import so picking up tui.selector doesn't freeze legacy
    # module-level paths (see sources/cursor.py for the same pattern).
    from chat_timeline._legacy.main import (
        _collect_archive_dedup_data,
        _get_modified_chats,
        _install_hook,
        _load_precommit_state,
        _save_precommit_state,
        _uninstall_hook,
        chat_used_state_path,
        get_chat_entries,
        load_used_state,
    )

    total = len(chats)
    if total == 0:
        return [], set(), False, False, set()

    os.system("")
    cursor = 0
    window_start = 0
    selected: set[int] = set()  # set of chat indices
    selected_entries: dict[int, set[int]] = {}
    input_mode = False
    input_buf = ""
    old_enabled = False

    # Expansion state
    expanded: set[int] = set()
    entry_cache: dict[int, list] = {}

    # Pre-commit state
    pc_state = _load_precommit_state()
    precommit_on = pc_state.get("enabled", False)
    hot_only = pc_state.get("hot_only", False)
    since_ts = pc_state.get("last_run_ts", 0)
    modified_indices = (
        _get_modified_chats(chats, since_ts) if precommit_on and since_ts > 0 else []
    )

    tracked_chats_data = pc_state.get("tracked_chats", {})
    tracking_modes: dict[int, str] = {}
    excluded_fps: dict[int, set] = {}
    force_add_fps: dict[int, set] = {}
    auto_skip_fps: dict[int, set] = {}

    try:
        archive_fps, archive_idents = _collect_archive_dedup_data(include_open=True)
    except Exception:
        archive_fps, archive_idents = set(), set()

    if precommit_on:
        explicit_state_indices: set[int] = set()
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
        if ci not in entry_cache and exporters:
            entries, _ = get_chat_entries(
                chats[ci],
                exporters,
                archive_fingerprints=archive_fps,
                archive_identities=archive_idents,
            )
            entry_cache[ci] = entries
            if tracking_modes.get(ci) == "tracked-checkpoint":
                auto_skip_fps[ci] = _auto_skip_fps_for_entries(entries)

    def _chat_has_checkpoint_data(ci):
        if ci in entry_cache:
            return any(e["is_used"] for e in entry_cache[ci])
        c = chats[ci]
        meta = {
            "composer_id": (c.get("composer_id") or c.get("_session_id") or c.get("id", ""))
        }
        dummy = Path(sanitize_filename(c.get("name", "unknown")))
        used_path = chat_used_state_path(meta, dummy)
        state = load_used_state(used_path)
        return bool(state.get("seen_entry_hashes"))

    def _prune_stale_checkpoint_tracking():
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

    def _select_chat_all_entries(ci):
        selected.add(ci)
        if ci in entry_cache:
            selected_entries[ci] = set(range(len(entry_cache[ci])))
        else:
            selected_entries.pop(ci, None)

    def _deselect_chat(ci):
        selected.discard(ci)
        selected_entries.pop(ci, None)

    def _do_render(hold_key=None, hold_elapsed=0.0):
        nonlocal window_start
        rows = _build_row_map(chats, expanded, entry_cache)
        window_start = _render(
            chats,
            rows,
            cursor,
            window_start,
            selected,
            selected_entries,
            input_mode,
            input_buf,
            precommit_on,
            tracking_modes,
            excluded_fps,
            force_add_fps,
            expanded,
            auto_skip_fps,
            reset_mode,
            hold_key,
            hold_elapsed,
            old_enabled,
            hot_only,
        )
        return rows

    _prune_stale_checkpoint_tracking()

    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    try:
        rows = _do_render()

        while True:
            key = read_key()
            if not key:
                continue

            rows = _build_row_map(chats, expanded, entry_cache)
            total_rows = len(rows)

            if input_mode:
                if key == "enter" or key == "\t":
                    selected = parse_selection_string(input_buf, total)
                    for ci in selected:
                        if ci in entry_cache:
                            selected_entries[ci] = set(range(len(entry_cache[ci])))
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
                held = check_hold_with_feedback(
                    "t",
                    lambda elapsed: _do_render(hold_key="t", hold_elapsed=elapsed),
                )

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
                        if (
                            tracking_modes.get(ci) == "tracked-checkpoint"
                            and ci in entry_cache
                        ):
                            auto_skip_fps[ci] = _auto_skip_fps_for_entries(entry_cache[ci])
                        else:
                            auto_skip_fps.pop(ci, None)
                        _save_tracking()
                    elif (
                        cur_row
                        and cur_row["type"] == "entry"
                        and cur_row["chat_idx"] in tracking_modes
                    ):
                        ci = cur_row["chat_idx"]
                        e_mode = tracking_modes[ci]
                        fp = cur_row["entry"]["fingerprint"]
                        is_used = (
                            cur_row["entry"]["is_used"]
                            if e_mode == "tracked-checkpoint"
                            else False
                        )
                        is_auto_skip = (
                            fp in auto_skip_fps.get(ci, set())
                            if e_mode == "tracked-checkpoint"
                            else False
                        )
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
                        if ci in selected and ci in entry_cache:
                            if ci not in selected_entries:
                                selected_entries[ci] = set(range(len(entry_cache[ci])))
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
                    ci = cur_row["chat_idx"]
                    ei = cur_row["entry_idx"]
                    if ci in selected:
                        if ci in selected_entries:
                            if ei in selected_entries[ci]:
                                selected_entries[ci].discard(ei)
                                if not selected_entries[ci]:
                                    _deselect_chat(ci)
                                    input_buf = compact_selection(selected)
                            else:
                                selected_entries[ci].add(ei)
                        else:
                            if ci in entry_cache:
                                all_ei = set(range(len(entry_cache[ci])))
                                all_ei.discard(ei)
                                if not all_ei:
                                    _deselect_chat(ci)
                                    input_buf = compact_selection(selected)
                                else:
                                    selected_entries[ci] = all_ei
                    else:
                        selected.add(ci)
                        selected_entries[ci] = {ei}
                        input_buf = compact_selection(selected)
            elif key == "a":
                if len(selected) == total:
                    selected.clear()
                    selected_entries.clear()
                else:
                    selected = set(range(total))
                    for ci in selected:
                        if ci in entry_cache:
                            selected_entries[ci] = set(range(len(entry_cache[ci])))
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
                    selected, selected_entries, entry_cache, reset_mode
                )
                explicit_fps = _compute_explicit_select_fps(
                    selected, selected_entries, entry_cache, reset_mode
                )
                return (sorted(selected), desel_fps, old_enabled, hot_only, explicit_fps)
            elif key == "esc":
                _save_tracking()
                return [], set(), old_enabled, hot_only, set()

            rows = _do_render()
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
