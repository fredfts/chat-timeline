"""Pure Markdown rendering and parsing helpers.

Extracted from ``_legacy/main.py`` in v0.2.0. These functions take their
project-root context as an explicit parameter (instead of reading the
``PROJECT_DIR`` module global) so they can be reused from anywhere in the
package and tested in isolation.

Functions:
    Time formatting:   ``epoch_ms_to_dt``, ``iso_to_dt``, ``fmt_dt``,
                       ``fmt_dt_filename``
    Markdown utility:  ``sanitize_markdown_content``, ``fenced_literal_block``,
                       ``strip_redacted``, ``sanitize_filename``
    Path utility:      ``relative_path(p, project_root)``
    Tool formatting:   ``format_tool_call_detail(tc, project_root=None)``
    Chat export:       ``export_chat_markdown(meta, turns,
                       include_tool_params=False, project_root=None)``
    Chat re-parsing:   ``parse_chat_export(filepath)``
    Selection parser:  ``parse_selection(arg, total)``
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def epoch_ms_to_dt(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_dt(dt):
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_dt_filename(dt):
    if not dt:
        return "unknown"
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def sanitize_markdown_content(text):
    """Neutralize heading syntax in free-form content so only exporter-defined
    headings shape the Markdown structure."""
    if not text:
        return ""

    out_lines = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in text.splitlines():
        stripped = line.lstrip()
        fence = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence:
            token = fence.group(1)
            ch = token[0]
            ln = len(token)
            if not in_fence:
                in_fence = True
                fence_char = ch
                fence_len = ln
            elif ch == fence_char and ln >= fence_len:
                in_fence = False
            out_lines.append(line)
            continue

        if not in_fence:
            atx = re.match(r"^(\s{0,3})#{1,6}\s+(.*)$", line)
            if atx:
                line = f"{atx.group(1)}**{atx.group(2)}**"
            else:
                bq_atx = re.match(r"^(\s{0,3}(?:>\s*)+)#{1,6}\s+(.*)$", line)
                if bq_atx:
                    line = f"{bq_atx.group(1)}**{bq_atx.group(2)}**"

        out_lines.append(line)

    return "\n".join(out_lines)


def fenced_literal_block(text, language="markdown"):
    """Wrap text in a safe fenced block using a fence longer than existing runs."""
    runs = re.findall(r"`+", text or "")
    max_run = max((len(r) for r in runs), default=0)
    fence = "`" * max(3, max_run + 1)
    header = f"{fence}{language}" if language else fence
    return "\n".join([header, text, fence])


def strip_redacted(text):
    """Remove content between '''' redaction markers."""
    if not text:
        return text
    return re.sub(r"'{4}[\s\S]*?'{4}", "", text).strip()


def sanitize_filename(s, max_len=80):
    s = re.sub(r'[<>:"/\\|?*;]', "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def relative_path(p, project_root: Path | None = None):
    """Best-effort conversion of absolute path to project-relative.

    ``project_root`` defaults to ``Path.cwd()`` when None — callers that
    know the project root (the CLI, the legacy main module) should pass it
    explicitly.
    """
    if not p:
        return p
    p = str(p).replace("\\", "/")
    root = (project_root or Path.cwd()).resolve().as_posix()
    if p.lower().startswith(root.lower() + "/"):
        return p[len(root) + 1 :]
    if re.match(r"^[A-Za-z]:", p):
        try:
            rel = os.path.relpath(p, root)
            if not rel.startswith(".."):
                return rel.replace("\\", "/")
        except ValueError:
            pass
    return p


# ---------------------------------------------------------------------------
# Tool call formatting (supports Cursor/Claude/Codex tool names)
# ---------------------------------------------------------------------------


def format_tool_call_detail(tc, project_root: Path | None = None):
    """One-line summary of a tool call for the structured export."""
    name = tc["name"]
    params = tc.get("params", {})
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            return f"{name}: {params[:120]}"
    if not isinstance(params, dict):
        return f"{name}"

    def _get_file_param(p):
        return relative_path(
            p.get("relativeWorkspacePath")
            or p.get("filePath")
            or p.get("file_path")
            or p.get("path")
            or "",
            project_root,
        )

    # Cursor tools
    if name in ("read_file_v2", "read_file"):
        return f"{name}: {_get_file_param(params)}"
    if name in ("edit_file_v2", "edit_file"):
        return f"{name}: {_get_file_param(params)}"
    if name in ("ripgrep_raw_search", "grep"):
        return f"{name}: {params.get('pattern', params.get('query', ''))}"
    if name in ("semantic_search_full",):
        return f"{name}: {params.get('query', '')}"
    if name in ("run_terminal_command",):
        return f"{name}: {params.get('command', '')}"
    if name == "todo_write":
        return f"{name}"
    if name == "read_lints":
        return f"{name}: {params.get('filePaths', '')}"

    # Claude Code tools
    if name == "Read":
        return f"{name}: {relative_path(params.get('file_path', ''), project_root)}"
    if name in ("Edit", "Write"):
        return f"{name}: {relative_path(params.get('file_path', ''), project_root)}"
    if name == "Bash":
        cmd = params.get("command", "")
        return f"{name}: {cmd[:120]}"
    if name == "Grep":
        return f"{name}: {params.get('pattern', '')}"
    if name == "Glob":
        return f"{name}: {params.get('pattern', '')}"
    if name == "Agent":
        desc = params.get("description", "")
        return f"{name}: {desc}" if desc else f"{name}"
    if name == "WebSearch":
        return f"{name}: {params.get('query', '')}"
    if name == "WebFetch":
        return f"{name}: {params.get('url', '')[:120]}"
    if name == "Skill":
        return f"{name}: {params.get('skill', '')}"
    if name == "NotebookEdit":
        return f"{name}"

    return f"{name}: {json.dumps(params, ensure_ascii=False, default=str)[:120]}"


# ---------------------------------------------------------------------------
# Chat export formatting (shared — builds .md from common turn format)
# ---------------------------------------------------------------------------


def export_chat_markdown(meta, turns, include_tool_params=False, project_root: Path | None = None):
    """Build full markdown string for a single chat export."""
    lines = []

    lines.append("---")
    lines.append(f'title: "{meta.get("name", "(unnamed)")}"')
    lines.append(f'composer_id: "{meta.get("id", "")}"')
    lines.append(f'created: "{fmt_dt(meta.get("created"))}"')
    lines.append(f'last_updated: "{fmt_dt(meta.get("last_updated"))}"')
    lines.append(f'status: "{meta.get("status", "")}"')
    lines.append(f'mode: "{meta.get("mode", "")}"')
    lines.append(f'model: "{meta.get("model", "unknown")}"')
    lines.append(f"max_mode: {str(meta.get('max_mode', False)).lower()}")
    lines.append(f'agent_backend: "{meta.get("agent_backend", "")}"')
    lines.append(f'branch: "{meta.get("branch", "")}"')
    lines.append(f"context_tokens: {meta.get('context_tokens', 0)}")
    lines.append(f"context_limit: {meta.get('context_limit', 0)}")
    lines.append(f"lines_added: {meta.get('lines_added', 0)}")
    lines.append(f"lines_removed: {meta.get('lines_removed', 0)}")
    lines.append(f"files_changed: {meta.get('files_changed', 0)}")
    lines.append(f"is_agentic: {str(meta.get('is_agentic', False)).lower()}")
    lines.append(f'source: "{meta.get("source", "")}"')

    files_affected = meta.get("files_affected", [])
    if files_affected:
        lines.append("files_affected:")
        for path in files_affected:
            lines.append(f'  - "{path}"')

    checkpoints = set()
    for t in turns:
        checkpoints.update(t.get("checkpoints", []))
    if checkpoints:
        lines.append("checkpoints:")
        for cp in sorted(checkpoints):
            lines.append(f'  - "{cp}"')

    lines.append("---")
    lines.append("")

    lines.append(f"# {meta.get('name', '(unnamed)')}")
    lines.append("")

    for i, turn in enumerate(turns, 1):
        ts = fmt_dt(turn["user_timestamp"])
        lines.append(f"## Q{i} [{turn['user_model']}] {ts}")
        lines.append("")
        user_text = sanitize_markdown_content(turn["user_text"].strip())
        if user_text:
            for uline in user_text.split("\n"):
                lines.append(f"> {uline}")
        else:
            lines.append("> *(continuation)*")
        lines.append("")

        if turn["thinking_blocks"]:
            lines.append("<details><summary>Thinking blocks</summary>")
            lines.append("")
            for tb in turn["thinking_blocks"]:
                dur = tb["duration_ms"] / 1000 if tb["duration_ms"] else 0
                lines.append(f"**{fmt_dt(tb['timestamp'])}** ({dur:.1f}s)")
                lines.append("")
                text = sanitize_markdown_content(tb.get("text", ""))
                if text:
                    lines.append(text.strip())
                    lines.append("")
            lines.append("</details>")
            lines.append("")

        if turn["tool_calls"]:
            lines.append(f"### Tool calls ({len(turn['tool_calls'])})")
            lines.append("")
            for tc in turn["tool_calls"]:
                detail = format_tool_call_detail(tc, project_root)
                lines.append(f"- `{detail}` ({tc['status']}) {fmt_dt(tc['timestamp'])}")
                if include_tool_params and tc.get("raw_args"):
                    lines.append("  ```")
                    lines.append(f"  {tc['raw_args'][:500]}")
                    lines.append("  ```")
            lines.append("")

        if turn["assistant_parts"]:
            first = turn["assistant_parts"][0]
            model = first.get("model", turn["user_model"])
            ts = fmt_dt(first.get("timestamp"))
            lines.append(f"## A{i} [{model}] {ts}")
            lines.append("")
            for part in turn["assistant_parts"]:
                text = sanitize_markdown_content(part["text"].strip())
                if text:
                    lines.append(text)
                    lines.append("")
        else:
            lines.append(f"## A{i}")
            lines.append("*(Tool-call-only turn — no text response)*")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chat export parsing (reads exported .md files for timeline/session)
# ---------------------------------------------------------------------------


def parse_chat_export(filepath):
    """Parse an exported chat .md file into metadata dict + list of turns."""
    content = filepath.read_text(encoding="utf-8", errors="replace")

    meta = {}
    fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    body = content[fm.end() :] if fm else content
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip().strip('"')

    turns = []
    cur = None
    section = None
    tb = None

    for line in body.splitlines():
        qm = re.match(r"^## Q(\d+)\s+\[([^\]]*)\]\s+(.+)$", line)
        if qm:
            if tb and cur:
                cur["thinking_blocks"].append(tb)
                tb = None
            if cur:
                turns.append(cur)
            cur = dict(
                number=int(qm.group(1)),
                user_model=qm.group(2),
                user_timestamp=qm.group(3),
                user_text="",
                thinking_blocks=[],
                tool_calls=[],
                response_model="",
                response_timestamp="",
                response_text="",
            )
            section = "q"
            continue

        if cur is None:
            continue

        am = re.match(r"^## A(\d+)\s*(?:\[([^\]]*)\])?\s*(.*)$", line)
        if am:
            if tb:
                cur["thinking_blocks"].append(tb)
                tb = None
            cur["response_model"] = am.group(2) or ""
            cur["response_timestamp"] = (am.group(3) or "").strip()
            section = "a"
            continue

        if re.match(r"^### Tool calls \(\d+\)$", line):
            if tb:
                cur["thinking_blocks"].append(tb)
                tb = None
            section = "tools"
            continue

        if "<details>" in line:
            section = "thinking"
            continue
        if "</details>" in line:
            if tb:
                cur["thinking_blocks"].append(tb)
                tb = None
            section = "q"
            continue

        if section == "q":
            if line.startswith("> "):
                cur["user_text"] += line[2:] + "\n"
            elif line == ">":
                cur["user_text"] += "\n"

        elif section == "thinking":
            thm = re.match(r"^\*\*(.+?)\*\*\s+\(([\d.]+)s\)$", line)
            if thm:
                if tb:
                    cur["thinking_blocks"].append(tb)
                tb = dict(timestamp=thm.group(1), duration_s=float(thm.group(2)), text="")
            elif tb is not None:
                if line.strip():
                    tb["text"] += line + "\n"

        elif section == "tools":
            tcm = re.match(r"^- `(.+?)`\s+\((\w+)\)\s+(.+)$", line)
            if tcm:
                cur["tool_calls"].append(
                    dict(detail=tcm.group(1), status=tcm.group(2), timestamp=tcm.group(3))
                )

        elif section == "a":
            if line != "---":
                cur["response_text"] += line + "\n"

    if tb and cur:
        cur["thinking_blocks"].append(tb)
    if cur:
        turns.append(cur)
    return meta, turns


def parse_selection(arg, total):
    """Parse selection string like '1,4,6' or '1-5' or 'all'."""
    if arg.lower() == "all":
        return list(range(total))
    indices = set()
    for part in arg.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            for x in range(int(a), int(b) + 1):
                if 1 <= x <= total:
                    indices.add(x - 1)
        elif part.isdigit():
            x = int(part)
            if 1 <= x <= total:
                indices.add(x - 1)
    return sorted(indices)
