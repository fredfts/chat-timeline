"""Codex source — reads conversation JSONL rollouts from ``~/.codex``."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path

from chat_timeline.markdown import (
    epoch_ms_to_dt,
    fmt_dt_filename,
    iso_to_dt,
    sanitize_filename,
)
from chat_timeline.sources._cache import JSONLCache

# See sources/cursor.py for why the _legacy.main imports are deferred.

SOURCE_NAME = "Codex"
SYSTEM_USER_PREFIXES = ("<environment_context>",)
INTERRUPTION_MARKERS = {
    "[request interrupted by user]",
    "[response interrupted by user]",
}
IDE_REQUEST_MARKER_RE = re.compile(
    r"^##\s*my request for codex\s*:?\s*$",
    re.IGNORECASE,
)
SESSION_ID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_CODEX_STORAGE_ROOTS_CACHE = None


# ---------------------------------------------------------------------------
# Codex storage paths
# ---------------------------------------------------------------------------


def codex_storage_roots():
    """Return all Codex data roots visible from this runtime."""
    global _CODEX_STORAGE_ROOTS_CACHE
    if _CODEX_STORAGE_ROOTS_CACHE is not None:
        return list(_CODEX_STORAGE_ROOTS_CACHE)

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

    # Native location
    _add(Path.home() / ".codex")

    # Cross-runtime fallbacks
    if platform.system() == "Windows":
        # Read WSL Codex data from Windows (default distro).
        try:
            proc = subprocess.run(
                ["wsl.exe", "wslpath", "-w", "~/.codex"],
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
        # Read Windows Codex data from WSL/Linux via mounted drive.
        users_root = Path("/mnt/c/Users")
        if users_root.exists():
            for user_dir in users_root.iterdir():
                if user_dir.is_dir():
                    _add(user_dir / ".codex")

    if roots:
        _CODEX_STORAGE_ROOTS_CACHE = tuple(roots)
        return list(_CODEX_STORAGE_ROOTS_CACHE)
    raise SystemExit("Codex data directory not found (~/.codex)")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _to_posix(path):
    return str(path or "").replace("\\", "/")


def _normalize_path(path):
    value = _to_posix(path).strip()
    if not value:
        return ""
    value = re.sub(r"/+", "/", value)
    if len(value) >= 2 and value[1] == ":":
        value = value[0].lower() + value[1:]
    return value.rstrip("/").lower()


def _windows_to_wsl(path):
    value = _to_posix(path).strip()
    match = re.match(r"^([A-Za-z]):/?(.*)$", value)
    if not match:
        return ""
    drive = match.group(1).lower()
    tail = match.group(2).lstrip("/")
    return f"/mnt/{drive}/{tail}" if tail else f"/mnt/{drive}"


def _wsl_to_windows(path):
    value = _to_posix(path).strip()
    match = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", value)
    if not match:
        return ""
    drive = match.group(1).upper()
    tail = (match.group(2) or "").lstrip("/")
    return f"{drive}:/{tail}" if tail else f"{drive}:/"


def _project_path_candidates(project_dir: Path):
    """Return normalized project path candidates in both Windows and WSL forms."""
    resolved = _to_posix(project_dir.resolve())
    candidates = {resolved.rstrip("/")}

    as_wsl = _windows_to_wsl(resolved)
    if as_wsl:
        candidates.add(as_wsl.rstrip("/"))

    as_win = _wsl_to_windows(resolved)
    if as_win:
        candidates.add(as_win.rstrip("/"))

    return {_normalize_path(p) for p in candidates if p}


def _paths_overlap(path, project_candidates):
    """True when path is the project, a child, or a parent of project path."""
    value = _normalize_path(path)
    if not value:
        return False
    for cand in project_candidates:
        if value == cand:
            return True
        if value.startswith(cand + "/"):
            return True
        if cand.startswith(value + "/"):
            return True
    return False


def _relative_workspace_path(path):
    from chat_timeline._legacy.main import relative_path

    raw = str(path or "")
    if not raw:
        return ""

    rel = relative_path(raw)
    if rel != raw:
        return rel

    as_wsl = _windows_to_wsl(raw)
    if as_wsl:
        rel_wsl = relative_path(as_wsl)
        if rel_wsl != as_wsl:
            return rel_wsl

    as_win = _wsl_to_windows(raw)
    if as_win:
        rel_win = relative_path(as_win)
        if rel_win != as_win:
            return rel_win

    return raw


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def load_conversation(jsonl_path: Path):
    """Load all records from a Codex JSONL rollout file."""
    messages = []
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def _peek_cwd(jsonl_path: Path) -> str:
    """Cheap pre-scan: read up to the first 2 JSON lines for ``payload.cwd``.

    Codex rollouts almost always lead with a ``session_meta`` record that
    has ``payload.cwd``. When found, ``list_chats`` can run ``_paths_overlap``
    *before* the expensive full-file parse. Returns "" if no cwd was visible
    in the head — callers fall back to today's full-parse behavior in that
    case, so nothing breaks for older/atypical session formats.
    """
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for _ in range(2):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = msg.get("payload", {})
                if isinstance(payload, dict):
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        pass
    return ""


def _extract_session_id_from_path(jsonl_path: Path):
    match = SESSION_ID_RE.search(jsonl_path.name)
    return match.group(1) if match else ""


def _extract_real_user_text(payload):
    """Return user text for real prompts, or None for internal/system noise."""
    if payload.get("type") != "user_message":
        return None

    text = payload.get("message", "")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    if any(text.startswith(prefix) for prefix in SYSTEM_USER_PREFIXES):
        return None
    if text.lower() in INTERRUPTION_MARKERS:
        return None
    return _extract_codex_request_body(text)


def _extract_codex_request_body(text: str):
    """Strip IDE wrapper and return only the real request body when available."""
    if not text:
        return text

    normalized = text.replace("\r\n", "\n")
    lines = normalized.split("\n")

    for i, line in enumerate(lines):
        if IDE_REQUEST_MARKER_RE.match(line.strip()):
            body = "\n".join(lines[i + 1 :]).strip()
            if body:
                return body
            break

    return normalized.strip()


def _title_from_user_text(user_text: str):
    """Build a compact title from the first meaningful line in user text."""
    cleaned_text = _extract_codex_request_body(user_text or "")
    lines = [line.strip() for line in cleaned_text.splitlines()]

    for candidate in lines:
        if candidate:
            return candidate.rstrip(";, ")[:120]
    return "(unnamed)"


def _safe_json_dict(raw):
    """Best-effort JSON parsing that always returns a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"_value": parsed}
        except Exception:
            return {}
    return {}


def _load_session_index(root: Path):
    """Load session index rows by session id, if present."""
    index_path = root / "session_index.jsonl"
    rows: dict[str, dict] = {}
    if not index_path.exists():
        return rows

    with open(index_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = row.get("id")
            if not isinstance(sid, str) or not sid:
                continue

            updated_dt = iso_to_dt(row.get("updated_at"))
            rows[sid.lower()] = {
                "title": row.get("thread_name", ""),
                "updated_ms": int(updated_dt.timestamp() * 1000) if updated_dt else 0,
            }
    return rows


def _session_files(root: Path):
    sessions_dir = root / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(
        [p for p in sessions_dir.glob("**/*.jsonl") if p.is_file()],
        key=lambda p: p.as_posix(),
    )


def extract_conversation_metadata(messages, fallback_title="", session_id_hint=""):
    """Extract metadata from a Codex rollout stream."""
    meta = {
        "session_id": session_id_hint,
        "title": fallback_title or "",
        "first_timestamp": None,
        "last_timestamp": None,
        "model": "",
        "branch": "",
        "cwd": "",
        "user_count": 0,
    }

    for msg in messages:
        ts = msg.get("timestamp")
        if ts:
            if meta["first_timestamp"] is None:
                meta["first_timestamp"] = ts
            meta["last_timestamp"] = ts

        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if msg_type == "session_meta":
            sid = payload.get("id")
            if isinstance(sid, str) and sid and not meta["session_id"]:
                meta["session_id"] = sid

            if not meta["cwd"] and isinstance(payload.get("cwd"), str):
                meta["cwd"] = payload.get("cwd", "")

            git_meta = payload.get("git", {})
            if isinstance(git_meta, dict) and not meta["branch"]:
                branch = git_meta.get("branch", "")
                if isinstance(branch, str):
                    meta["branch"] = branch

            model = payload.get("model")
            if isinstance(model, str) and model:
                meta["model"] = model

        elif msg_type == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                meta["model"] = model
            if not meta["cwd"] and isinstance(payload.get("cwd"), str):
                meta["cwd"] = payload.get("cwd", "")

        elif msg_type == "event_msg":
            user_text = _extract_real_user_text(payload)
            if user_text is None:
                continue

            meta["user_count"] += 1
            if not meta["title"]:
                meta["title"] = _title_from_user_text(user_text)

    if not meta["title"]:
        meta["title"] = "(unnamed)"
    if not meta["session_id"]:
        meta["session_id"] = ""
    return meta


# ---------------------------------------------------------------------------
# Chat listing
# ---------------------------------------------------------------------------


def list_chats(
    project_dir: Path,
    scope: Path | None = None,
    cache_dir: Path | None = None,
):
    """Return list of chat metadata dicts, sorted newest-first.

    ``scope`` narrows the cwd-overlap check without affecting where exports
    land. Defaults to ``project_dir``.

    ``cache_dir``, when given, enables an on-disk metadata cache under
    ``<cache_dir>/codex/``. Invalidated by JSONL mtime/size, so the worst
    case (corrupt or stale entry) is one extra parse — same as today.
    """
    match_dir = scope if scope is not None else project_dir
    project_candidates = _project_path_candidates(match_dir)
    cache = JSONLCache(cache_dir, "codex") if cache_dir is not None else None

    chats = []
    for root in codex_storage_roots():
        index_rows = _load_session_index(root)
        for jsonl_file in _session_files(root):
            sid_hint = _extract_session_id_from_path(jsonl_file)
            idx = index_rows.get(sid_hint.lower(), {}) if sid_hint else {}

            # Phase 1 (cheap): peek at the JSONL head for the cwd. If we get
            # a definitive non-overlap, skip the full parse — the dominant
            # cost on wide-scope scans.
            cwd_hint = _peek_cwd(jsonl_file)
            if cwd_hint and not _paths_overlap(cwd_hint, project_candidates):
                continue

            # Phase 2: full parse (or cache hit). The cache stores the chat
            # dict; on hit we reconstruct it without touching the JSONL body.
            cached = cache.get(jsonl_file) if cache is not None else None
            if cached is not None:
                # Rehydrate the Path serialized as str
                cached["_jsonl_path"] = Path(cached["_jsonl_path"])
                chats.append(cached)
                continue

            messages = load_conversation(jsonl_file)
            if not messages:
                continue

            meta = extract_conversation_metadata(
                messages,
                fallback_title=idx.get("title", ""),
                session_id_hint=sid_hint,
            )

            if meta["user_count"] == 0:
                continue
            if not _paths_overlap(meta.get("cwd", ""), project_candidates):
                continue

            first_dt = iso_to_dt(meta["first_timestamp"])
            last_dt = iso_to_dt(meta["last_timestamp"])
            last_event_ms = int(last_dt.timestamp() * 1000) if last_dt else 0
            try:
                file_mtime_ms = int(jsonl_file.stat().st_mtime * 1000)
            except OSError:
                file_mtime_ms = 0
            updated_ms = max(
                int(idx.get("updated_ms", 0) or 0),
                last_event_ms,
                file_mtime_ms,
            )

            chat = {
                "name": meta["title"] or "(unnamed)",
                "lastUpdatedAt": updated_ms,
                "createdAt": int(first_dt.timestamp() * 1000) if first_dt else 0,
                "unifiedMode": "agent",
                # Codex-specific fields
                "_session_id": meta.get("session_id", ""),
                "_jsonl_path": jsonl_file,
                "_model": meta.get("model", ""),
                "_branch": meta.get("branch", ""),
                "_cwd": meta.get("cwd", ""),
            }
            chats.append(chat)

            if cache is not None:
                # Serialize Path as str for JSON storage; rehydrated on read.
                serializable = dict(chat)
                serializable["_jsonl_path"] = str(jsonl_file)
                cache.put(jsonl_file, serializable)

    # Deduplicate across roots by session id (fallback to file path)
    seen: dict[str, dict] = {}
    for c in chats:
        key = c.get("_session_id") or str(c.get("_jsonl_path"))
        if key not in seen or c["lastUpdatedAt"] > seen[key]["lastUpdatedAt"]:
            seen[key] = c
    chats = list(seen.values())
    chats.sort(key=lambda c: c["lastUpdatedAt"], reverse=True)

    if chats:
        return chats
    raise SystemExit(f"No Codex sessions found for {match_dir}")


# ---------------------------------------------------------------------------
# Conversation building
# ---------------------------------------------------------------------------


def _normalize_tool_call(name, params):
    """Map Codex-native tool names into shared exporter-friendly names."""
    if name == "exec_command":
        return "run_terminal_command", {
            "command": params.get("cmd", ""),
            "workdir": params.get("workdir", ""),
        }
    if name == "write_stdin":
        chars = params.get("chars", "")
        snippet = chars[:80] if isinstance(chars, str) else ""
        return "run_terminal_command", {
            "command": f"<stdin>{snippet}",
            "session_id": params.get("session_id", ""),
        }
    if name == "request_user_input":
        return "AskQuestion", params
    if name == "update_plan":
        return "todo_write", params
    return name, params


def _extract_turn_models(messages):
    """Return mapping turn_id -> model from turn_context records."""
    turn_models = {}
    for msg in messages:
        if msg.get("type") != "turn_context":
            continue
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            continue
        turn_id = payload.get("turn_id")
        model = payload.get("model")
        if isinstance(turn_id, str) and turn_id and isinstance(model, str) and model:
            turn_models[turn_id] = model
    return turn_models


def _patch_changed_files(payload):
    """Extract changed file paths from patch_apply_end payload."""
    changes = payload.get("changes", {})
    if not isinstance(changes, dict):
        return []

    paths = []
    seen = set()
    for raw_path in changes.keys():
        rel = _relative_workspace_path(raw_path)
        if not rel:
            continue
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(rel)
    return paths


def build_conversation(messages):
    """Parse rollout records into structured Q&A turns."""
    turn_models = _extract_turn_models(messages)

    turns = []
    current = None
    pending_turn_id = ""

    for msg in messages:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        ts = iso_to_dt(msg.get("timestamp"))

        if msg_type == "event_msg":
            payload_type = payload.get("type", "")

            if payload_type == "task_started":
                turn_id = payload.get("turn_id", "")
                if isinstance(turn_id, str):
                    pending_turn_id = turn_id
                continue

            if payload_type == "user_message":
                user_text = _extract_real_user_text(payload)
                if user_text is None:
                    continue

                if current:
                    turns.append(current)

                turn_id = payload.get("turn_id") or pending_turn_id
                model = turn_models.get(turn_id, "unknown")
                current = {
                    "user_text": user_text,
                    "user_timestamp": ts,
                    "user_model": model or "unknown",
                    "assistant_parts": [],
                    "tool_calls": [],
                    "thinking_blocks": [],
                    "checkpoints": [],
                    "_turn_id": turn_id,
                }
                continue

            if payload_type == "agent_message" and current is not None:
                text = payload.get("message", "")
                if isinstance(text, str) and text.strip():
                    model = turn_models.get(
                        current.get("_turn_id", ""),
                        current.get("user_model", "unknown"),
                    )
                    current["assistant_parts"].append(
                        {
                            "text": text,
                            "timestamp": ts,
                            "model": model or "unknown",
                        }
                    )
                continue

            if payload_type == "patch_apply_end" and current is not None:
                status = payload.get("status", "")
                if not status:
                    status = "completed" if payload.get("success") else "failed"
                call_id = payload.get("call_id", "")
                for path in _patch_changed_files(payload):
                    current["tool_calls"].append(
                        {
                            "name": "edit_file",
                            "status": status or "completed",
                            "timestamp": ts,
                            "params": {"file_path": path},
                            "raw_args": json.dumps(
                                {"call_id": call_id, "file_path": path},
                                ensure_ascii=False,
                                default=str,
                            ),
                        }
                    )
                continue

        if msg_type != "response_item" or current is None:
            continue

        item_type = payload.get("type", "")

        if item_type == "function_call":
            raw_args = payload.get("arguments", "")
            params = _safe_json_dict(raw_args)
            raw_name = payload.get("name", "unknown")
            name, params = _normalize_tool_call(raw_name, params)
            current["tool_calls"].append(
                {
                    "name": name,
                    "status": "completed",
                    "timestamp": ts,
                    "params": params if isinstance(params, dict) else {},
                    "raw_args": raw_args
                    if isinstance(raw_args, str)
                    else json.dumps(raw_args, ensure_ascii=False, default=str),
                }
            )

        elif item_type == "custom_tool_call":
            raw_name = payload.get("name", "unknown")
            raw_input = payload.get("input", "")
            params = _safe_json_dict(raw_input)
            name, params = _normalize_tool_call(raw_name, params)
            current["tool_calls"].append(
                {
                    "name": name,
                    "status": payload.get("status", "completed"),
                    "timestamp": ts,
                    "params": params if isinstance(params, dict) else {},
                    "raw_args": raw_input
                    if isinstance(raw_input, str)
                    else json.dumps(raw_input, ensure_ascii=False, default=str),
                }
            )

        elif item_type == "web_search_call":
            action = payload.get("action", {})
            if not isinstance(action, dict):
                action = {}
            current["tool_calls"].append(
                {
                    "name": "WebSearch",
                    "status": payload.get("status", "completed"),
                    "timestamp": ts,
                    "params": {"query": action.get("query", "")},
                    "raw_args": json.dumps(action, ensure_ascii=False, default=str),
                }
            )

    if current:
        turns.append(current)

    for turn in turns:
        turn.pop("_turn_id", None)
    return turns


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_single_chat(chat, include_tool_params=False):
    """Export one Codex conversation to STAGED_DIR and return the file path."""
    from chat_timeline._legacy.main import STAGED_DIR, export_chat_markdown

    jsonl_path = chat["_jsonl_path"]
    session_id = chat.get("_session_id", "")
    name = chat.get("name", "(unnamed)")
    created_dt = epoch_ms_to_dt(chat.get("createdAt"))

    messages = load_conversation(jsonl_path)
    if not messages:
        return None

    turns = build_conversation(messages)
    if not turns:
        return None

    # Use first user message timestamp when available.
    first_user_ts = turns[0].get("user_timestamp") if turns else None
    effective_dt = first_user_ts or created_dt

    files_affected = set()
    for turn in turns:
        for tc in turn["tool_calls"]:
            name = tc.get("name", "")
            if name not in ("edit_file", "Edit", "Write"):
                continue
            params = tc.get("params", {})
            if not isinstance(params, dict):
                continue
            fp = (
                params.get("file_path")
                or params.get("relativeWorkspacePath")
                or params.get("filePath")
                or params.get("path")
                or ""
            )
            if fp:
                files_affected.add(_relative_workspace_path(fp))

    meta = {
        "name": chat.get("name", "(unnamed)"),
        "id": session_id,
        "created": created_dt,
        "last_updated": epoch_ms_to_dt(chat.get("lastUpdatedAt")),
        "status": "completed",
        "mode": "agent",
        "model": chat.get("_model", "unknown"),
        "max_mode": False,
        "agent_backend": "codex",
        "branch": chat.get("_branch", ""),
        "context_tokens": 0,
        "context_limit": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "files_changed": len(files_affected),
        "is_agentic": True,
        "files_affected": sorted(files_affected),
        "source": SOURCE_NAME,
    }

    md = export_chat_markdown(meta, turns, include_tool_params=include_tool_params)

    safe_name = sanitize_filename(chat.get("name", "(unnamed)") or "(unnamed)")
    ts_str = fmt_dt_filename(effective_dt)
    filename = f"{ts_str}_{SOURCE_NAME}_{safe_name}.md"

    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = STAGED_DIR / filename
    out_path.write_text(md, encoding="utf-8")
    return out_path
