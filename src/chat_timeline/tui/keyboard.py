"""Cross-platform keypress reader for the interactive selector.

Wraps msvcrt (Windows) and termios+tty (POSIX) behind a small, identical
API:

    HOLD_SECONDS           — duration that counts as "held"
    read_key()             — one keypress, normalized to "up"/"down"/...
    check_hold(char)       — block up to HOLD_SECONDS waiting for ``char`` to release
    check_hold_with_feedback(char, render_cb) — same with a periodic redraw
    flush_stdin()          — drain buffered keypresses
"""

from __future__ import annotations

import select as _select
import sys
import time as _time

HOLD_SECONDS = 3.0

# Use ``sys.platform`` (not ``platform.system()``) so mypy narrows the
# branches correctly — only one of {msvcrt, termios/tty} is loaded per
# platform, and mypy needs the static check to know which to type-check.
if sys.platform == "win32":
    import msvcrt

    def read_key() -> str:
        """Read one logical keypress via msvcrt. Returns a string identifier."""
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {
                "H": "up",
                "P": "down",
                "I": "pgup",
                "Q": "pgdn",
                "G": "home",
                "O": "end",
                "K": "left",
                "M": "right",
            }.get(code, "")
        if ch == "\r":
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x08":
            return "backspace"
        if ch == " ":
            return "space"
        return ch

    def check_hold(char: str, duration: float = HOLD_SECONDS) -> bool:
        """Check if ``char`` is held for ``duration`` seconds (Windows)."""
        deadline = _time.monotonic() + duration
        while _time.monotonic() < deadline:
            if not msvcrt.kbhit():
                _time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            if ch == char:
                continue  # auto-repeat of same key
            return False  # different key pressed
        return True

else:
    import termios
    import tty

    def read_key() -> str:
        """Read one logical keypress via termios. Returns a string identifier."""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 in "ABCDHF":
                        return {
                            "A": "up",
                            "B": "down",
                            "C": "right",
                            "D": "left",
                            "H": "home",
                            "F": "end",
                        }.get(ch3, "")
                    if ch3 in "56":
                        sys.stdin.read(1)  # consume the ~
                        return {"5": "pgup", "6": "pgdn"}.get(ch3, "")
                    if ch3 in "14":
                        sys.stdin.read(1)  # consume the ~
                        return {"1": "home", "4": "end"}.get(ch3, "")
                return "esc"
            if ch in ("\r", "\n"):
                return "enter"
            if ch in ("\x7f", "\x08"):
                return "backspace"
            if ch == " ":
                return "space"
            if ch == "\t":
                return "\t"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def check_hold(char: str, duration: float = HOLD_SECONDS) -> bool:
        """Check if ``char`` is held for ``duration`` seconds (Linux/WSL)."""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            deadline = _time.monotonic() + duration
            while _time.monotonic() < deadline:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = _select.select([fd], [], [], min(0.05, remaining))
                if ready:
                    ch = sys.stdin.read(1)
                    if ch == char:
                        continue  # auto-repeat
                    return False  # different key
            return True
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def flush_stdin() -> None:
    """Drain any buffered characters from stdin."""
    if sys.platform == "win32":
        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ready, _, _ = _select.select([fd], [], [], 0.0)
                if ready:
                    sys.stdin.read(1)
                else:
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def check_hold_with_feedback(char: str, render_cb, duration: float = HOLD_SECONDS) -> bool:
    """Check for a held key with visual feedback via render_cb(elapsed).

    render_cb is called every ~100ms with the elapsed time.
    Returns True if held for the full duration, False if released early.
    After returning, stdin is flushed to prevent buffered auto-repeat
    chars from being processed as new keypresses.
    """
    start = _time.monotonic()
    result = False
    while True:
        elapsed = _time.monotonic() - start
        if elapsed >= duration:
            result = True
            break
        render_cb(elapsed)
        if sys.platform == "win32":
            _time.sleep(0.1)
            if not msvcrt.kbhit():
                break
            ch = msvcrt.getwch()
            if ch != char:
                break
        else:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ready, _, _ = _select.select([fd], [], [], 0.1)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch != char:
                        break
                else:
                    break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    flush_stdin()
    return result
