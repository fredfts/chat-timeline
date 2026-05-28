"""Backwards-compatible shim — content moved to ``chat_timeline.sources.cursor`` in v0.2.0."""

from chat_timeline.sources.cursor import *  # noqa: F401, F403
from chat_timeline.sources.cursor import (  # noqa: F401 (named exports kept stable)
    SOURCE_NAME,
    export_single_chat,
    list_chats,
)
