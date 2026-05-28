"""Tests for chat_timeline.sources.claude — list_chats + slug matching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chat_timeline.sources import claude as claude_mod


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def claude_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "claude_root"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setattr(claude_mod, "_CLAUDE_STORAGE_ROOTS_CACHE", (root,))
    # Reset the project-storages cache so each test resolves slugs fresh.
    monkeypatch.setattr(claude_mod, "_PROJECT_STORAGES_CACHE", {})
    return root


def test_project_dir_to_slugs_includes_native_and_drive_variants(tmp_path):
    slugs = claude_mod.project_dir_to_slugs(tmp_path / "myproj")
    # At minimum the native slug form should be present.
    resolved = (tmp_path / "myproj").resolve()
    native_slug = str(resolved).replace("/", "-").replace("\\", "-")
    assert native_slug in slugs


def test_list_chats_finds_session_in_matching_project_storage(claude_root, tmp_path):
    project = tmp_path / "myproj"
    project.mkdir()
    slug = claude_mod.project_dir_to_slugs(project)[0]
    storage = claude_root / "projects" / slug
    sid = "11111111-1111-1111-1111-111111111111"
    _write_jsonl(
        storage / f"{sid}.jsonl",
        [
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": "hello world"},
            },
            {
                "type": "assistant",
                "sessionId": sid,
                "timestamp": "2026-01-01T00:00:30Z",
                "message": {
                    "model": "claude-test",
                    "id": "msg-1",
                    "content": [{"type": "text", "text": "hi back"}],
                },
            },
        ],
    )

    chats = claude_mod.list_chats(project)

    assert len(chats) == 1
    assert chats[0]["_session_id"] == sid
    assert chats[0]["_model"] == "claude-test"


def test_list_chats_drops_sessions_with_only_system_user_text(claude_root, tmp_path):
    project = tmp_path / "myproj"
    project.mkdir()
    slug = claude_mod.project_dir_to_slugs(project)[0]
    storage = claude_root / "projects" / slug
    sid = "22222222-2222-2222-2222-222222222222"
    _write_jsonl(
        storage / f"{sid}.jsonl",
        [
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": "<command-name>foo</command-name>"},
            },
        ],
    )

    chats = claude_mod.list_chats(project)
    assert chats == []


def test_list_chats_collapses_clone_chains(claude_root, tmp_path):
    """Two sessions sharing the first 3 user-message UUIDs collapse to one."""
    project = tmp_path / "myproj"
    project.mkdir()
    slug = claude_mod.project_dir_to_slugs(project)[0]
    storage = claude_root / "projects" / slug

    shared = [f"uuid-{i}" for i in range(3)]
    sid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def _session_msgs(sid: str, extra: int) -> list[dict]:
        base = [
            {
                "type": "user",
                "sessionId": sid,
                "uuid": u,
                "timestamp": f"2026-01-01T00:0{i}:00Z",
                "message": {"content": f"msg {i}"},
            }
            for i, u in enumerate(shared)
        ]
        for i in range(extra):
            base.append(
                {
                    "type": "user",
                    "sessionId": sid,
                    "uuid": f"{sid}-{i}",
                    "timestamp": f"2026-01-01T00:1{i}:00Z",
                    "message": {"content": f"extra {i}"},
                }
            )
        return base

    # sid_a has 5 user messages; sid_b has 3. They share the first 3 UUIDs.
    _write_jsonl(storage / f"{sid_a}.jsonl", _session_msgs(sid_a, extra=2))
    _write_jsonl(storage / f"{sid_b}.jsonl", _session_msgs(sid_b, extra=0))

    chats = claude_mod.list_chats(project)

    # sid_a ranks higher (more user messages), so sid_b is dropped as a clone.
    assert {c["_session_id"] for c in chats} == {sid_a}
