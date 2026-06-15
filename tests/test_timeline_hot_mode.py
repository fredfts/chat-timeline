"""Tests for the tri-state ``hot_mode`` filter in ``generate_timeline``.

  off   -> every turn lands
  entry -> only turns that touched a file land
  chat  -> a chat lands in full iff at least one of its turns touched a file

``chat_timeline.timeline`` is imported at fixture time, not module level, as a
suite convention: importing it snapshots ``chat_timeline._state`` path globals,
so deferring keeps collection from binding them to the real repo. The fixture
redirects every output dir to a tmp location so generation never writes into
the real project.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _chat_md(title, composer_id, turns):
    """Render a staged chat .md the way the exporters do.

    ``turns`` is a list of (question_text, tool_detail). A tool_detail like
    ``"Edit: foo.py"`` marks a file-changing (hot) turn; ``"Read: foo.py"``
    is cold; ``None`` means the turn made no tool calls at all (also cold).
    """
    lines = [
        "---",
        f"title: {title}",
        "source: claude",
        f"composer_id: {composer_id}",
        "---",
        "",
    ]
    for i, (qtext, tool_detail) in enumerate(turns, start=1):
        ts_q = f"2026-01-01 10:0{i}:00"
        ts_a = f"2026-01-01 10:0{i}:01"
        lines += [f"## Q{i} [claude-x] {ts_q}", f"> {qtext}", ""]
        lines += [f"## A{i} [claude-x] {ts_a}", "ok", ""]
        if tool_detail is not None:
            lines += ["### Tool calls (1)", f"- `{tool_detail}` (ok) {ts_a}", ""]
    return "\n".join(lines) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point timeline's dir globals at tmp dirs and stage two chats.

    Chat A: Q1 hot (Edit), Q2 cold (Read)  -> a mixed chat
    Chat B: Q1 cold (Read)                 -> a fully cold chat

    Returns a namespace with the timeline module plus the staged/contents dirs.
    """
    from chat_timeline import timeline

    staged_dir = tmp_path / "staged"
    contents_dir = tmp_path / "contents"
    history_dir = tmp_path / "history"
    used_dir = tmp_path / "used"
    for d in (staged_dir, contents_dir, history_dir, used_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(timeline, "STAGED_DIR", staged_dir)
    monkeypatch.setattr(timeline, "CONTENTS_DIR", contents_dir)
    monkeypatch.setattr(timeline, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(timeline, "USED_DIR", used_dir)
    # Keep the test hermetic — don't shell out to git for header metadata.
    monkeypatch.setattr(timeline, "get_head_short", lambda: "deadbee")
    monkeypatch.setattr(timeline, "get_current_branch", lambda: "main")

    (staged_dir / "a_chat.md").write_text(
        _chat_md("Chat A", "chatA", [("alpha hot", "Edit: a.py"), ("alpha cold", "Read: a.py")]),
        encoding="utf-8",
    )
    (staged_dir / "b_chat.md").write_text(
        _chat_md("Chat B", "chatB", [("beta cold", "Read: b.py")]),
        encoding="utf-8",
    )
    return SimpleNamespace(timeline=timeline, contents=contents_dir, staged=staged_dir)


def _landed_prompts(contents_dir):
    data = json.loads((contents_dir / "timeline.json").read_text(encoding="utf-8"))
    return sorted(e["prompt"] for e in data["entries"])


def test_hot_off_keeps_every_turn(env):
    env.timeline.generate_timeline(hot_mode="off")
    assert _landed_prompts(env.contents) == ["alpha cold", "alpha hot", "beta cold"]


def test_hot_default_is_off(env):
    # The default argument must behave like "off".
    env.timeline.generate_timeline()
    assert _landed_prompts(env.contents) == ["alpha cold", "alpha hot", "beta cold"]


def test_hot_entry_keeps_only_file_changing_turns(env):
    env.timeline.generate_timeline(hot_mode="entry")
    assert _landed_prompts(env.contents) == ["alpha hot"]


def test_hot_chat_keeps_whole_chat_when_any_turn_is_hot(env):
    env.timeline.generate_timeline(hot_mode="chat")
    # Chat A has a hot turn -> both of its turns land; Chat B is fully cold.
    assert _landed_prompts(env.contents) == ["alpha cold", "alpha hot"]


def test_hot_entry_force_add_overrides_cold(env):
    # Force-add a cold turn's fingerprint: it should land despite entry mode.
    meta, turns = env.timeline.parse_chat_export(env.staged / "b_chat.md")
    beta_fp = env.timeline.entry_fingerprint(meta, turns[0])
    env.timeline.generate_timeline(hot_mode="entry", force_add_fingerprints={beta_fp})
    assert "beta cold" in _landed_prompts(env.contents)
