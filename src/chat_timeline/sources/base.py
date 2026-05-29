"""Source protocol — what every chat-source module must expose.

The orchestration layer (``_legacy.main._collect_chats`` today, eventually
``chat_timeline.app``) loads each source module by name and calls these
functions. A real subclass / class isn't required — module-level functions
matching this shape are enough.

Chat-dict schema returned by ``list_chats``:

    Required (used by the interactive selector and downstream generators):
        "name":          str          — display title
        "lastUpdatedAt": int          — ms epoch, used for sort & "modified since"
        "createdAt":     int          — ms epoch
        "unifiedMode":   str          — display only (e.g. "agent")

    Identity (at least one):
        "composer_id":   str          — Cursor stores it under this key
        "_session_id":   str          — Claude / Codex store it here

    Added by the orchestrator:
        "_source":       str          — populated by ``_collect_chats``

    Source-specific (optional, varies by source):
        "_jsonl_path":   Path         — Claude / Codex; Cursor uses SQLite
        "_cursor_root":  str          — Cursor only
        "_ws_hash":      str          — Cursor only
        "_model":        str          — Claude / Codex
        "_branch":       str          — Claude / Codex
        "_cwd":          str          — Codex only
        "_user_count":   int          — Claude only
        "_user_identities": list[str] — Claude only (used for clone-chain dedupe)

The ``scope`` parameter narrows which sessions are considered "in" the
project. When ``None``, scope defaults to ``project_dir``. Today only
Cursor and Codex consult it; Claude filters by user_count alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol


class Source(Protocol):
    """Structural protocol satisfied by ``sources.{cursor,claude,codex}``."""

    SOURCE_NAME: str

    def list_chats(self, project_dir: Path, scope: Path | None = None) -> list[dict]:
        """Return all chats associated with ``project_dir`` (or ``scope``),
        sorted newest-first. May raise SystemExit if no storage is found."""
        ...

    def export_single_chat(self, chat: dict, include_tool_params: bool = False) -> Path | None:
        """Write the chat's markdown export to the staged dir; return its path."""
        ...


# Convenient type aliases for the orchestration layer.
ListChatsFn = Callable[..., list[dict]]
ExportChatFn = Callable[..., "Path | None"]
