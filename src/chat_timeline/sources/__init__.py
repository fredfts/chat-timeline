"""Chat-source plugins.

Each source module exposes:

    SOURCE_NAME: str
    list_chats(project_dir, scope=None) -> list[dict]
    export_single_chat(chat, include_tool_params=False) -> Path | None

See ``chat_timeline.sources.base`` for the chat-dict schema.
"""
