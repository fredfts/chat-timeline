"""
Claude Code-specific chat extraction module.

Reads conversation data from Claude Code's local JSONL files and exports
to the common markdown format used by main.py.
"""

import json
import platform
import subprocess
from pathlib import Path

from chat_timeline._legacy.main import (
    PROJECT_DIR, STAGED_DIR,
    epoch_ms_to_dt, iso_to_dt, fmt_dt, fmt_dt_filename,
    export_chat_markdown, sanitize_filename,
)

SOURCE_NAME = "Claude"
SYSTEM_USER_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
)
INTERRUPTION_MARKERS = {
    "[request interrupted by user]",
    "[response interrupted by user]",
}
_CLAUDE_STORAGE_ROOTS_CACHE = None
_PROJECT_STORAGES_CACHE = {}


# ---------------------------------------------------------------------------
# Claude Code storage paths
# ---------------------------------------------------------------------------

def claude_storage_roots():
    """Return all Claude Code data roots visible from this runtime."""
    global _CLAUDE_STORAGE_ROOTS_CACHE
    if _CLAUDE_STORAGE_ROOTS_CACHE is not None:
        return list(_CLAUDE_STORAGE_ROOTS_CACHE)

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
    _add(Path.home() / ".claude")

    # Cross-runtime fallbacks
    if platform.system() == "Windows":
        # Read WSL Claude data from Windows (default distro).
        try:
            proc = subprocess.run(
                ["wsl.exe", "wslpath", "-w", "~/.claude"],
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
        # Read Windows Claude data from WSL/Linux via mounted drive.
        users_root = Path("/mnt/c/Users")
        if users_root.exists():
            for user_dir in users_root.iterdir():
                if user_dir.is_dir():
                    _add(user_dir / ".claude")

    if roots:
        _CLAUDE_STORAGE_ROOTS_CACHE = tuple(roots)
        return list(_CLAUDE_STORAGE_ROOTS_CACHE)
    raise SystemExit("Claude Code data directory not found (~/.claude)")


def project_dir_to_slugs(project_dir: Path):
    """Convert a project path to possible Claude Code directory slugs.

    Handles both native and WSL-mapped forms:
      /mnt/c/Users/Fred/mascat -> -mnt-c-Users-Fred-mascat
      C:/Users/Fred/mascat     -> C:-Users-Fred-mascat
      C:/Users/Fred/mascat     -> C--Users-Fred-mascat (new app variant)
      C:/Users/Fred/mascat     -> -mnt-c-Users-Fred-mascat (fallback)
    """
    resolved = str(project_dir.resolve())
    slugs = []

    def _add_slug(slug: str):
        if slug and slug not in slugs:
            slugs.append(slug)

    native_slug = resolved.replace("/", "-").replace("\\", "-")
    _add_slug(native_slug)
    # Some Claude app builds sanitize drive separators (`C:` -> `C-`).
    _add_slug(native_slug.replace(":", "-"))

    # Windows path -> WSL-style fallback slug
    # C:/Users/Fred/mascat -> /mnt/c/Users/Fred/mascat -> -mnt-c-Users-Fred-mascat
    if len(resolved) >= 3 and resolved[1] == ":" and resolved[2] in ("\\", "/"):
        drive = resolved[0].lower()
        tail = resolved[3:].replace("\\", "/").lstrip("/")
        wsl_path = f"/mnt/{drive}/{tail}" if tail else f"/mnt/{drive}"
        wsl_slug = wsl_path.replace("/", "-")
        _add_slug(wsl_slug)

    return slugs


def find_project_storages(project_dir: Path):
    """Return all matching Claude Code project storage directories."""
    key = str(project_dir.resolve()).lower()
    if key in _PROJECT_STORAGES_CACHE:
        return list(_PROJECT_STORAGES_CACHE[key])

    slugs = project_dir_to_slugs(project_dir)
    storages = []
    seen = set()
    searched_roots = []

    for root in claude_storage_roots():
        searched_roots.append(str(root))
        projects_dir = root / "projects"
        if not projects_dir.exists():
            continue

        matched = None
        for slug in slugs:
            project_path = projects_dir / slug
            if project_path.exists():
                matched = project_path
                break

        # Try case-insensitive variations
        if matched is None:
            for d in projects_dir.iterdir():
                if not d.is_dir():
                    continue
                d_name = d.name.lower()
                if any(d_name == slug.lower() for slug in slugs):
                    matched = d
                    break

        if matched is not None:
            key = str(matched).lower()
            if key not in seen:
                storages.append(matched)
                seen.add(key)

    if storages:
        _PROJECT_STORAGES_CACHE[key] = tuple(storages)
        return list(_PROJECT_STORAGES_CACHE[key])
    raise SystemExit(
        f"No Claude Code project storage found for {project_dir}\n"
        f"  Tried slugs: {', '.join(slugs)}\n"
        f"  Searched roots: {', '.join(searched_roots)}")


def find_project_storage(project_dir: Path):
    """Backward-compatible: return the first matching project storage directory."""
    return find_project_storages(project_dir)[0]


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def load_conversation(jsonl_path: Path):
    """Load all messages from a Claude Code JSONL conversation file."""
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


def _extract_real_user_text(msg):
    """Return user text for real human prompts, or None for internal/system noise."""
    if msg.get("type") != "user":
        return None

    content = msg.get("message", {}).get("content", "")
    if isinstance(content, list):
        return None  # tool_result
    if not isinstance(content, str):
        return None

    text = content.strip()
    if not text:
        return None
    if any(text.startswith(prefix) for prefix in SYSTEM_USER_PREFIXES):
        return None
    if text.lower() in INTERRUPTION_MARKERS:
        return None
    return text


def _user_identity(msg, user_text: str):
    """Stable user-message identity for cross-session clone dedupe."""
    for key in ("uuid", "promptId"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            return f"id:{value.lower()}"

    ts = msg.get("timestamp", "")
    normalized = " ".join(user_text.split())
    return f"fallback:{ts}|{normalized[:160]}"


def extract_conversation_metadata(messages):
    """Extract metadata from a list of JSONL messages.

    Returns a dict with: session_id, title, first_timestamp, last_timestamp,
    model, branch, version.
    """
    meta = {
        "session_id": "",
        "title": "",
        "first_timestamp": None,
        "last_timestamp": None,
        "model": "",
        "branch": "",
        "version": "",
    }

    for msg in messages:
        msg_type = msg.get("type", "")

        if msg_type == "custom-title":
            meta["title"] = msg.get("customTitle", "")

        if not meta["session_id"] and msg.get("sessionId"):
            meta["session_id"] = msg["sessionId"]

        if not meta["branch"] and msg.get("gitBranch"):
            meta["branch"] = msg["gitBranch"]

        if not meta["version"] and msg.get("version"):
            meta["version"] = msg["version"]

        ts = msg.get("timestamp")
        if ts:
            if meta["first_timestamp"] is None:
                meta["first_timestamp"] = ts
            meta["last_timestamp"] = ts

        if msg_type == "assistant" and not meta["model"]:
            meta["model"] = msg.get("message", {}).get("model", "")

    # Fallback title from first real user message
    if not meta["title"]:
        for msg in messages:
            if msg.get("type") == "user":
                content = msg.get("message", {}).get("content", "")
                if isinstance(content, str) and not content.startswith("<local-command-caveat>"):
                    # Use first line only, capped at 80 chars
                    first_line = content.split("\n")[0].rstrip(";, ")
                    meta["title"] = first_line[:80].strip()
                    break

    return meta


# ---------------------------------------------------------------------------
# Chat listing
# ---------------------------------------------------------------------------

def list_chats(project_dir: Path):
    """Return list of chat metadata dicts, sorted newest-first.

    Each dict has standardized keys for the interactive selector:
    name, lastUpdatedAt, createdAt, unifiedMode, plus Claude-specific fields.
    """
    project_storages = find_project_storages(project_dir)

    chats = []
    for project_storage in project_storages:
        for jsonl_file in project_storage.glob("*.jsonl"):
            messages = load_conversation(jsonl_file)
            if not messages:
                continue

            meta = extract_conversation_metadata(messages)

            first_dt = iso_to_dt(meta["first_timestamp"])
            last_dt = iso_to_dt(meta["last_timestamp"])

            user_identities = []
            for msg in messages:
                user_text = _extract_real_user_text(msg)
                if user_text is None:
                    continue
                user_identities.append(_user_identity(msg, user_text))
            user_count = len(user_identities)

            # Skip conversations with no actual user messages
            if user_count == 0:
                continue

            chats.append({
                "name": meta["title"] or "(unnamed)",
                "lastUpdatedAt": int(last_dt.timestamp() * 1000) if last_dt else 0,
                "createdAt": int(first_dt.timestamp() * 1000) if first_dt else 0,
                "unifiedMode": "agent",
                # Claude-specific fields
                "_session_id": meta["session_id"],
                "_jsonl_path": jsonl_file,
                "_model": meta["model"],
                "_branch": meta["branch"],
                "_version": meta["version"],
                "_user_count": user_count,
                "_user_identities": user_identities,
            })

    # Deduplicate across roots by session_id (fallback to file path)
    seen = {}
    for c in chats:
        key = c.get("_session_id") or str(c.get("_jsonl_path"))
        if key not in seen or c["lastUpdatedAt"] > seen[key]["lastUpdatedAt"]:
            seen[key] = c
    chats = list(seen.values())

    # Claude Code app can fork/rewind by cloning earlier prompts into a new
    # session file with a different session_id. Collapse those clone chains by
    # grouping chats that share the same first 3 user-message identities.
    ranked = sorted(
        chats,
        key=lambda c: (
            len(c.get("_user_identities", [])),
            c.get("lastUpdatedAt", 0),
        ),
        reverse=True,
    )
    collapsed = []
    for candidate in ranked:
        seq = candidate.get("_user_identities", [])
        is_clone = False
        if len(seq) >= 3:
            root = seq[:3]
            for kept in collapsed:
                kept_seq = kept.get("_user_identities", [])
                if len(kept_seq) < 3:
                    continue
                # Sharing the first 3 user-message UUIDs is essentially
                # impossible by coincidence (UUIDs are globally unique), so
                # treat any match as the same conversation. Whichever ranks
                # lower (fewer turns, or same turns but older) is the fork —
                # covers exact prefixes, rewind forks (shorter), and rewrite
                # forks (same length, diverging at the rewritten turn).
                if kept_seq[:3] == root:
                    is_clone = True
                    break
        if not is_clone:
            collapsed.append(candidate)

    chats = collapsed
    chats.sort(key=lambda c: c["lastUpdatedAt"], reverse=True)
    return chats


# ---------------------------------------------------------------------------
# Conversation building
# ---------------------------------------------------------------------------

def _load_subagent_tool_calls(session_dir: Path, agent_id: str, timestamp):
    """Load tool calls from a subagent's JSONL file."""
    subagent_path = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
    if not subagent_path.exists():
        return []

    tool_calls = []
    messages = load_conversation(subagent_path)
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        for block in msg.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                params = block.get("input", {})
                tool_calls.append({
                    "name": block.get("name", "unknown"),
                    "status": "completed",
                    "timestamp": iso_to_dt(msg.get("timestamp")) or timestamp,
                    "params": params if isinstance(params, dict) else {},
                    "raw_args": json.dumps(params, ensure_ascii=False, default=str),
                })
    return tool_calls


def build_conversation(messages, session_dir: Path):
    """Parse JSONL messages into structured Q&A turns.

    Args:
        messages: list of parsed JSONL message dicts
        session_dir: directory containing the session's subagents/ folder
    """
    # Build a map of tool_use_id -> agent_id from progress messages
    tool_to_agent = {}
    for msg in messages:
        if msg.get("type") == "progress":
            data = msg.get("data", {})
            if data.get("type") == "agent_progress":
                agent_id = data.get("agentId", "")
                parent_tool_id = msg.get("parentToolUseID", "")
                if agent_id and parent_tool_id:
                    tool_to_agent[parent_tool_id] = agent_id

    turns = []
    current = None
    # Dedup streaming chunks by block identity, not by position. Each JSONL
    # line typically carries just one block from a larger logical assistant
    # message, so the old `(msg_id, idx_within_line)` scheme collided on
    # `idx=0` and dropped tool_use blocks that followed thinking/text under
    # the same msg_id. Identity keys avoid that: tool_use blocks have unique
    # `id`s within a msg, and text/thinking blocks dedup by their content
    # prefix (handles the case where a future Claude version replays earlier
    # blocks in cumulative content arrays).
    seen_blocks_by_msg = {}

    def _block_fingerprint(block):
        bt = block.get("type", "")
        if bt == "tool_use":
            return ("tool_use", block.get("id", ""))
        if bt == "thinking":
            return ("thinking", (block.get("thinking", "") or "")[:128])
        if bt == "text":
            return ("text", (block.get("text", "") or "")[:128])
        return None

    for msg in messages:
        msg_type = msg.get("type", "")

        if msg_type == "user":
            user_text = _extract_real_user_text(msg)
            if user_text is None:
                continue

            # Start a new turn
            if current:
                turns.append(current)

            current = {
                "user_text": user_text,
                "user_timestamp": iso_to_dt(msg.get("timestamp")),
                "user_model": "",
                "assistant_parts": [],
                "tool_calls": [],
                "thinking_blocks": [],
                "checkpoints": [],
            }

        elif msg_type == "assistant" and current is not None:
            api_msg = msg.get("message", {})
            model = api_msg.get("model", "unknown")
            msg_id = api_msg.get("id", "")

            if not current["user_model"]:
                current["user_model"] = model

            for block in api_msg.get("content", []):
                if not isinstance(block, dict):
                    continue

                fp = _block_fingerprint(block)
                if msg_id and fp is not None:
                    seen_set = seen_blocks_by_msg.setdefault(msg_id, set())
                    if fp in seen_set:
                        continue
                    seen_set.add(fp)

                block_type = block.get("type", "")

                if block_type == "thinking":
                    text = block.get("thinking", "")
                    if text:
                        current["thinking_blocks"].append({
                            "duration_ms": 0,
                            "text": text,
                            "timestamp": iso_to_dt(msg.get("timestamp")),
                        })

                elif block_type == "tool_use":
                    params = block.get("input", {})
                    tool_id = block.get("id", "")
                    tc = {
                        "name": block.get("name", "unknown"),
                        "status": "completed",
                        "timestamp": iso_to_dt(msg.get("timestamp")),
                        "params": params if isinstance(params, dict) else {},
                        "raw_args": json.dumps(
                            params, ensure_ascii=False, default=str),
                    }
                    current["tool_calls"].append(tc)

                    # If this is an Agent tool call, flatten subagent tool calls
                    if block.get("name") == "Agent" and tool_id in tool_to_agent:
                        agent_id = tool_to_agent[tool_id]
                        sub_tcs = _load_subagent_tool_calls(
                            session_dir, agent_id,
                            iso_to_dt(msg.get("timestamp")))
                        current["tool_calls"].extend(sub_tcs)

                elif block_type == "text":
                    text = block.get("text", "")
                    if text:
                        current["assistant_parts"].append({
                            "text": text,
                            "timestamp": iso_to_dt(msg.get("timestamp")),
                            "model": model,
                        })

    if current:
        turns.append(current)
    return turns


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_single_chat(chat, include_tool_params=False):
    """Export one Claude Code conversation to STAGED_DIR and return the file path."""
    jsonl_path = chat["_jsonl_path"]
    session_id = chat["_session_id"]
    name = chat.get("name", "(unnamed)")
    created_dt = epoch_ms_to_dt(chat.get("createdAt"))

    messages = load_conversation(jsonl_path)
    if not messages:
        return None

    session_dir = jsonl_path.parent / session_id
    turns = build_conversation(messages, session_dir)

    if not turns:
        return None

    # Use the first user message timestamp if available
    first_user_ts = turns[0].get("user_timestamp") if turns else None
    effective_dt = first_user_ts or created_dt

    # Collect files affected from Edit/Write tool calls
    files_affected = set()
    total_tool_calls = 0
    for turn in turns:
        for tc in turn["tool_calls"]:
            total_tool_calls += 1
            if tc["name"] in ("Edit", "Write", "edit_file", "edit_file_v2"):
                fp = tc["params"].get("file_path",
                     tc["params"].get("relativeWorkspacePath",
                     tc["params"].get("filePath",
                     tc["params"].get("path", ""))))
                if fp:
                    files_affected.add(fp)

    # Extract turn duration if available
    turn_duration_ms = 0
    for msg in messages:
        if (msg.get("type") == "system"
                and msg.get("subtype") == "turn_duration"):
            turn_duration_ms += msg.get("durationMs", 0)

    meta = {
        "name": name,
        "id": session_id,
        "created": created_dt,
        "last_updated": epoch_ms_to_dt(chat.get("lastUpdatedAt")),
        "status": "completed",
        "mode": "agent",
        "model": chat.get("_model", "unknown"),
        "max_mode": False,
        "agent_backend": "claude-code",
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

    safe_name = sanitize_filename(name or "(unnamed)")
    ts_str = fmt_dt_filename(effective_dt)

    filename = f"{ts_str}_{SOURCE_NAME}_{safe_name}.md"
    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = STAGED_DIR / filename

    out_path.write_text(md, encoding="utf-8")
    return out_path
