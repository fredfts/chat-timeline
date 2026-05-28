#!/usr/bin/env bash
# One-shot installer for chat-timeline on macOS/Linux/WSL.
# Usage:  ./install.sh                         (from a clone)
#         curl -fsSL <raw-url>/install.sh | bash
set -euo pipefail

need() { command -v "$1" >/dev/null 2>&1; }

if ! need python3 && ! need python; then
  echo "error: python 3.9+ required. Install Python first."
  echo "  macOS:  brew install python"
  echo "  Linux:  use your package manager (apt/dnf/pacman/…)"
  exit 1
fi

PY="$(command -v python3 || command -v python)"
ver=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$ver" in
  3.9|3.1[0-9]) ;;
  *) echo "warning: detected Python $ver; chat-timeline requires 3.9+" ;;
esac

if need pipx; then
  pipx install --force chat-timeline
else
  echo "pipx not found — installing into the user site with pip"
  "$PY" -m pip install --user --upgrade chat-timeline
fi

if ! need timeline; then
  echo
  echo "note: 'timeline' is not on PATH. Add your user-base bin dir:"
  "$PY" -m site --user-base
  exit 0
fi

if [ -d .git ] || git rev-parse --show-toplevel >/dev/null 2>&1; then
  timeline init
else
  echo
  echo "Skipped 'timeline init' — current directory is not a git repository."
  echo "Run 'timeline init' from inside your project to finish setup."
fi
