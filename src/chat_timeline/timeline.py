"""Timeline generation + rotation + dedup state.

Extracted from ``_legacy/main.py`` in Phase 5. The on-disk format (chats/used,
contents/timeline.json, timeline.md) is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from chat_timeline._state import (
    CONTENTS_DIR,
    HISTORY_DIR,
    HISTORY_DIR_NAME,
    PROJECT_DIR,
    STAGED_DIR,
    TIMELINE_DIR,
    USED_DIR,
)
from chat_timeline.git_utils import (
    get_current_branch as _gu_get_current_branch,
)
from chat_timeline.git_utils import (
    get_head_message as _gu_get_head_message,
)
from chat_timeline.git_utils import (
    get_head_short as _gu_get_head_short,
)
from chat_timeline.git_utils import (
    git_mv as _gu_git_mv,
)
from chat_timeline.git_utils import (
    git_run as _gu_git_run,
)
from chat_timeline.markdown import (
    fmt_dt,
    parse_chat_export,
    sanitize_filename,
    strip_redacted,
)


def get_head_short():
    return _gu_get_head_short(PROJECT_DIR)


def get_head_message():
    return _gu_get_head_message(PROJECT_DIR)


def get_current_branch():
    return _gu_get_current_branch(PROJECT_DIR)


def git_run(*args, cwd=None):
    return _gu_git_run(*args, cwd=Path(cwd) if cwd else PROJECT_DIR)


def _git_mv(src: Path, dst: Path):
    return _gu_git_mv(src, dst, cwd=PROJECT_DIR)


# generate_session is invoked from generate_timeline; deferred to avoid
# a circular import at module load (timeline imports session imports timeline).
def generate_session(*args, **kwargs):
    from chat_timeline.session import generate_session as _gs

    return _gs(*args, **kwargs)


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
                json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except Exception:
            pass
        return

    content = path.read_text(encoding="utf-8", errors="replace")
    fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm:
        return
    fm_text = fm.group(1)
    # Insert commit field after the parent_commit line (or at end of frontmatter)
    if re.search(r"^commit:", fm_text, re.MULTILINE):
        return  # already has commit field
    insert_after = re.search(r"^parent_commit:.*$", fm_text, re.MULTILINE)
    if insert_after:
        pos = insert_after.end()
        new_fm = fm_text[:pos] + f'\ncommit: "{commit_short}"' + fm_text[pos:]
    else:
        new_fm = fm_text + f'\ncommit: "{commit_short}"'
    content = f"---\n{new_fm}\n---{content[fm.end() :]}"
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
    _read_frontmatter_field(tl, "parent_commit") if tl.exists() else None
    _read_timeline_json_parent(js)

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

    # Guard: skip only when the COMMITTED timeline says it's already on HEAD.
    # If committed parent is missing/unreadable, prefer rotating instead of
    # risking an unintended append onto the current timeline.
    if (not force) and committed_tl_parent and committed_tl_parent == commit_short:
        print(
            f"  HEAD ({commit_short}) unchanged since committed timeline "
            "generation, skipping rotation"
        )
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
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


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
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
    "edit_file",
    "Edit",
    "Write",
    "apply_patch",
    "write_file",
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
        content_items.append(
            dict(
                id=th_id,
                type="thinking",
                entry=eid,
                chat=chat_name,
                turn=turn.get("number", 0),
                block=i,
                timestamp=blk.get("timestamp", ""),
                duration_s=duration_s,
                text=blk_text,
                word_count=len(blk_text.split()),
            )
        )

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
        content_items.append(
            dict(
                id=tc_ref,
                type="tool_calls",
                entry=eid,
                chat=chat_name,
                turn=turn.get("number", 0),
                count=len(turn.get("tool_calls", [])),
                summary=tool_summary,
                calls=[
                    dict(
                        name=tc.get("detail", "").split(":", 1)[0].strip(),
                        detail=tc.get("detail", ""),
                        status=tc.get("status", ""),
                        timestamp=tc.get("timestamp", ""),
                    )
                    for tc in turn.get("tool_calls", [])
                ],
                edits=edits_list,
                reads=reads_list,
            )
        )

    resp_text = strip_redacted((turn.get("response_text") or "").strip())
    resp_ref = None
    resp_words = len(resp_text.split()) if resp_text else 0
    if resp_text:
        resp_ref = f"R-{eid}"
        content_items.append(
            dict(
                id=resp_ref,
                type="response",
                entry=eid,
                chat=chat_name,
                turn=turn.get("number", 0),
                model=turn.get("response_model", ""),
                timestamp=turn.get("response_timestamp", ""),
                text=resp_text,
                word_count=resp_words,
            )
        )

    entry = dict(
        id=eid,
        timestamp=turn.get("user_timestamp", ""),
        chat=chat_name,
        turn=turn.get("number", 0),
        model=turn.get("user_model", ""),
        prompt=prompt,
        fingerprint=turn_hash,
        thinking_refs=th_refs,
        thinking_count=len(turn.get("thinking_blocks", [])),
        thinking_s=th_total_s,
        tc_ref=tc_ref,
        tool_count=len(turn.get("tool_calls", [])),
        tool_summary=tool_summary,
        edits=edits_list,
        reads=reads_list,
        resp_ref=resp_ref,
        resp_model=turn.get("response_model", ""),
        resp_words=resp_words,
    )

    return entry, content_items


def get_chat_entries(chat, exporters, archive_fingerprints=None, archive_identities=None):
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
            if (
                not is_used
                and archive_identities
                and identity is not None
                and identity in archive_identities
            ):
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
                entry.get("numbers", [entry.get("number", 0)])
            )

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
        thinking_s=sum(c.get("duration_s", 0) for c in content if c["type"] == "thinking"),
        tool_calls=sum(c.get("count", 0) for c in content if c["type"] == "tool_calls"),
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
            chat_map[cn] = dict(
                name=cn, prompts=0, thinking=0, thinking_s=0.0, tools=0, edits=0, words=0
            )
        cs = chat_map[cn]
        cs["prompts"] += 1
        cs["thinking"] += e.get("thinking_count", 0)
        cs["thinking_s"] += e.get("thinking_s", 0)
        cs["tools"] += e.get("tool_count", 0)
        cs["edits"] += len(e.get("edits", []))
        cs["words"] += e.get("resp_words", 0)
    return list(chat_map.values())


def _render_timeline_md(all_entries, all_content, stats, chat_stats, commit_short, branch, now_iso):
    """Render timeline.md from merged data."""
    lines = []

    lines.append("---")
    lines.append("type: timeline")
    lines.append(f'parent_commit: "{commit_short}"')
    lines.append(f'branch: "{branch}"')
    lines.append(f'generated: "{now_iso}"')
    lines.append(f"chats: {stats['chats']}")
    lines.append(f"entries: {stats['entries']}")
    lines.append(f"content_items: {len(all_content)}")
    lines.append("---")
    lines.append("")

    lines.append("# Timeline")
    lines.append("")

    lines.append("## Stats")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Chats | {stats['chats']} |")
    lines.append(f"| Entries | {stats['entries']} |")
    lines.append(f"| Thinking blocks | {stats['thinking_blocks']} ({stats['thinking_s']:.0f}s) |")
    lines.append(f"| Tool calls | {stats['tool_calls']} |")
    lines.append(f"| Files edited | {len(stats['edits'])} |")
    lines.append(f"| Files read | {len(stats['reads'])} |")
    lines.append(f"| Response words | {stats['response_words']:,} |")
    lines.append(f"| Models | {', '.join(sorted(stats['models']))} |")
    lines.append("")

    if chat_stats:
        lines.append("**Per chat:**")
        lines.append("")
        lines.append("| Chat | Entries | Thinking | Tools | Edits | Words |")
        lines.append("|---|---|---|---|---|---|")
        for cs in chat_stats:
            lines.append(
                f"| {cs['name'][:60]} | {cs['prompts']}"
                f" | {cs['thinking']} ({cs['thinking_s']:.0f}s)"
                f" | {cs['tools']} | {cs['edits']}"
                f" | {cs['words']:,} |"
            )
        lines.append("")

    lines.append("## Entries")
    lines.append("")

    if not all_entries:
        lines.append("*No entries.*")
        lines.append("")
    else:
        for entry in all_entries:
            lines.append(
                f"**{entry['id']}** [{entry['timestamp']}] Q{entry['turn']} — {entry['chat']}"
            )
            lines.append("")
            lines.append(f"Model: {entry['model']}")
            lines.append("")

            if entry["prompt"]:
                for pline in entry["prompt"].split("\n"):
                    if pline.strip():
                        lines.append(f"> {pline}")
                lines.append("")

            if entry["thinking_count"] > 0:
                refs = ", ".join(entry["thinking_refs"])
                lines.append(
                    f"- Thinking: {entry['thinking_count']} blocks,"
                    f" {entry['thinking_s']:.1f}s [{refs}]"
                )
            if entry["tool_count"] > 0:
                parts = [f"{n} x{c}" for n, c in entry["tool_summary"].items()]
                lines.append(
                    f"- Tools ({entry['tool_count']}): {', '.join(parts)} [{entry['tc_ref']}]"
                )
            if entry["edits"]:
                files = ", ".join(f"`{f}`" for f in entry["edits"])
                lines.append(f"- Edits: {files}")
            if entry["reads"]:
                flist = entry["reads"][:5]
                files = ", ".join(f"`{f}`" for f in flist)
                if len(entry["reads"]) > 5:
                    files += f" +{len(entry['reads']) - 5} more"
                lines.append(f"- Reads: {files}")
            if entry["resp_ref"]:
                lines.append(f"- Response: {entry['resp_words']:,} words [{entry['resp_ref']}]")

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


def generate_timeline(
    reset_chats=False,
    reset_timeline=False,
    excluded_fingerprints=None,
    force_add_fingerprints=None,
    clean_mode_chat_keys=None,
    strict_dedup=False,
    hot_mode="off",
    explicit_select_fingerprints=None,
):
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
    hot_mode:       tri-state "hot" filter over turns that did not touch any
                    file (no Edit/Write/apply_patch tool call):
                      "off"   — emit every turn.
                      "entry" — skip individual cold turns.
                      "chat"  — skip a whole chat unless at least one of its
                                turns touched a file (then all of its turns
                                pass the filter).
                    Cold turns still bypass dedup/watermark for this turn, so
                    they can surface in a later "off" run. Combine with
                    reset_timeline to get a pure hot rebuild. Force-add ('t')
                    and explicit Space cherry-pick override this filter — the
                    user said "include this one".
    explicit_select_fingerprints: per-entry Space cherry-picks from the TUI.
                    Treated as an explicit "include" signal that bypasses
                    hot_mode (in addition to is_forced).
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
                e for e in existing_entries if (not e.get("id")) or (e.get("id") in keep_ids)
            ]
            existing_content = [
                c for c in existing_content if (not c.get("entry")) or (c.get("entry") in keep_ids)
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
            existing_identity_to_eid.setdefault(ident, _ARCHIVED_EID_PLACEHOLDER)
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

        # hot=chat: a chat is "hot" iff at least one of its turns touched a
        # file. When it isn't, every turn is cold; when it is, no turn is.
        chat_is_hot = hot_mode == "chat" and any(_turn_has_file_changes(t) for t in turns)

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
                    th
                    for i, th in enumerate(turn_hashes)
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
                            refreshed_entry, refreshed_content = _build_timeline_entry_payload(
                                existing_eid, chat_name, turn, turn_hash
                            )
                            existing_entries[existing_idx] = refreshed_entry
                            existing_content = [
                                c for c in existing_content if c.get("entry") != existing_eid
                            ]
                            existing_content.extend(refreshed_content)
                            if existing_fp:
                                if not any(
                                    e.get("fingerprint") == existing_fp for e in existing_entries
                                ):
                                    existing_fingerprints.discard(existing_fp)
                            existing_fingerprints.add(turn_hash)
                    last_added_idx = max(last_added_idx, ti)
                    continue

            # Layer 1: within-timeline dedup (includes entries added this run)
            if turn_hash in existing_fingerprints:
                last_added_idx = max(last_added_idx, ti)
                continue

            is_forced = force_add_fingerprints and turn_hash in force_add_fingerprints

            # Layer 2: cross-timeline dedup (solidified used-state)
            # Skipped on fresh cycle, clean-mode chats, or forced entries.
            if (
                (not reset_chats)
                and (not is_clean)
                and (not fresh_cycle)
                and turn_hash in seen_hashes
                and not is_forced
            ):
                continue

            # Layer 3: checkpoint auto-skip
            if (
                checkpoint_auto_skip_hashes
                and turn_hash in checkpoint_auto_skip_hashes
                and not is_forced
            ):
                continue

            # Layer 4: user-excluded entries (from interactive tracking)
            if excluded_fingerprints and turn_hash in excluded_fingerprints:
                continue

            # Hot filter: drop cold turns unless the user explicitly opted
            # them in via 't' force-add or Space cherry-pick. "entry" judges
            # each turn on its own file changes; "chat" judges the whole chat
            # (computed once as chat_is_hot above). Do NOT advance
            # last_added_idx or existing_fingerprints here, so the turn stays
            # eligible for a later "off" run (the watermark may still mark it
            # seen if a later hot turn lands in this chat).
            is_explicit_pick = bool(
                explicit_select_fingerprints and turn_hash in explicit_select_fingerprints
            )
            if hot_mode != "off" and not is_forced and not is_explicit_pick:
                if hot_mode == "chat":
                    is_cold = not chat_is_hot
                else:  # "entry"
                    is_cold = not _turn_has_file_changes(turn)
                if is_cold:
                    continue

            existing_fingerprints.add(turn_hash)
            last_added_idx = max(last_added_idx, ti)

            eid = f"E{entry_num:04d}"
            entry_num += 1
            entry_payload, content_payload = _build_timeline_entry_payload(
                eid, chat_name, turn, turn_hash
            )
            new_entries.append(entry_payload)
            new_content.extend(content_payload)
            if turn_identity is not None:
                existing_identity_to_eid[turn_identity] = eid

        # Save pending used-state: watermark approach — save all hashes
        # up to the last processed timeline-aligned entry so earlier entries
        # don't reappear, while entries beyond the watermark remain available
        # for future runs.
        if last_added_idx >= 0:
            watermark_hashes = set(all_turn_hashes[: last_added_idx + 1])
            updated_hashes = seen_hashes | watermark_hashes
        else:
            # No entries added this run — keep existing baseline
            updated_hashes = set(seen_hashes)

        save_used_state(
            used_path,
            dict(
                version=1,
                composer_id=meta.get("composer_id", ""),
                chat_title=chat_name,
                source_file=fp.name,
                updated_at=now_iso,
                seen_entry_hashes=sorted(updated_hashes),
            ),
        )

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
        all_entries, all_content, stats, chat_stats, commit_short, branch, now_iso
    )

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    tl_path = HISTORY_DIR / "timeline.md"
    tl_path.write_text(tl_md, encoding="utf-8")

    # Write timeline.json (with entries for incremental append)
    content_json = dict(
        metadata=dict(
            generated=now_iso,
            parent_commit=commit_short,
            entries=len(all_entries),
            content_items=len(all_content),
        ),
        entries=all_entries,
        content=all_content,
    )
    CONTENTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(content_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    added = len(new_entries)
    if added:
        print(f"  Added {added} new entries (total: {len(all_entries)})")
    elif existing_entries:
        print(f"  No new entries (total: {len(all_entries)})")

    return tl_path, json_path, len(tl_md)


# ---------------------------------------------------------------------------
# Interactive UI — extracted to chat_timeline.tui in v0.2.0.
# The legacy module keeps re-exports so internal callers still work.
# ---------------------------------------------------------------------------

from chat_timeline.tui.keyboard import (  # noqa: F401, E402
    HOLD_SECONDS,
)
from chat_timeline.tui.selector import (  # noqa: F401, E402
    PAGE_SIZE,
    compact_selection,
    interactive_select,
    parse_selection_string,
)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
