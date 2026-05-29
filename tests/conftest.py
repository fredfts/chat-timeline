"""Test-suite fixtures shared across modules."""

from __future__ import annotations

import pytest

from chat_timeline import paths as paths_mod


@pytest.fixture(autouse=True)
def _clear_path_caches():
    """Reset the lru_cache on path resolvers so cwd changes between tests don't bleed."""
    paths_mod._git_toplevel.cache_clear()
    paths_mod._resolve_project_root.cache_clear()
    yield
    paths_mod._git_toplevel.cache_clear()
    paths_mod._resolve_project_root.cache_clear()
