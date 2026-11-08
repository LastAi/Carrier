# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""A stand-in for ``ClaudeSDKClient`` used by the Flight Deck tests.

It behaves like Claude Code from Flight Deck's point of view: it runs the
registered hooks and the permission callback in the same order the real CLI
does, really writes files, and yields real SDK message objects — so the
runner, the hooks and ``sdk.normalize`` are all exercised for real.

The "model" follows commands in the prompt, one per line::

    write <path> <text>     use the Write tool on <path> (relative to cwd)
    ask                     ask the user a question (AskUserQuestion)
    limit-warn              report the 5-hour limit as nearly used (85%)
    limit-hit [seconds]     stop at the usage limit (it resets after ``seconds``, default 3600)
    slow                    take a long time (for interrupt tests)
    anything else           just reply
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import time

from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock,
                              ToolPermissionContext, ToolResultBlock, ToolUseBlock, UserMessage)
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

ALL_CLIENTS: list["FakeClaudeClient"] = []


class FakeClaudeClient:
    def __init__(self, options: Any) -> None:
        self.options = options
        self.session_id = "fake-" + uuid.uuid4().hex[:8]
        self.prompts: list[str] = []
        self.contexts: list[str] = []  # additionalContext the hooks gave "Claude"
        self.denied: list[str] = []
        self.answers: list[Any] = []
        self.images: list[list[dict[str, Any]]] = []  # image blocks per message that had any  # what AskUserQuestion got back (None = skipped)
        self.connected = False
        self._interrupted = asyncio.Event()
        ALL_CLIENTS.append(self)

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def set_model(self, model: str | None = None) -> None:
        self.options.model = model

    async def set_permission_mode(self, mode: str) -> None:
        self.options.permission_mode = mode

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        if not isinstance(prompt, str):  # streaming input: a user message with content blocks
            blocks = [b async for m in prompt for b in m["message"]["content"]]
            self.images.append([b for b in blocks if b["type"] == "image"])
            prompt = "\n".join(b["text"] for b in blocks if b["type"] == "text")
        self.prompts.append(prompt)
        self._interrupted.clear()

    async def _hooks(self, event: str, tool: str | None, data: dict[str, Any]) -> list[dict[str, Any]]:
        outs = []
        for m in (self.options.hooks or {}).get(event, []):
            if m.matcher and tool and not re.fullmatch(m.matcher, tool):
                continue
            for fn in m.hooks:
                out = await fn({"hook_event_name": event, "session_id": self.session_id,
                                "cwd": self.options.cwd, "transcript_path": "", **data}, data.get("tool_use_id"),
                               {"signal": None})
                outs.append(out or {})
                ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext")
                if ctx:
                    self.contexts.append(ctx)
        return outs

    async def receive_response(self):
        prompt = self.prompts[-1]
        await self._hooks("UserPromptSubmit", None, {"prompt": prompt})
        if len(self.prompts) == 1:
            yield SystemMessage(subtype="init", data={"session_id": self.session_id, "model": "fake-model",
                                                            "apiKeySource": "none"})
        yield StreamEvent(uuid="u", session_id=self.session_id,
                          event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "On it"}})
        for line in prompt.splitlines():
            parts = line.split(" ", 2)
            if parts[0] == "slow":
                try:
                    await asyncio.wait_for(self._interrupted.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                                    is_error=False, num_turns=1, session_id=self.session_id, total_cost_usd=0.01)
                return
            if parts[0] == "limit-warn":
                yield RateLimitEvent(rate_limit_info=RateLimitInfo(
                    status="allowed_warning", resets_at=int(time.time()) + 7200, rate_limit_type="five_hour",
                    utilization=0.85), uuid="rl", session_id=self.session_id)
                continue
            if parts[0] == "limit-hit":
                reset = int(time.time()) + (int(parts[1]) if len(parts) > 1 else 3600)
                yield RateLimitEvent(rate_limit_info=RateLimitInfo(
                    status="rejected", resets_at=reset, rate_limit_type="five_hour", utilization=1.0),
                    uuid="rl", session_id=self.session_id)
                yield AssistantMessage(content=[TextBlock(text="You've hit your limit.")], model="<synthetic>",
                                       error="rate_limit")
                yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
                                    num_turns=1, session_id=self.session_id, total_cost_usd=0.0,
                                    result="You've hit your limit.", api_error_status=429)
                return
            if parts[0] == "ask-unrouted":  # Claude Code asks without calling the permission callback
                inp = {"questions": [{"question": "Which colour?", "options": [{"label": "Red"}, {"label": "Blue"}]}]}
                tid = "t" + uuid.uuid4().hex[:6]
                yield AssistantMessage(content=[ToolUseBlock(id=tid, name="AskUserQuestion", input=inp)], model="fake")
                yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="(no answer)", is_error=True)])
                continue
            if parts[0] == "ask":
                inp = {"questions": [{"question": "Which layout?", "header": "Layout", "multiSelect": False,
                                      "options": [{"label": "In place", "description": "Nothing moves."},
                                                  {"label": "Reorganise", "description": "Move folders."}]}]}
                tid = "t" + uuid.uuid4().hex[:6]
                yield AssistantMessage(content=[ToolUseBlock(id=tid, name="AskUserQuestion", input=inp)], model="fake")
                res = await self.options.can_use_tool("AskUserQuestion", inp, ToolPermissionContext())
                self.answers.append(res.updated_input.get("answers") if res.behavior == "allow" else None)
                yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="answered",
                                                           is_error=res.behavior != "allow")])
                continue
            if parts[0] == "write" and len(parts) == 3:
                path = str(Path(self.options.cwd) / parts[1])
                inp = {"file_path": path, "content": parts[2] + "\n"}
                tid = "t" + uuid.uuid4().hex[:6]
                pre = await self._hooks("PreToolUse", "Write", {"tool_name": "Write", "tool_input": inp,
                                                               "tool_use_id": tid})
                if any(o.get("hookSpecificOutput", {}).get("permissionDecision") == "deny" for o in pre):
                    self.denied.append(parts[1])
                    yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="blocked", is_error=True)])
                    continue
                ask = any(o.get("hookSpecificOutput", {}).get("permissionDecision") == "ask" for o in pre)
                if self.options.permission_mode == "default" or ask:
                    res = await self.options.can_use_tool("Write", inp, ToolPermissionContext())
                    if res.behavior == "deny":
                        self.denied.append(parts[1])
                        yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content=res.message,
                                                                   is_error=True)])
                        continue
                yield AssistantMessage(content=[ToolUseBlock(id=tid, name="Write", input=inp)], model="fake")
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(inp["content"])
                await self._hooks("PostToolUse", "Write", {"tool_name": "Write", "tool_input": inp,
                                                          "tool_response": "ok", "tool_use_id": tid})
                yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="File written", is_error=False)])
        yield AssistantMessage(content=[TextBlock(text=f"Done: {prompt.splitlines()[0][:40]}")], model="fake")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id=self.session_id, total_cost_usd=0.02 * len(self.prompts))
