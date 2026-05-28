"""Tests for the pure helpers inside chat_timeline.tui.selector.

The interactive loop itself is too coupled to terminal state to unit-test;
those are exercised in the Phase 6 end-to-end smoke run.
"""

from __future__ import annotations

from chat_timeline.tui.selector import (
    _build_row_map,
    _chat_key_for_tracking,
    _chat_tracking_lookup_keys,
    _removed_marker_is_active,
    _removed_marker_payload,
    compact_selection,
    parse_selection_string,
)


def test_compact_selection_collapses_ranges():
    assert compact_selection(set()) == ""
    assert compact_selection({0}) == "1"
    assert compact_selection({0, 1, 2}) == "1-3"
    assert compact_selection({0, 2}) == "1,3"
    assert compact_selection({0, 1, 2, 5, 6, 9}) == "1-3,6-7,10"


def test_parse_selection_string_inverse_of_compact():
    cases = ["1", "1-3", "1,3", "1-3,6-7,10"]
    for s in cases:
        indices = parse_selection_string(s, 100)
        assert compact_selection(indices) == s


def test_parse_selection_string_handles_garbage():
    assert parse_selection_string("", 5) == set()
    assert parse_selection_string("99", 5) == set()  # out of range
    assert parse_selection_string("abc,1", 5) == {0}
    assert parse_selection_string("1-bad", 5) == set()


def test_chat_key_for_tracking_priority():
    assert _chat_key_for_tracking({"composer_id": "cid", "_session_id": "sid"}) == "cid"
    assert _chat_key_for_tracking({"_session_id": "sid"}) == "sid"
    assert (
        _chat_key_for_tracking({"_source": "claude", "name": "foo"}) == "claude:foo"
    )


def test_chat_tracking_lookup_keys_includes_legacy_fallback():
    chat = {"composer_id": "cid", "_source": "cursor", "name": "foo"}
    keys = _chat_tracking_lookup_keys(chat)
    assert keys[0] == "cid"
    assert "cursor:foo" in keys


def test_removed_marker_payload_captures_last_updated():
    chat = {"lastUpdatedAt": 12345}
    p = _removed_marker_payload(chat)
    assert p["removed"] is True
    assert p["removed_chat_last_updated_at"] == 12345


def test_removed_marker_is_active_with_timestamp():
    chat = {"lastUpdatedAt": 100}
    # Marker captured at ts=100; chat still at 100 → marker active
    assert _removed_marker_is_active({"removed": True, "removed_chat_last_updated_at": 100}, chat)
    # Chat advanced to 200 → marker expired
    chat["lastUpdatedAt"] = 200
    assert not _removed_marker_is_active(
        {"removed": True, "removed_chat_last_updated_at": 100}, chat
    )


def test_removed_marker_is_active_legacy_no_timestamp():
    chat = {"lastUpdatedAt": 100}
    # Legacy marker without timestamp: active when chat not currently modified
    assert _removed_marker_is_active({"removed": True}, chat, is_modified=False)
    assert not _removed_marker_is_active({"removed": True}, chat, is_modified=True)


def test_removed_marker_is_active_rejects_non_removed():
    assert not _removed_marker_is_active({"removed": False}, {"lastUpdatedAt": 100})
    assert not _removed_marker_is_active({}, {"lastUpdatedAt": 100})
    assert not _removed_marker_is_active(None, {"lastUpdatedAt": 100})


def test_build_row_map_inserts_entries_for_expanded_chats():
    chats = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    expanded = {1}
    entry_cache = {1: [{"q": 1}, {"q": 2}]}
    rows = _build_row_map(chats, expanded, entry_cache)
    assert [r["type"] for r in rows] == ["chat", "chat", "entry", "entry", "chat"]
    assert rows[0]["chat_idx"] == 0
    assert rows[1]["chat_idx"] == 1
    assert rows[2] == {"type": "entry", "chat_idx": 1, "entry_idx": 0, "entry": {"q": 1}}
    assert rows[4]["chat_idx"] == 2
