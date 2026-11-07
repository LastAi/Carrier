# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Model access, behind one small interface.

Every model call in Carrier (e.g. Debrief's segmenter, criteria and guide) goes
through :meth:`Backend.ask`
and nothing else. That keeps the rest of the code independent of any SDK and
makes testing trivial: tests pass a :class:`FakeBackend` with canned replies.

The real backend uses the **Claude Agent SDK**, which drives the Claude Code
CLI you already have installed. That means:

* no separate API key — it uses your existing Claude Code login,
* agents can be given read-only tools (Read, Grep, Glob) to look around the
  repository when they need more context than the diff.

Safety notes for contributors
-----------------------------
* ``setting_sources=[]`` stops the inner sessions loading the user's
  settings — crucially their **hooks**. Without it, Debrief's own Stop hook
  would fire after every agent call and recursively start new reviews.
* ``CARRIER_INNER=1`` is also set in the inner environment as a second
  guard; ``debrief prepare`` exits immediately when it sees it.
* Callers choose the tools; Debrief's agents only ever get read-only ones,
  so reviewing never writes to the repository (fixes go back to the human
  as a list). Flight Deck, which runs real Claude Code work, will not use
  this backend for that — it drives Claude Code sessions directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from carrier.core.models import EFFORT_ORDER, effective_effort

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]


# Claude Code's basic model aliases and its reasoning-effort levels. The
# models actually offered come from Claude Code itself (core.models).
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = EFFORT_ORDER



async def probe_models(timeout: float = 60.0) -> list[dict[str, Any]]:
    """Ask Claude Code which models it offers (connect + disconnect; no message, no cost).

    Returns its raw ``models`` list (see ``core.models.from_server_info``).
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    client = ClaudeSDKClient(ClaudeAgentOptions(setting_sources=[], env={"CARRIER_INNER": "1"}))
    await asyncio.wait_for(client.connect(), timeout)
    try:
        info = await client.get_server_info() or {}
        return list(info.get("models") or [])
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def refresh_models() -> dict[str, Any]:
    """Blocking: ask Claude Code for its models and save them (see core.models)."""
    from carrier.core import models

    raw = asyncio.run(probe_models())
    found, default = models.from_server_info(raw)
    return models.store(found, default)


class BackendError(RuntimeError):
    """A model call failed (CLI missing, auth problem, API error, …)."""


class Backend(Protocol):
    async def ask(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        tools: list[str] | None = None,
        cwd: Path | None = None,
        max_turns: int = 1,
        add_dirs: list[str] | None = None,
        effort: str | None = None,
    ) -> str:
        """Send one prompt and return the final text reply.

        ``add_dirs`` are extra folders the (read-only) tools may look in,
        e.g. a Carrier project's extra folders. ``effort`` is Claude Code's
        reasoning effort (see ``EFFORTS``); None = the model's default."""
        ...


class AgentSDKBackend:
    """Backend that runs each call as a short Claude Code session."""

    def __init__(self, concurrency: int = 4) -> None:
        # One semaphore per backend caps parallel Claude Code processes.
        self._sem = asyncio.Semaphore(concurrency)

    async def ask(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        tools: list[str] | None = None,
        cwd: Path | None = None,
        max_turns: int = 1,
        add_dirs: list[str] | None = None,
        effort: str | None = None,
    ) -> str:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                TextBlock,
                query,
            )
        except ImportError as exc:  # pragma: no cover - depends on install
            raise BackendError("claude-agent-sdk is not installed: pip install claude-agent-sdk") from exc

        tools = list(tools or [])
        options = ClaudeAgentOptions(
            system_prompt=system,
            model=model,
            tools=tools,  # the only tools that exist in this session
            allowed_tools=tools,  # ...and they are pre-approved (read-only)
            cwd=str(cwd) if cwd else None,
            add_dirs=list(add_dirs or []),
            effort=effective_effort(model, effort),  # nearest level this model supports
            # Tools need a turn to run and another to answer.
            max_turns=max_turns if tools else 1,
            setting_sources=[],  # do NOT load user hooks/settings (see module doc)
            env={"CARRIER_INNER": "1"},
        )
        texts: list[str] = []
        result: str | None = None
        async with self._sem:
            try:
                async for msg in query(prompt=prompt, options=options):
                    if isinstance(msg, AssistantMessage):
                        texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                    elif isinstance(msg, ResultMessage):
                        if msg.is_error:
                            raise BackendError(f"Claude Code returned an error: {msg.result or msg.subtype}")
                        result = msg.result
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"Claude Code call failed: {exc}") from exc
        # The result message holds the final answer; fall back to the last
        # text block for older SDK versions.
        return (result or (texts[-1] if texts else "")).strip()


# A fake reply handler receives (system, prompt) and returns the reply text.
FakeHandler = Callable[[str, str], str]


class FakeBackend:
    """Deterministic backend for tests and ``--no-ai`` demos.

    Every call is recorded in ``self.calls`` so tests can assert on prompts.
    """

    def __init__(self, handler: FakeHandler) -> None:
        self.handler = handler
        self.calls: list[dict[str, object]] = []

    async def ask(self, *, system: str, prompt: str, model: str | None = None,
                  tools: list[str] | None = None, cwd: Path | None = None,
                  max_turns: int = 1, add_dirs: list[str] | None = None,
                  effort: str | None = None) -> str:
        self.calls.append({"system": system, "prompt": prompt, "model": model, "tools": tools,
                           "add_dirs": add_dirs, "effort": effort, "max_turns": max_turns})
        await asyncio.sleep(0)  # behave like a real coroutine
        return self.handler(system, prompt)
