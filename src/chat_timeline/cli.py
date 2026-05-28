"""``timeline`` console entry point.

Two new top-level subcommands wrap the legacy CLI:

  * ``timeline init``   — scaffold this project for chat-timeline.
  * ``timeline deinit`` — remove the pre-commit hook and managed gitignore
                          block (output data is left in place).

Anything else is forwarded to the legacy argparse in
``chat_timeline._legacy.main``.
"""

from __future__ import annotations

import sys

from chat_timeline import __version__


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

    # Fall through to the legacy CLI (same flags as before).
    from chat_timeline._legacy.main import main as legacy_main

    legacy_main()


if __name__ == "__main__":
    main()
