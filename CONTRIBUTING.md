# Contributing

Thanks for your interest. The codebase is small and stdlib-only.

## Local setup

```bash
git clone https://github.com/espmfred/chat-timeline.git
cd chat-timeline
python -m pip install -e ".[dev]"
pre-commit install
```

Verify the CLI works:

```bash
timeline --version
timeline --help
```

## Running tests

```bash
pytest
```

## Lint and types

```bash
ruff check
ruff format --check
mypy
```

CI runs the same on Ubuntu, Windows, and macOS for every supported Python
version (3.9 → 3.13).

## Code layout

- `src/chat_timeline/cli.py` — top-level dispatcher (`init`, `deinit`, fall
  through to legacy).
- `src/chat_timeline/paths.py` — project root / timeline home resolution.
- `src/chat_timeline/init_cmd.py` — `timeline init` / `timeline deinit`.
- `src/chat_timeline/_legacy/` — vendored monolith from `mascat/timeline/`.
  Slated for module-by-module split in v0.2.0. Lint rules ignore this
  directory until then.
- `src/chat_timeline/data/` — packaged resources (`LLM_INSTRUCTIONS.md`).

## Pull requests

- One change per PR.
- Reference an issue if applicable.
- Update `CHANGELOG.md` under the unreleased section.
- For source-format changes (Cursor/Claude/Codex schemas): include a fixture
  under `tests/fixtures/` and a regression test.

## Releasing (maintainers)

1. Bump version in `pyproject.toml` and `src/chat_timeline/__init__.py`.
2. Move unreleased entries in `CHANGELOG.md` under the new version + date.
3. Commit, tag `vX.Y.Z`, push tag.
4. GitHub Actions builds and publishes to PyPI via trusted publishing.
