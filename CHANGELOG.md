# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [0.1.0] — Unreleased

Initial extraction from `mascat/timeline/`. Behavior of the legacy CLI is
preserved.

### Added
- `timeline` console entry point (`chat_timeline.cli:main`).
- `python -m chat_timeline` invocation.
- `timeline init` — idempotent one-shot project setup: creates the
  `timeline/` directory tree, writes `.gitignore` entries, ships
  `LLM_INSTRUCTIONS.md`, installs the git pre-commit hook.
- `timeline deinit` — removes the pre-commit hook and managed `.gitignore`
  block; leaves exported data in place.
- Project-root and timeline-home auto-detection
  (`TIMELINE_PROJECT_ROOT`, `TIMELINE_HOME` env vars).
- Pre-commit hook prefers the installed `timeline` entry point with
  fallbacks to `python -m chat_timeline` and `wsl.exe timeline`.

### Changed
- `PROJECT_DIR` no longer derives from `Path(__file__).parent`; it now
  resolves from the current working directory's git toplevel.

### Migration from `mascat/timeline/main.py`
- Install: `pipx install chat-timeline`.
- Run `timeline init` in your project root (it detects the existing
  `timeline/` and only refreshes managed sections).
- Delete the vendored `mascat/timeline/{main,claude,cursor,codex}.py`.
- The on-disk format (`chats/used/*.json`, `contents/timeline.json`,
  `timeline.md`) is unchanged — your history carries over.
