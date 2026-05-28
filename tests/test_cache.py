"""Tests for chat_timeline.sources._cache — JSONLCache mtime invalidation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from chat_timeline.sources._cache import JSONLCache


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_cache_hit_after_put(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    _write(jsonl, "{}\n")
    cache = JSONLCache(tmp_path / ".cache", "codex")

    assert cache.get(jsonl) is None
    cache.put(jsonl, {"foo": "bar"})
    assert cache.get(jsonl) == {"foo": "bar"}


def test_cache_invalidates_on_mtime_change(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    _write(jsonl, "{}\n")
    cache = JSONLCache(tmp_path / ".cache", "codex")
    cache.put(jsonl, {"v": 1})
    assert cache.get(jsonl) == {"v": 1}

    # Bump mtime by 1s in the future to simulate a modification.
    stat = jsonl.stat()
    os.utime(jsonl, (stat.st_atime + 1, stat.st_mtime + 1))

    assert cache.get(jsonl) is None


def test_cache_invalidates_on_size_change(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    _write(jsonl, "{}\n")
    cache = JSONLCache(tmp_path / ".cache", "codex")
    cache.put(jsonl, {"v": 1})

    # Same mtime, different size — should also invalidate
    stat = jsonl.stat()
    _write(jsonl, '{"longer": "content"}\n')
    os.utime(jsonl, (stat.st_atime, stat.st_mtime))

    assert cache.get(jsonl) is None


def test_cache_handles_missing_source_file(tmp_path):
    cache = JSONLCache(tmp_path / ".cache", "codex")
    assert cache.get(tmp_path / "nonexistent.jsonl") is None
    # Put on missing source is a no-op
    cache.put(tmp_path / "nonexistent.jsonl", {"v": 1})
    assert cache.get(tmp_path / "nonexistent.jsonl") is None


def test_cache_handles_corrupt_entry(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    _write(jsonl, "{}\n")
    cache = JSONLCache(tmp_path / ".cache", "codex")

    # Manually write a corrupt cache file at the expected path
    key_path = cache._key_path(jsonl)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("not json {", encoding="utf-8")

    assert cache.get(jsonl) is None  # treated as miss, doesn't raise


def test_cache_per_source_isolation(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    _write(jsonl, "{}\n")
    codex_cache = JSONLCache(tmp_path / ".cache", "codex")
    claude_cache = JSONLCache(tmp_path / ".cache", "claude")

    codex_cache.put(jsonl, {"src": "codex"})
    claude_cache.put(jsonl, {"src": "claude"})

    assert codex_cache.get(jsonl) == {"src": "codex"}
    assert claude_cache.get(jsonl) == {"src": "claude"}


def test_cache_file_layout(tmp_path):
    """Sanity: hit/miss is keyed by JSONL absolute path."""
    jsonl = tmp_path / "session.jsonl"
    _write(jsonl, "{}\n")
    cache = JSONLCache(tmp_path / ".cache", "codex")
    cache.put(jsonl, {"v": 1})

    files = list((tmp_path / ".cache" / "codex").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"] == {"v": 1}
    assert isinstance(payload["mtime_ns"], int)
    assert isinstance(payload["size"], int)
