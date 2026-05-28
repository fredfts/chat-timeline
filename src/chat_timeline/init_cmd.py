"""``timeline init`` / ``timeline deinit`` — scaffold or tear down a project.

``init`` is idempotent: it can be run repeatedly on the same project; it never
overwrites user data and only refreshes managed sections of files it owns.
"""

from __future__ import annotations

import argparse
import importlib.resources as resources
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from chat_timeline.paths import find_project_root, find_timeline_home

GITIGNORE_OPEN = "# >>> chat-timeline >>>"
GITIGNORE_CLOSE = "# <<< chat-timeline <<<"
GITIGNORE_BODY = (
    "/timeline/__pycache__/\n"
    "/timeline/.precommit_state.json\n"
)
TIMELINE_GITIGNORE = (
    "/__pycache__/\n"
    "\n"
    "/chats\n"
    "/sessions\n"
    "/contents\n"
    ".precommit_state.json\n"
)
PRECOMMIT_STATE_DEFAULT = {
    "enabled": False,
    "last_run_ts": 0,
    "tracked_chats": {},
    "hot_only": False,
}


def _print(msg: str) -> None:
    print(f"[timeline init] {msg}")


def _ensure_dirs(home: Path) -> None:
    for sub in ("", "chats", "chats/staged", "chats/used",
                "sessions", "contents", "timeline"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    _print(f"directories ready under {home}")


def _write_if_missing(path: Path, content: str, *, label: str) -> None:
    if path.exists():
        return
    path.write_text(content, encoding="utf-8")
    _print(f"wrote {label}: {path}")


def _ship_llm_instructions(home: Path) -> None:
    target = home / "LLM_INSTRUCTIONS.md"
    if target.exists():
        return
    src = resources.files("chat_timeline.data") / "LLM_INSTRUCTIONS.md"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    _print(f"wrote LLM_INSTRUCTIONS: {target}")


def _update_project_gitignore(project_root: Path, home_rel: str) -> None:
    """Idempotently maintain a managed block in <project>/.gitignore."""
    gitignore = project_root / ".gitignore"
    body = (
        f"/{home_rel}/__pycache__/\n"
        f"/{home_rel}/.precommit_state.json\n"
    )
    block = f"{GITIGNORE_OPEN}\n{body}{GITIGNORE_CLOSE}\n"
    if not gitignore.exists():
        gitignore.write_text(block, encoding="utf-8")
        _print(f"created .gitignore with managed block: {gitignore}")
        return
    content = gitignore.read_text(encoding="utf-8")
    if GITIGNORE_OPEN in content:
        # Replace existing managed block (body may have changed).
        start = content.index(GITIGNORE_OPEN)
        end = content.index(GITIGNORE_CLOSE, start) + len(GITIGNORE_CLOSE)
        new = content[:start] + block.rstrip("\n") + content[end:]
        if new != content:
            gitignore.write_text(new, encoding="utf-8")
            _print(f"refreshed managed block in {gitignore}")
        return
    sep = "" if content.endswith("\n") else "\n"
    gitignore.write_text(content + sep + "\n" + block, encoding="utf-8")
    _print(f"appended managed block to {gitignore}")


def _remove_project_gitignore_block(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return
    content = gitignore.read_text(encoding="utf-8")
    if GITIGNORE_OPEN not in content:
        return
    start = content.index(GITIGNORE_OPEN)
    # Walk back over any blank line separating the block from the prior content.
    while start > 0 and content[start - 1] == "\n":
        start -= 1
    end = content.index(GITIGNORE_CLOSE) + len(GITIGNORE_CLOSE)
    if end < len(content) and content[end] == "\n":
        end += 1
    new = content[:start] + content[end:]
    if not new.strip():
        gitignore.unlink()
        _print(f"removed empty {gitignore}")
    else:
        gitignore.write_text(new, encoding="utf-8")
        _print(f"removed managed block from {gitignore}")


def _is_git_repo(project_root: Path) -> bool:
    return (project_root / ".git").exists()


def _parse_init_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="timeline init",
        description="Scaffold this project for chat-timeline.",
    )
    parser.add_argument(
        "--no-git", action="store_true",
        help="Allow init outside a git repository (skips hook install).",
    )
    parser.add_argument(
        "--no-hook", action="store_true",
        help="Skip pre-commit hook installation.",
    )
    return parser.parse_args(list(argv))


def run_init(argv: Sequence[str]) -> None:
    args = _parse_init_args(argv)

    project_root = find_project_root()
    home = find_timeline_home(project_root)

    is_git = _is_git_repo(project_root)
    if not is_git and not args.no_git:
        print(
            "error: not inside a git repository.\n"
            f"  cwd: {Path.cwd()}\n"
            f"  resolved project root: {project_root}\n"
            "Run `git init` first, or pass --no-git to scaffold anyway.",
            file=sys.stderr,
        )
        sys.exit(2)

    _print(f"project root: {project_root}")
    _print(f"timeline home: {home}")

    _ensure_dirs(home)
    _write_if_missing(home / ".gitignore", TIMELINE_GITIGNORE,
                      label="timeline/.gitignore")
    _write_if_missing(
        home / ".precommit_state.json",
        json.dumps(PRECOMMIT_STATE_DEFAULT, indent=2) + "\n",
        label=".precommit_state.json",
    )
    _ship_llm_instructions(home)

    try:
        home_rel = home.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        home_rel = home.name
    _update_project_gitignore(project_root, home_rel)

    hook_installed = False
    if is_git and not args.no_hook:
        # Reach into legacy installer; it picks up paths through env / module
        # state set above.
        os.environ.setdefault("TIMELINE_PROJECT_ROOT", str(project_root))
        os.environ.setdefault("TIMELINE_HOME", str(home))
        from chat_timeline._legacy.main import _install_hook  # noqa: WPS433
        _install_hook()
        hook_installed = True

    print()
    _print("done.")
    print("Next steps:")
    print("  timeline           # interactive selector, exports + session + timeline")
    print("  timeline -t        # rebuild timeline only")
    if hook_installed:
        print("  press `p` in the selector to enable pre-commit auto mode")
    elif is_git:
        print("  (pre-commit hook skipped — run `timeline init` without --no-hook to install)")
    else:
        print("  (no git repo — pre-commit hook not installed)")


def _parse_deinit_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="timeline deinit",
        description=(
            "Remove the pre-commit hook and the managed .gitignore block. "
            "Output data (chats/, sessions/, contents/, timeline.md) is left "
            "in place."
        ),
    )
    return parser.parse_args(list(argv))


def run_deinit(argv: Sequence[str]) -> None:
    _parse_deinit_args(argv)
    project_root = find_project_root()
    home = find_timeline_home(project_root)

    os.environ.setdefault("TIMELINE_PROJECT_ROOT", str(project_root))
    os.environ.setdefault("TIMELINE_HOME", str(home))
    from chat_timeline._legacy.main import _uninstall_hook  # noqa: WPS433
    _uninstall_hook()
    _remove_project_gitignore_block(project_root)
    _print("done. output data preserved under " + str(home))
