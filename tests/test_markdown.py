"""Tests for chat_timeline.markdown — extracted from _legacy/main.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from chat_timeline.markdown import (
    epoch_ms_to_dt,
    export_chat_markdown,
    fenced_literal_block,
    fmt_dt,
    fmt_dt_filename,
    format_tool_call_detail,
    iso_to_dt,
    parse_chat_export,
    parse_selection,
    relative_path,
    sanitize_filename,
    sanitize_markdown_content,
    strip_redacted,
)


def test_epoch_ms_to_dt_handles_none_and_zero():
    assert epoch_ms_to_dt(None) is None
    assert epoch_ms_to_dt(0) is None


def test_epoch_ms_to_dt_returns_utc():
    dt = epoch_ms_to_dt(1_700_000_000_000)
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_iso_to_dt_round_trip():
    assert iso_to_dt(None) is None
    assert iso_to_dt("") is None
    assert iso_to_dt("not-a-date") is None
    dt = iso_to_dt("2026-05-28T12:00:00Z")
    assert dt == datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_fmt_dt_and_filename():
    dt = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    assert fmt_dt(None) == ""
    assert fmt_dt(dt) == "2026-05-28 12:00:00 UTC"
    assert fmt_dt_filename(None) == "unknown"
    assert fmt_dt_filename(dt) == "2026-05-28_12-00-00"


def test_sanitize_markdown_neutralizes_atx_headings():
    out = sanitize_markdown_content("# Heading\n\ntext")
    assert out.splitlines()[0] == "**Heading**"


def test_sanitize_markdown_preserves_fenced_code():
    src = "```python\n# not a heading\n```"
    assert sanitize_markdown_content(src) == src


def test_sanitize_markdown_handles_blockquote_headings():
    out = sanitize_markdown_content("> # quoted heading")
    assert "**quoted heading**" in out


def test_fenced_literal_block_grows_past_existing_runs():
    text = "code with ``` inside"
    out = fenced_literal_block(text)
    fence = out.splitlines()[0].rstrip("markdown")
    assert len(fence) >= 4  # longer than the inner ```


def test_strip_redacted_removes_marker_block():
    assert strip_redacted("before''''secret''''after") == "beforeafter"
    assert strip_redacted("") == ""
    assert strip_redacted(None) is None


def test_sanitize_filename_strips_dangerous_chars():
    assert sanitize_filename('a<b>:c"d/e\\f|g?h*i;j') == "abcdefghij"
    assert sanitize_filename("  spaced   name  ") == "spaced name"
    assert len(sanitize_filename("x" * 200)) == 80


def test_relative_path_with_project_root(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    child = project / "src" / "file.py"
    assert relative_path(str(child), project) == "src/file.py"


def test_relative_path_outside_root_returned_as_is(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    elsewhere = "/some/other/path"
    out = relative_path(elsewhere, project)
    # On Windows paths starting with "/" stay unchanged; on POSIX they also pass through.
    assert "other/path" in out


def test_relative_path_handles_falsy():
    assert relative_path("") == ""
    assert relative_path(None) is None


def test_format_tool_call_detail_dispatches_by_name():
    read_call = {"name": "Read", "params": {"file_path": "x.py"}}
    bash_call = {"name": "Bash", "params": {"command": "ls -la"}}
    assert format_tool_call_detail(read_call) == "Read: x.py"
    assert format_tool_call_detail(bash_call) == "Bash: ls -la"
    long_cmd = "a" * 200
    out = format_tool_call_detail({"name": "Bash", "params": {"command": long_cmd}})
    assert out == f"Bash: {long_cmd[:120]}"
    assert format_tool_call_detail({"name": "todo_write", "params": {}}) == "todo_write"


def test_format_tool_call_detail_handles_string_params():
    call = {"name": "Anything", "params": "raw blob"}
    assert format_tool_call_detail(call) == "Anything: raw blob"


def test_export_chat_markdown_minimal_golden():
    """Tiny fixture pinned to detect any incidental formatting change."""
    meta = {
        "name": "Test chat",
        "id": "abc",
        "created": None,
        "last_updated": None,
        "source": "claude",
    }
    turns = [
        {
            "user_model": "user",
            "user_timestamp": None,
            "user_text": "Hello world",
            "thinking_blocks": [],
            "tool_calls": [],
            "assistant_parts": [{"model": "claude", "timestamp": None, "text": "Hi back"}],
        }
    ]
    out = export_chat_markdown(meta, turns)
    assert out.startswith("---\n")
    assert 'title: "Test chat"' in out
    assert "# Test chat" in out
    assert "## Q1 [user]" in out
    assert "> Hello world" in out
    assert "## A1 [claude]" in out
    assert "Hi back" in out
    # Final turn divider
    assert out.rstrip().endswith("---")


def test_parse_chat_export_round_trip(tmp_path: Path):
    ts = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    meta = {"name": "Round trip", "id": "rt-1", "source": "cursor"}
    turns = [
        {
            "user_model": "user",
            "user_timestamp": ts,
            "user_text": "ping",
            "thinking_blocks": [],
            "tool_calls": [],
            "assistant_parts": [{"model": "asst", "timestamp": ts, "text": "pong"}],
        }
    ]
    f = tmp_path / "chat.md"
    f.write_text(export_chat_markdown(meta, turns), encoding="utf-8")
    parsed_meta, parsed_turns = parse_chat_export(f)
    assert parsed_meta["title"] == "Round trip"
    assert len(parsed_turns) == 1
    assert "ping" in parsed_turns[0]["user_text"]


def test_parse_selection_variants():
    assert parse_selection("all", 5) == [0, 1, 2, 3, 4]
    assert parse_selection("1,3", 5) == [0, 2]
    assert parse_selection("1-3", 5) == [0, 1, 2]
    assert parse_selection("1,3-4", 5) == [0, 2, 3]
    assert parse_selection("99", 5) == []  # out-of-range silently dropped
