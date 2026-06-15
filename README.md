# chat-timeline

Export chat history from **Cursor**, **Claude Code**, and **Codex** and build a
git-tracked, LLM-friendly project timeline.

The result is three artifacts that live inside your repo:

- `timeline/timeline.md` — chronological, compact, ready for an LLM to read.
- `timeline/contents/timeline.json` — referenced content keyed by entry ID.
- `timeline/sessions/session.md` — full chat exports + git index context.

A built-in git pre-commit hook keeps the timeline in sync with your commits
automatically.

Works on Windows, macOS, and Linux. Pure stdlib — no runtime dependencies.

---

## Install + configure (one command)

```bash
pipx install chat-timeline && timeline init
```

`pipx` not available? The equivalent is:

```bash
python -m pip install --user chat-timeline && timeline init
```

`timeline init` is idempotent and does everything for you:

1. Resolves the project root (`git rev-parse --show-toplevel`).
2. Creates `timeline/` with `chats/`, `sessions/`, `contents/`, and the
   archive folder.
3. Writes `timeline/.gitignore` and a managed block in the project's
   `.gitignore`.
4. Installs `LLM_INSTRUCTIONS.md` for downstream prompts.
5. Installs the git pre-commit hook (calls `timeline -p` on each commit).

To reverse: `timeline deinit` removes the hook and the managed `.gitignore`
block. Your exported data is left untouched.

---

## Daily use

```bash
timeline                  # interactive selector → exports + session + timeline
timeline claude           # claude only
timeline cursor 1-5       # cursor chats #1..#5
timeline -t               # rebuild timeline only (incremental)
timeline -t -rt           # rebuild timeline from scratch
timeline -p               # pre-commit standalone (auto-detects modified chats)
```

Equivalent: `python -m chat_timeline …`.

Full flag reference: `timeline --help`.

### Interactive selector keys

| Key | Action |
|---|---|
| `↑ ↓` | move pointer |
| `PgUp PgDn` | page |
| `Home End` | jump to first/last |
| `Space` | toggle chat (or entry, when expanded) |
| `→` | expand chat to entries |
| `←` | collapse / jump back to parent |
| `a` | toggle select-all |
| `t` | cycle tracking mode (chat) or toggle exclude/force-add (entry). Hold 3s to clear tracking. |
| `p` | toggle pre-commit auto mode |
| `o` | toggle timeline archive rotation for this run |
| `h` | cycle hot filter: off → chat (keep chats with any file-changing turn) → entry (keep only file-changing turns) |
| `Tab` | switch list ↔ numeric input |
| `Enter` | confirm · `Esc` cancel |

---

## What it reads (per source)

| Source | Storage location | Notes |
|---|---|---|
| Cursor | `%APPDATA%/Cursor/User/workspaceStorage/` (Win), `~/Library/Application Support/Cursor/User/workspaceStorage/` (mac), `~/.config/Cursor/User/workspaceStorage/` (linux) | SQLite |
| Claude Code | `~/.claude/projects/<slug>/*.jsonl` | Cross-runtime: Windows reads WSL data, WSL reads Windows data |
| Codex | `~/.codex/sessions/**/*.jsonl` | Same cross-runtime discovery |

Nothing is transmitted anywhere. Everything runs locally on your machine.

---

## Configuration

| Env var | Effect |
|---|---|
| `TIMELINE_PROJECT_ROOT` | Override the auto-detected git toplevel. |
| `TIMELINE_HOME` | Override the default `<project>/timeline` location. |
| `TIMELINE_AMEND` | Set by the pre-commit hook on `git commit --amend`. |

### Scan cache

To keep wide-scope scans fast, parsed Codex session metadata is cached
under `<TIMELINE_HOME>/.cache/sessions/codex/<sha1>.json`, keyed by JSONL
mtime + size. The cache is regenerated on demand — safe to delete, and
already covered by the managed `.gitignore` block.

---

## Pre-commit hook details

The hook is a POSIX shell script that:

1. Detects `git commit --amend` from the parent process's `cmdline`.
2. Calls the installed `timeline` entry point, with fallbacks to
   `python -m chat_timeline` and `wsl.exe timeline`.
3. Stages any updated timeline files into the commit.

On Windows it runs under Git Bash (which ships with Git for Windows).

---

## Status

v0.2.0 ships the modular layout (`sources/`, `tui/`, `markdown.py`,
`git_utils.py`, `session.py`, `timeline.py`, `precommit.py`, `app.py`,
`_state.py`) with no `_legacy/` block — the CLI and on-disk format are
unchanged from v0.1.x.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports welcome — please include
the output of `timeline --version` and your OS + Python version.

## License

MIT. See [LICENSE](LICENSE).
