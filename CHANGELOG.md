# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [0.1.1] — 2026-05-28

Hotfix patch surfaced during the `mascat` migration to `timeline …`.

### Fixed
- `timeline deinit` now removes the hook entirely when a pre-commit file
  contains BOTH a marker-delimited timeline section AND a separate
  standalone timeline block; previously the marker path stripped the
  section and returned early, leaving the standalone block behind.
- Standalone-hook detection in `_uninstall_hook` now matches the legacy
  install form where `SCRIPT="$TOPLEVEL/timeline/main.py"` and
  `python3 "$SCRIPT" -x` live on separate lines (so the literal
  `timeline/main.py -x` never appears as a substring). Detection now
  recognises the `# chat-timeline pre-commit hook` /
  `# timeline pre-commit hook` headers and AND-matches the path against
  the `-x`/`-p` flag.
- `timeline` invoked outside a git repository now refuses to run instead
  of silently falling back to cwd. The previous behavior let the
  Codex/Claude source scanners' `paths_overlap` rule scoop up every
  session whose recorded cwd was anywhere under the launch directory.
  Pass `--no-git` or set `TIMELINE_PROJECT_ROOT` to override.

## [0.1.0] — 2026-05-28

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
