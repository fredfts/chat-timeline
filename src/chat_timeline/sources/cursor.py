"""Cursor source — reads chat data from Cursor's SQLite stores."""

from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import unquote

from chat_timeline.markdown import (
    epoch_ms_to_dt,
    fmt_dt_filename,
    iso_to_dt,
    sanitize_filename,
)
from chat_timeline.markdown import export_chat_markdown as _md_export_chat_markdown

SOURCE_NAME = "Cursor"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
AICHAT_VIEW_ID_RE = re.compile(r"workbench\.panel\.aichat\.view\.([0-9a-fA-F-]{36})")
EDITOR_COMPOSER_ID_RE = re.compile(r'"composerId":"([0-9a-fA-F-]{36})"')
EDITOR_COMPOSER_ID_ESCAPED_RE = re.compile(r'\\"composerId\\":\\"([0-9a-fA-F-]{36})\\"')
_CURSOR_STORAGE_ROOTS_CACHE = None


# ---------------------------------------------------------------------------
# Cursor storage paths
# ---------------------------------------------------------------------------


def cursor_storage_roots():
    """Return all Cursor user-data roots visible from this runtime."""
    global _CURSOR_STORAGE_ROOTS_CACHE
    if _CURSOR_STORAGE_ROOTS_CACHE is not None:
        return list(_CURSOR_STORAGE_ROOTS_CACHE)

    roots = []
    seen = set()

    def _add(path: Path):
        try:
            key = str(path.resolve()).lower()
        except Exception:
            key = str(path).lower()
        if key in seen:
            return
        if path.exists():
            roots.append(path)
            seen.add(key)

    # Native locations
    appdata = os.environ.get("APPDATA")
    if appdata:
        _add(Path(appdata) / "Cursor" / "User")

    home = Path.home()
    _add(home / "Library" / "Application Support" / "Cursor" / "User")
    _add(home / ".config" / "Cursor" / "User")

    # Cross-runtime fallbacks
    if platform.system() == "Windows":
        # Read WSL Cursor data from Windows (default distro).
        try:
            proc = subprocess.run(
                ["wsl.exe", "wslpath", "-w", "~/.config/Cursor/User"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if proc.returncode == 0:
                raw = proc.stdout.strip().strip('"')
                if raw:
                    _add(Path(raw))
        except Exception:
            pass
    else:
        # Read Windows Cursor data from WSL/Linux via mounted drive.
        users_root = Path("/mnt/c/Users")
        if users_root.exists():
            for user_dir in users_root.iterdir():
                if user_dir.is_dir():
                    _add(user_dir / "AppData" / "Roaming" / "Cursor" / "User")

    if roots:
        _CURSOR_STORAGE_ROOTS_CACHE = tuple(roots)
        return list(_CURSOR_STORAGE_ROOTS_CACHE)
    raise SystemExit("Cursor user data directory not found")


def _project_uri_candidates(project_dir: Path):
    """Return normalized file:// URI candidates for both Windows and WSL path forms."""
    resolved = project_dir.resolve().as_posix()
    path_candidates = {resolved.rstrip("/")}

    # Windows -> WSL: C:/x/y -> /mnt/c/x/y
    if len(resolved) >= 3 and resolved[1] == ":" and resolved[2] == "/":
        drive = resolved[0].lower()
        tail = resolved[3:].lstrip("/")
        wsl_path = f"/mnt/{drive}/{tail}" if tail else f"/mnt/{drive}"
        path_candidates.add(wsl_path.rstrip("/"))

    # WSL -> Windows: /mnt/c/x/y -> C:/x/y
    if resolved.startswith("/mnt/") and len(resolved) > 7:
        drive = resolved[5]
        if drive.isalpha() and resolved[6] == "/":
            tail = resolved[7:].lstrip("/")
            win_path = f"{drive.upper()}:/{tail}" if tail else f"{drive.upper()}:/"
            path_candidates.add(win_path.rstrip("/"))

    uris = set()
    for p in path_candidates:
        encoded = p.replace(":", "%3A")
        if not encoded.startswith("/"):
            encoded = "/" + encoded
        uri = f"file:///{encoded.lstrip('/')}"
        uris.add(unquote(uri).rstrip("/").lower())
    return uris


def find_workspace_hashes(storage_root: Path, project_dir: Path):
    """Return workspaceStorage hashes under storage_root for project_dir."""
    ws_root = storage_root / "workspaceStorage"
    if not ws_root.exists():
        return []

    targets = _project_uri_candidates(project_dir)
    hashes = []
    for d in ws_root.iterdir():
        wj = d / "workspace.json"
        if not wj.exists():
            continue
        try:
            folder = json.loads(wj.read_text()).get("folder", "")
            if unquote(folder).rstrip("/").lower() in targets:
                hashes.append(d.name)
        except Exception:
            pass
    return hashes


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def open_db(path):
    return sqlite3.connect(str(path))


def db_get(conn, table, key):
    try:
        cur = conn.execute(f"SELECT value FROM {table} WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return None
        val = row[0]
        if isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        return val
    except Exception:
        return None


def db_get_json(conn, table, key):
    raw = db_get(conn, table, key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def db_get_prefix(conn, table, prefix):
    """Return all (key, parsed_json) for keys starting with prefix.

    Uses a range comparison instead of LIKE so the query hits the PK index
    regardless of SQLite's case_sensitive_like pragma (OFF by default would
    force a full table scan — painful on the bubble store, which can be
    hundreds of MB).
    """
    # U+FFFF is larger than any character likely to appear in a Cursor
    # storage key, so [prefix, prefix + "￿") brackets the prefix range.
    cur = conn.execute(
        f"SELECT key, value FROM {table} WHERE key >= ? AND key < ?",
        (prefix, prefix + "￿"),
    )
    results = {}
    for k, v in cur.fetchall():
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="replace")
        if v:
            try:
                results[k] = json.loads(v)
            except Exception:
                results[k] = v
    return results


def _safe_epoch_ms(value):
    """Best-effort conversion to integer epoch milliseconds."""
    try:
        return int(value or 0)
    except Exception:
        return 0


def _open_global_cursor_db(storage_root: Path):
    """Open Cursor globalStorage DB with cross-runtime compatibility."""
    gl_db = storage_root / "globalStorage" / "state.vscdb"
    if not gl_db.exists():
        return None

    # WSL reading mounted Windows Cursor DB often needs immutable mode
    # to avoid sqlite "disk I/O error" on globalStorage/state.vscdb.
    if platform.system() != "Windows" and gl_db.as_posix().startswith("/mnt/"):
        return sqlite3.connect(
            f"file:{gl_db.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
    return open_db(gl_db)


def _collect_workspace_composer_refs(ws_conn):
    """Extract composer references from legacy and migrated workspace state."""
    composer_ids = set()
    legacy_composers = []

    cd = db_get_json(ws_conn, "ItemTable", "composer.composerData")
    if isinstance(cd, dict):
        # Legacy format: metadata already listed in allComposers
        all_composers = cd.get("allComposers", [])
        if isinstance(all_composers, list):
            for c in all_composers:
                if not isinstance(c, dict):
                    continue
                legacy_composers.append(c)
                cid = c.get("composerId")
                if isinstance(cid, str) and UUID_RE.match(cid):
                    composer_ids.add(cid.lower())

        # Migrated format: currently opened/recent IDs
        for key in ("selectedComposerIds", "lastFocusedComposerIds"):
            ids = cd.get(key, [])
            if isinstance(ids, list):
                for cid in ids:
                    if isinstance(cid, str) and UUID_RE.match(cid):
                        composer_ids.add(cid.lower())

    # Migrated format: pane state maps aichat view IDs (which are composer IDs).
    pane_rows = db_get_prefix(ws_conn, "ItemTable", "workbench.panel.composerChatViewPane.")
    for val in pane_rows.values():
        if isinstance(val, (dict, list)):
            text = json.dumps(val, ensure_ascii=False, default=str)
        else:
            text = str(val or "")
        for cid in AICHAT_VIEW_ID_RE.findall(text):
            if UUID_RE.match(cid):
                composer_ids.add(cid.lower())

    # Migrated format: embedded editor state stores serialized composerId values.
    editor_state = db_get(ws_conn, "ItemTable", "workbench.parts.embeddedAuxBarEditor.state")
    if editor_state:
        for regex in (EDITOR_COMPOSER_ID_RE, EDITOR_COMPOSER_ID_ESCAPED_RE):
            for cid in regex.findall(editor_state):
                if UUID_RE.match(cid):
                    composer_ids.add(cid.lower())

    return composer_ids, legacy_composers


def _load_global_composer_meta(global_conn, composer_id):
    """Load compact composer metadata from global cursorDiskKV."""
    cd = db_get_json(global_conn, "cursorDiskKV", f"composerData:{composer_id}")
    if not isinstance(cd, dict):
        return None

    is_nal_shell = (
        cd.get("isNAL") and not cd.get("name") and not cd.get("fullConversationHeadersOnly")
    )
    if is_nal_shell:
        return None

    cid = cd.get("composerId") or composer_id
    return {
        "composerId": cid,
        "name": cd.get("name", "(unnamed)"),
        "unifiedMode": cd.get("unifiedMode", "?"),
        "createdAt": _safe_epoch_ms(cd.get("createdAt")),
        "lastUpdatedAt": _safe_epoch_ms(cd.get("lastUpdatedAt") or cd.get("createdAt")),
        "status": cd.get("status", ""),
        "agentBackend": cd.get("agentBackend", ""),
    }


def _load_global_composer_headers(global_conn, workspace_hashes):
    """Load NAL chat entries from composer.composerHeaders in global ItemTable.

    Returns list of metadata dicts for chats belonging to the given workspace
    hashes, skipping drafts and empty shells.
    """
    raw = db_get_json(global_conn, "ItemTable", "composer.composerHeaders")
    if not isinstance(raw, dict):
        return []

    all_composers = raw.get("allComposers", [])
    if not isinstance(all_composers, list):
        return []

    ws_set = {h.lower() for h in workspace_hashes}
    results = []
    for c in all_composers:
        if not isinstance(c, dict):
            continue
        if c.get("isDraft"):
            continue
        name = c.get("name")
        if not name:
            continue

        ws_id = (c.get("workspaceIdentifier") or {}).get("id", "")
        if ws_id.lower() not in ws_set:
            continue

        cid = c.get("composerId")
        if not isinstance(cid, str) or not UUID_RE.match(cid):
            continue

        results.append(
            {
                "composerId": cid,
                "name": name,
                "unifiedMode": c.get("unifiedMode", "?"),
                "createdAt": _safe_epoch_ms(c.get("createdAt")),
                "lastUpdatedAt": _safe_epoch_ms(c.get("lastUpdatedAt") or c.get("createdAt")),
                "status": c.get("status", ""),
                "agentBackend": c.get("agentBackend", ""),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Rich-text extraction (Cursor's Lexical JSON format)
# ---------------------------------------------------------------------------


def extract_rich_text(rt):
    if not rt:
        return ""
    try:
        data = json.loads(rt) if isinstance(rt, str) else rt

        def walk(node):
            parts = []
            if isinstance(node, dict):
                if "text" in node and isinstance(node["text"], str):
                    parts.append(node["text"])
                for child in node.get("children", []):
                    parts.extend(walk(child))
            return parts

        return "".join(walk(data))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Chat listing
# ---------------------------------------------------------------------------


def list_chats(project_dir: Path, scope: Path | None = None):
    """Return list of chat metadata dicts, sorted newest-first.

    ``scope`` narrows the workspace-hash match without affecting where exports
    land. Defaults to ``project_dir``.
    """
    match_dir = scope if scope is not None else project_dir

    all_composers = []
    for root in cursor_storage_roots():
        hashes = find_workspace_hashes(root, match_dir)
        if not hashes:
            continue

        global_conn = None
        global_cache: dict = {}
        try:
            global_conn = _open_global_cursor_db(root)
        except Exception:
            global_conn = None

        ws_root = root / "workspaceStorage"
        try:
            for h in hashes:
                ws_db = ws_root / h / "state.vscdb"
                if not ws_db.exists():
                    continue

                conn = open_db(ws_db)
                try:
                    composer_ids, legacy_rows = _collect_workspace_composer_refs(conn)
                finally:
                    conn.close()

                # Keep legacy rows when present (pre-migration format).
                for row in legacy_rows:
                    if not isinstance(row, dict):
                        continue
                    c = dict(row)
                    cid = c.get("composerId")
                    if not isinstance(cid, str) or not cid:
                        continue
                    c["createdAt"] = _safe_epoch_ms(c.get("createdAt"))
                    c["lastUpdatedAt"] = _safe_epoch_ms(
                        c.get("lastUpdatedAt") or c.get("createdAt")
                    )
                    c.setdefault("name", c.get("name", "(unnamed)"))
                    c.setdefault("unifiedMode", c.get("unifiedMode", "?"))
                    c["_ws_hash"] = h
                    c["_cursor_root"] = str(root)
                    all_composers.append(c)

                # Migrated format: resolve composer IDs from global cursorDiskKV.
                if global_conn is None:
                    continue
                for cid in composer_ids:
                    if cid in global_cache:
                        meta = global_cache[cid]
                    else:
                        meta = _load_global_composer_meta(global_conn, cid)
                        global_cache[cid] = meta
                    if not meta:
                        continue
                    c = dict(meta)
                    c["_ws_hash"] = h
                    c["_cursor_root"] = str(root)
                    all_composers.append(c)

            # NAL (New Agent Layout): composer.composerHeaders in the global
            # ItemTable is the authoritative list for the new agents workflow.
            if global_conn is not None:
                for c in _load_global_composer_headers(global_conn, hashes):
                    c["_ws_hash"] = hashes[0]
                    c["_cursor_root"] = str(root)
                    all_composers.append(c)
        finally:
            if global_conn is not None:
                global_conn.close()

    if not all_composers:
        raise SystemExit(f"No Cursor workspace found for {match_dir}")

    # Deduplicate by composerId, keeping the version with most recent lastUpdatedAt
    seen: dict[str, dict] = {}
    for c in all_composers:
        cid = c.get("composerId")
        if not cid:
            continue
        cur_ts = _safe_epoch_ms(c.get("lastUpdatedAt") or c.get("createdAt"))
        prev_ts = _safe_epoch_ms(seen.get(cid, {}).get("lastUpdatedAt"))
        if cid not in seen or cur_ts > prev_ts:
            seen[cid] = c
    deduped = list(seen.values())
    deduped.sort(
        key=lambda c: _safe_epoch_ms(c.get("lastUpdatedAt") or c.get("createdAt")),
        reverse=True,
    )

    # Standardize keys for the interactive selector
    for c in deduped:
        c.setdefault("name", c.get("name", "(unnamed)"))
        c.setdefault("unifiedMode", c.get("unifiedMode", "?"))
    return deduped


# ---------------------------------------------------------------------------
# Chat loading & conversation building
# ---------------------------------------------------------------------------


def load_full_chat(storage_root: Path, composer_id: str):
    """Load composerData + all bubbles from the global DB."""
    conn = None
    try:
        conn = _open_global_cursor_db(storage_root)
        if conn is None:
            return None, {}

        cd = db_get_json(conn, "cursorDiskKV", f"composerData:{composer_id}")
        if not cd:
            return None, {}

        bubble_rows = db_get_prefix(conn, "cursorDiskKV", f"bubbleId:{composer_id}:")
        bubble_map = {}
        for k, v in bubble_rows.items():
            bid = k.split(":")[-1]
            if isinstance(v, dict):
                bubble_map[bid] = v
        return cd, bubble_map
    finally:
        if conn is not None:
            conn.close()


def build_conversation(cd, bubble_map):
    """Parse composerData + bubbles into structured Q&A turns."""
    headers = cd.get("fullConversationHeadersOnly", [])
    model_config = cd.get("modelConfig", {})
    default_model = model_config.get("modelName", "unknown")

    turns = []
    current = None

    for header in headers:
        bid = header["bubbleId"]
        btype = header["type"]
        bubble = bubble_map.get(bid, {})

        if btype == 1:
            if current:
                turns.append(current)
            current = {
                "user_text": bubble.get("text", "") or extract_rich_text(bubble.get("richText")),
                "user_timestamp": iso_to_dt(bubble.get("createdAt")),
                "user_model": (bubble.get("modelInfo") or {}).get("modelName", "") or default_model,
                "assistant_parts": [],
                "tool_calls": [],
                "thinking_blocks": [],
                "checkpoints": [],
            }
            cp = bubble.get("checkpointId")
            if cp:
                current["checkpoints"].append(cp)
        elif btype == 2 and current is not None:
            cap_type = bubble.get("capabilityType")
            text = bubble.get("text", "") or extract_rich_text(bubble.get("richText"))

            if cap_type == 30 and bubble.get("thinking"):
                thinking = bubble.get("thinking", {})
                current["thinking_blocks"].append(
                    {
                        "duration_ms": bubble.get("thinkingDurationMs", 0),
                        "text": thinking.get("text", ""),
                        "timestamp": iso_to_dt(bubble.get("createdAt")),
                    }
                )
            elif cap_type == 15 and bubble.get("toolFormerData"):
                tfd = bubble.get("toolFormerData", {})
                raw_params = tfd.get("params", {})
                if isinstance(raw_params, str):
                    try:
                        raw_params = json.loads(raw_params)
                    except Exception:
                        raw_params = {"_raw": raw_params[:500]}
                tc = {
                    "name": tfd.get("name", "unknown"),
                    "status": tfd.get("status", ""),
                    "timestamp": iso_to_dt(bubble.get("createdAt")),
                    "params": raw_params if isinstance(raw_params, dict) else {},
                    "raw_args": tfd.get("rawArgs", ""),
                }
                current["tool_calls"].append(tc)
            elif text:
                model = (bubble.get("modelInfo") or {}).get("modelName", "") or default_model
                current["assistant_parts"].append(
                    {
                        "text": text,
                        "timestamp": iso_to_dt(bubble.get("createdAt")),
                        "model": model,
                    }
                )

            cp = bubble.get("checkpointId")
            if cp:
                current["checkpoints"].append(cp)

    if current:
        turns.append(current)
    return turns


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_single_chat(chat, include_tool_params=False):
    """Export one Cursor chat to STAGED_DIR and return the file path."""
    from chat_timeline._state import PROJECT_DIR, STAGED_DIR

    def export_chat_markdown(meta, turns, include_tool_params=False):
        return _md_export_chat_markdown(meta, turns, include_tool_params, project_root=PROJECT_DIR)

    composer_id = chat["composerId"]
    name = chat.get("name", "(unnamed)")
    created_dt = epoch_ms_to_dt(chat.get("createdAt"))
    root_raw = chat.get("_cursor_root", "")
    storage_root = Path(root_raw) if root_raw else None
    candidate_roots = []
    if storage_root is not None and storage_root.exists():
        candidate_roots.append(storage_root)
    try:
        for root in cursor_storage_roots():
            if all(str(root).lower() != str(r).lower() for r in candidate_roots):
                candidate_roots.append(root)
    except SystemExit:
        pass
    if not candidate_roots:
        return None

    cd = None
    bubble_map = {}
    for root in candidate_roots:
        cd, bubble_map = load_full_chat(root, composer_id)
        if cd:
            break
    if not cd:
        return None

    turns = build_conversation(cd, bubble_map)

    # Use the first user message timestamp if available, else composerData.createdAt
    first_user_ts = None
    if turns and turns[0].get("user_timestamp"):
        first_user_ts = turns[0]["user_timestamp"]
    effective_dt = first_user_ts or created_dt or epoch_ms_to_dt(cd.get("createdAt"))

    # Build standardized metadata
    model_config = cd.get("modelConfig", {})
    files_affected = []
    for uri in cd.get("originalFileStates", {}).keys():
        path = unquote(uri).replace("file:///c:", "C:").replace("file:///", "")
        files_affected.append(path)

    meta = {
        "name": cd.get("name", "(unnamed)"),
        "id": cd.get("composerId", ""),
        "created": epoch_ms_to_dt(cd.get("createdAt")),
        "last_updated": epoch_ms_to_dt(cd.get("lastUpdatedAt")),
        "status": cd.get("status", ""),
        "mode": cd.get("unifiedMode", ""),
        "model": model_config.get("modelName", "unknown"),
        "max_mode": model_config.get("maxMode", False),
        "agent_backend": cd.get("agentBackend", ""),
        "branch": cd.get("createdOnBranch", ""),
        "context_tokens": cd.get("contextTokensUsed", 0),
        "context_limit": cd.get("contextTokenLimit", 0),
        "lines_added": cd.get("totalLinesAdded", 0),
        "lines_removed": cd.get("totalLinesRemoved", 0),
        "files_changed": cd.get("filesChangedCount", 0),
        "is_agentic": cd.get("isAgentic", False),
        "files_affected": files_affected,
        "source": SOURCE_NAME,
    }

    md = export_chat_markdown(meta, turns, include_tool_params=include_tool_params)

    safe_name = sanitize_filename(name or "(unnamed)")
    ts_str = fmt_dt_filename(effective_dt)

    filename = f"{ts_str}_{SOURCE_NAME}_{safe_name}.md"
    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = STAGED_DIR / filename

    out_path.write_text(md, encoding="utf-8")
    return out_path
