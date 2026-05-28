"""Per-JSONL metadata cache used by the source scanners.

Each entry is a JSON file under ``<base_dir>/<source>/<sha1>.json`` keyed
by the absolute JSONL path. The cache is invalidated when the JSONL's
mtime or size changes — both are stored alongside the cached payload.

JSON (not pickle) so cache entries survive Python version changes and are
debuggable by eye.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class JSONLCache:
    """Mtime-keyed cache for parsed JSONL session metadata."""

    def __init__(self, base_dir: Path, source_name: str) -> None:
        self.dir = base_dir / source_name

    def _key_path(self, jsonl_path: Path) -> Path:
        try:
            resolved = str(jsonl_path.resolve())
        except OSError:
            resolved = str(jsonl_path)
        digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()
        return self.dir / f"{digest}.json"

    def get(self, jsonl_path: Path) -> Any | None:
        """Return cached data if the JSONL hasn't changed since storage,
        else None. Corrupt or stale entries are treated as misses."""
        try:
            stat = jsonl_path.stat()
        except OSError:
            return None
        cache_path = self._key_path(jsonl_path)
        if not cache_path.exists():
            return None
        try:
            entry = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if entry.get("mtime_ns") != stat.st_mtime_ns:
            return None
        if entry.get("size") != stat.st_size:
            return None
        return entry.get("data")

    def put(self, jsonl_path: Path, data: Any) -> None:
        """Store ``data`` under ``jsonl_path``'s key. Best-effort —
        failures (permissions, disk full) are silently swallowed."""
        try:
            stat = jsonl_path.stat()
        except OSError:
            return
        cache_path = self._key_path(jsonl_path)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "data": data,
            }
            cache_path.write_text(
                json.dumps(entry, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass
