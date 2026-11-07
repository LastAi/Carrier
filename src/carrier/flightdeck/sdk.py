# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Adapter between Flight Deck and the Claude Agent SDK.

This is the only Flight Deck module that imports the SDK (``core/llm.py``
does the same for one-shot calls). It does three things:

1. :func:`build_options` — configure a session so it behaves like **normal
   Claude Code**: the Claude Code system prompt, your user/project/local
   settings and CLAUDE.md files, all tools, and your chosen permission mode.
   Flight Deck adds only a short note to the system prompt, its hooks, and a
   permission callback that shows prompts in the Carrier window.
2. :func:`normalize` — turn SDK message objects into small JSON *events*
   that the UI renders and the store persists (see ``EVENT TYPES`` below).
3. Small helpers to build hook outputs and permission results.

EVENT TYPES (dicts with a ``type`` key)::

    user        {text}                          a message the user sent
    text        {text}                          Claude's reply text
    thinking    {text}                          extended thinking (collapsed in UI)
    live        {text}                          streaming preview (not persisted)
    tool_use    {id, name, summary, input}      Claude used a tool
    tool_result {id, text, is_error}            the tool's output (truncated)
    result      {cost, turns, is_error, text}   end of a turn
    permission  {req_id, tool, summary, reason} Claude Code asks for approval
    permission_done {req_id, decision}
    notice      {kind, text}                    coordination notice (Flight Deck)
    status      {status}
    error       {text}
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from carrier.core.models import effective_effort

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_RESULT_CHARS = 4000

SYSTEM_APPEND = """\
You are running inside Carrier Flight Deck, which may run several Claude Code
sessions in parallel on related projects. Flight Deck may add notes, marked
[Carrier Flight Deck], about other sessions' work. Treat them as information:
avoid undoing other sessions' changes, keep shared files compatible, and say
in your reply when your change affects another project."""


def build_options(*, cwd: Path, model: str | None, mode: str, resume: str | None,
                  hooks: dict[str, list[Any]], can_use_tool: Callable[..., Any], context: str = "",
                  add_dirs: list[str] | None = None, effort: str | None = None) -> Any:
    """``context`` is the Ready Room text (goal, rules, task) for this session;
    ``add_dirs`` are the project's extra folders, which Claude Code may also use."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        cwd=str(cwd),
        add_dirs=list(add_dirs or []),
        model=model or None,
        # The user's preferred effort, mapped to the nearest level this model
        # supports (none if it has no effort setting) — see core.models.
        effort=effective_effort(model or None, effort),
        permission_mode=mode,  # "default" | "acceptEdits" | "plan" | "bypassPermissions"
        resume=resume,
        # Behave like normal Claude Code: its system prompt, plus a short note.
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": SYSTEM_APPEND + ("\n\n" + context if context else "")},
        # Load the user's settings and CLAUDE.md files, as the CLI would.
        setting_sources=["user", "project", "local"],
        include_partial_messages=True,  # streaming preview in the UI
        hooks=hooks,
        can_use_tool=can_use_tool,
    )


def make_client(options: Any) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)


def hook_matcher(matcher: str | None, fn: Callable[..., Any]) -> Any:
    from claude_agent_sdk import HookMatcher

    return HookMatcher(matcher=matcher, hooks=[fn])


def allow(updated_input: dict[str, Any] | None = None) -> Any:
    from claude_agent_sdk import PermissionResultAllow

    return PermissionResultAllow(updated_input=updated_input)


def deny(message: str, interrupt: bool = False) -> Any:
    from claude_agent_sdk import PermissionResultDeny

    return PermissionResultDeny(message=message, interrupt=interrupt)


# -- hook outputs --------------------------------------------------------------


def pre_tool_output(context: str | None = None, decision: str | None = None,
                    reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if context:
        out["additionalContext"] = context
    if decision:
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = reason or ""
    return {"hookSpecificOutput": out} if len(out) > 1 else {}


def post_tool_output(context: str | None) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}} if context else {}


def prompt_output(context: str | None) -> dict[str, Any]:
    return ({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
            if context else {})


# -- messages → events -----------------------------------------------------------


def allow_key(name: str, inp: dict[str, Any]) -> tuple[str, str]:
    """What "allow for this session" covers, and a label for the button.

    Shell commands are scoped to their program (``git``, ``npm`` …) and web
    fetches to their site, so allowing one ``git status`` doesn't allow
    every command. Other tools (edits, reads) are allowed as a whole, like
    Claude Code's own "auto-accept edits".
    """
    if name == "Bash":
        cmd = str(inp.get("command", "")).strip().split()
        prog = cmd[0] if cmd else ""
        return f"Bash:{prog}", f"Allow `{prog}` commands for this session" if prog else "Allow this command"
    if name == "WebFetch":
        from urllib.parse import urlparse

        host = urlparse(str(inp.get("url", ""))).netloc
        return f"WebFetch:{host}", f"Allow fetching from {host} for this session"
    return name, f"Allow {name} for this session"


def summarize_tool(name: str, inp: dict[str, Any], cwd: Path | None = None) -> str:
    """One-line description of a tool call for the UI ("Edit src/app.py")."""
    def rel(p: str) -> str:
        if cwd:
            try:
                return Path(p).resolve().relative_to(cwd.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        return p

    if name in ("Read", "Edit", "Write", "MultiEdit"):
        return f"{name} {rel(str(inp.get('file_path', '')))}"
    if name == "NotebookEdit":
        return f"{name} {rel(str(inp.get('notebook_path', '')))}"
    if name == "Bash":
        return f"Bash: {str(inp.get('command', ''))[:200]}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}" + (f" in {inp['path']}" if inp.get("path") else "")
    if name in ("WebFetch", "WebSearch"):
        return f"{name} {inp.get('url') or inp.get('query') or ''}"
    if name == "Task":
        return f"Task: {inp.get('description', '')}"
    if name == "TodoWrite":
        return "Update to-do list"
    text = json.dumps(inp, ensure_ascii=False)
    return f"{name} {text[:160]}"


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text") or (f"[{c.get('type')}]" if c.get("type") else json.dumps(c)))
            else:
                parts.append(str(c))
        text = "\n".join(parts)
    else:
        text = str(content)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n… (truncated)"


def _trim_input(inp: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in inp.items():
        if isinstance(v, str) and len(v) > 2000:
            v = v[:2000] + "… (truncated)"
        out[k] = v
    return out


def normalize(msg: Any, cwd: Path | None = None) -> list[dict[str, Any]]:
    """Convert one SDK message into zero or more events."""
    from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock,
                                  ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage)

    events: list[dict[str, Any]] = []
    if isinstance(msg, StreamEvent):
        ev = msg.event or {}
        if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            events.append({"type": "live", "text": ev["delta"].get("text", "")})
        return events
    if isinstance(msg, AssistantMessage):
        if msg.parent_tool_use_id:  # inside a sub-agent: show only tool use, compactly
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    events.append({"type": "tool_use", "id": b.id, "name": b.name, "sub": True,
                                   "summary": summarize_tool(b.name, b.input or {}, cwd),
                                   "input": _trim_input(b.input or {})})
            return events
        for b in msg.content:
            if isinstance(b, TextBlock) and b.text.strip():
                events.append({"type": "text", "text": b.text})
            elif isinstance(b, ThinkingBlock) and b.thinking.strip():
                events.append({"type": "thinking", "text": b.thinking})
            elif isinstance(b, ToolUseBlock):
                events.append({"type": "tool_use", "id": b.id, "name": b.name,
                               "summary": summarize_tool(b.name, b.input or {}, cwd),
                               "input": _trim_input(b.input or {})})
        if msg.error:
            events.append({"type": "error", "text": str(msg.error)})
        return events
    if isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, list) else []
        for b in content:
            if isinstance(b, ToolResultBlock):
                events.append({"type": "tool_result", "id": b.tool_use_id,
                               "text": _result_text(b.content), "is_error": bool(b.is_error)})
        return events
    if isinstance(msg, ResultMessage):
        events.append({"type": "result", "cost": msg.total_cost_usd or 0.0, "turns": msg.num_turns,
                       "is_error": bool(msg.is_error), "text": (msg.result or "") if msg.is_error else "",
                       "session": msg.session_id})
        return events
    if type(msg).__name__ == "ConversationResetMessage":  # e.g. after /clear: cost counter restarts
        events.append({"type": "reset", "text": "Conversation cleared."})
        return events
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        data = msg.data or {}
        events.append({"type": "system", "session": data.get("session_id"), "model": data.get("model"),
                       "text": f"Claude Code session started ({data.get('model', 'default model')})."})
    return events
