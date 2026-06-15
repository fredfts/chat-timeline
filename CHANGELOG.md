# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [0.3.0] — 2026-06-15

### Changed
- The selector's `h` key now cycles a tri-state **hot** filter instead of a
  boolean toggle: `off` → `chat` → `entry`. `entry` keeps only the turns that
  touched a file (the previous "hot ON" behavior); `chat` keeps a whole chat
  whenever at least one of its turns touched a file. The persisted setting
  moves from `hot_only` (bool) to `hot_mode` (string); existing state files
  are migrated on load (`hot_only: true` → `"entry"`, `false` → `"off"`).

### Fixed
- Pre-commit hook install/uninstall now resolve `<project>/.git/hooks/pre-commit`
  per call instead of using the path frozen into `chat_timeline._state` at
  import time, so they always target the current project even when the cwd or
  `TIMELINE_PROJECT_ROOT` changed after import. Removes a test-isolation hazard
  where importing the package could pin the hook path to the wrong repo.

## [0.2.0] — 2026-05-28

Major refactor: the 3,430-line vendored monolith at
`src/chat_timeline/_legacy/main.py` is gone. Code is now organized into
single-purpose modules and the package is fully lint/type-checked. The
on-disk format (`chats/`, `sessions/`, `contents/`, `timeline.md`) and
the CLI are unchanged — outputs are byte-identical to v0.1.1.

### Added
- Source plugin layer: `chat_timeline.sources.{base,cursor,claude,codex}`
  with a `Source` protocol and a documented chat-dict schema. Sources
  now take an optional `scope` parameter so callers can narrow the
  cwd-overlap match without affecting where exports land.
- Mtime-keyed JSONL cache (`chat_timeline.sources._cache.JSONLCache`)
  under `<HISTORY_DIR>/.cache/sessions/<source>/`. Warm scans skip the
  full JSONL parse for matched sessions; entries auto-invalidate when
  the source file changes.
- Two-phase scan for Codex: read the first JSONL line for `payload.cwd`
  and skip non-overlapping sessions without the expensive full parse.
- Test coverage: 76 cases across markdown, git_utils, cache, sources,
  TUI helpers, paths, init, and the cli guard.

### Changed
- `chat_timeline.markdown`, `.git_utils`, `.session`, `.timeline`,
  `.precommit`, `.tui.{keyboard,selector}`, `.app`, and `._state` carved
  out of the legacy monolith. Each takes its dependencies explicitly
  (or via the `_state` path globals) rather than reading shared module-
  level state.
- `paths._git_toplevel` and `paths.find_project_root` are now memoized
  (`functools.cache`). The v0.1.1 CLI guard double-call no longer pays
  the ~70ms subprocess cost twice.
- `init_cmd.TIMELINE_GITIGNORE` now ignores `/.cache/`.

### Removed
- `src/chat_timeline/_legacy/` (and the `[tool.ruff].extend-exclude` /
  `[tool.mypy].exclude` entries that fenced it off).

### Performance
- Fixes the v0.1.0 scan-perf regression in projects with many Codex
  sessions: the peek-then-cache flow makes wide-scope scans cheap. Cold
  runs are slightly faster than v0.1.1 thanks to the lru-cached
  `_git_toplevel`; subsequent runs avoid re-parsing matched JSONLs.

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
