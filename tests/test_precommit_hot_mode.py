"""Tests for the tri-state hot filter plumbing in ``precommit``:
the ``next_hot_mode`` cycle and the ``hot_only`` -> ``hot_mode`` migration
performed by ``_load_precommit_state``.

``chat_timeline.precommit`` is imported at fixture time, not module level, as a
suite convention: importing it snapshots ``chat_timeline._state`` path globals,
so deferring keeps collection from binding them to the real repo.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def pc(tmp_path, monkeypatch):
    from chat_timeline import precommit

    # Redirect state reads/writes to a tmp file so nothing touches the real
    # project's timeline/.precommit_state.json.
    monkeypatch.setattr(precommit, "PRECOMMIT_STATE", tmp_path / ".precommit_state.json")
    return precommit


def test_next_hot_mode_cycles_off_chat_entry(pc):
    assert pc.next_hot_mode("off") == "chat"
    assert pc.next_hot_mode("chat") == "entry"
    assert pc.next_hot_mode("entry") == "off"


def test_next_hot_mode_unknown_falls_back_to_chat(pc):
    assert pc.next_hot_mode("bogus") == "chat"
    assert pc.next_hot_mode(None) == "chat"


def _load_with(pc, payload):
    pc.PRECOMMIT_STATE.write_text(json.dumps(payload), encoding="utf-8")
    return pc._load_precommit_state()


def test_load_defaults_to_off_when_file_absent(pc):
    st = pc._load_precommit_state()
    assert st["hot_mode"] == "off"
    assert "hot_only" not in st


def test_load_migrates_legacy_hot_only_true_to_entry(pc):
    st = _load_with(pc, {"enabled": True, "hot_only": True})
    assert st["hot_mode"] == "entry"
    assert "hot_only" not in st


def test_load_migrates_legacy_hot_only_false_to_off(pc):
    st = _load_with(pc, {"hot_only": False})
    assert st["hot_mode"] == "off"
    assert "hot_only" not in st


def test_load_prefers_explicit_hot_mode_over_legacy(pc):
    st = _load_with(pc, {"hot_mode": "chat", "hot_only": True})
    assert st["hot_mode"] == "chat"
    assert "hot_only" not in st


def test_load_sanitizes_invalid_hot_mode(pc):
    st = _load_with(pc, {"hot_mode": "garbage"})
    assert st["hot_mode"] == "off"
