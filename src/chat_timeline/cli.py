"""``timeline`` console entry point.

Two top-level subcommands wrap the rest of the CLI:

  * ``timeline init``   — scaffold this project for chat-timeline.
  * ``timeline deinit`` — remove the pre-commit hook and managed gitignore
                          block (output data is left in place).

Anything else is forwarded to ``chat_timeline.app:main``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from chat_timeline import __version__


def _require_git_repo_or_explicit_root(argv: list[str]) -> list[str]:
    """Guard the CLI against running outside a git repo.

    Without this, ``find_project_root()`` falls back to cwd and the source
    scanners' path-overlap rules match every Codex/Claude session whose
    recorded cwd is *under* that cwd — e.g. running ``timeline`` from
    ``C:\\Users\\Fred\\`` would scoop up every project's history.

    Strips ``--no-git`` from argv before returning so the downstream
    argparse (which doesn't know that flag) doesn't reject it.
    """
    if "--no-git" in argv:
        return [a for a in argv if a != "--no-git"]
    if os.environ.get("TIMELINE_PROJECT_ROOT"):
        return argv
    from chat_timeline.paths import _git_toplevel

    if _git_toplevel(Path.cwd()) is not None:
        return argv
    print(
        "error: not inside a git repository.\n"
        f"  cwd: {Path.cwd()}\n"
        "Run `timeline` from inside a git repo, set TIMELINE_PROJECT_ROOT, or\n"
        "pass --no-git to scan from cwd anyway (may include unrelated chats).",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    argv = sys.argv[1:]

    if argv and argv[0] in ("-V", "--version"):
        print(f"chat-timeline {__version__}")
        return

    if argv and argv[0] == "init":
        from chat_timeline.init_cmd import run_init

        run_init(argv[1:])
        return

    if argv and argv[0] in ("deinit", "uninstall"):
        from chat_timeline.init_cmd import run_deinit

        run_deinit(argv[1:])
        return

    argv = _require_git_repo_or_explicit_root(argv)
    sys.argv = [sys.argv[0], *argv]

    from chat_timeline.app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
