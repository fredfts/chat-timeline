"""CLI smoke tests."""

from __future__ import annotations

import sys

import chat_timeline
from chat_timeline.cli import main


def test_version(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["timeline", "--version"])
    main()
    assert chat_timeline.__version__ in capsys.readouterr().out
