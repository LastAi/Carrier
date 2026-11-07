# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Draft with Claude: the user describes what they want, Claude writes it up.

Used by the Ready Room's "✨ Draft with Claude…" buttons for rules, rule sets
and Debrief review agents. Nothing is saved here: the draft goes back to
the page, the user edits it, and saves it like any other text.

One short, tool-less model call per draft (``core.llm.Backend``), so it
cannot read or change files. All prompts for drafting live in this module.
"""

from __future__ import annotations

import asyncio
from typing import Any

from carrier.core.jsonutil import extract_json
from carrier.core.llm import Backend, BackendError

KINDS = ("project_rules", "global_rules", "rule_set", "agent")

RULES_SYSTEM = """You turn a user's description into clear working rules for Claude Code, an AI coding agent.
Write rules Claude can follow and check itself against:
- one rule per line, as a short "- " bullet, imperative ("Keep…", "Never…", "Ask before…");
- concrete and testable; no filler, no restating that Claude should be helpful;
- keep every requirement the user gave and add nothing they didn't ask for;
- group related rules under a short **bold** heading only if there are more than about eight.
Reply with the rules only — no preamble, no closing remarks."""

RULES_PROMPT = """Write {what}.
{context}
## What the user wants
{description}
{current}"""

AGENT_SYSTEM = """You write review agents for Debrief, a code-review tool. A review agent reads a diff of changes an
AI coding agent made, plus what the user asked for and the project's notes, and FLAGS changes a human should
look at closely. It does not fix anything.

Write the agent's instruction ("what to look for") from the user's description:
- say precisely what counts as a problem, with one or two concrete examples;
- say what should NOT be flagged, to avoid noise;
- address the agent as "you"; do not mention output format (Debrief adds that).

Reply with JSON only:
{"title": "short name, 2-5 words", "severity": "high|medium|low",
 "prompt": "the instruction, markdown, 60-250 words"}"""

AGENT_PROMPT = """## What the user wants this review agent to catch
{description}
{context}{current}"""


def _current_block(current: str) -> str:
    current = (current or "").strip()
    if not current:
        return ""
    return ("\n## The current text (revise it to match what the user wants; keep what still fits)\n"
            + current[:8000])


def _context_block(goal: str) -> str:
    goal = (goal or "").strip()
    return f"\n## The project (for context)\n{goal[:3000]}\n" if goal else ""


async def draft_async(kind: str, description: str, backend: Backend, current: str = "",
                      goal: str = "", model: str | None = "sonnet") -> dict[str, Any]:
    """Return ``{"text": ...}`` for rules, or ``{"title", "severity", "prompt"}`` for an agent."""
    if kind not in KINDS:
        raise ValueError(f"Unknown draft kind: {kind}")
    description = (description or "").strip()
    if not description:
        raise ValueError("Describe what you want first.")
    if kind == "agent":
        reply = await backend.ask(system=AGENT_SYSTEM, model=model,
                                  prompt=AGENT_PROMPT.format(description=description[:6000],
                                                             context=_context_block(goal),
                                                             current=_current_block(current)))
        try:
            data = extract_json(reply)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not str(data.get("prompt", "")).strip():
            # The model ignored the format: keep its text as the instruction.
            return {"title": "", "severity": "medium", "prompt": reply.strip()}
        sev = str(data.get("severity", "medium")).lower()
        return {"title": str(data.get("title", "")).strip()[:80],
                "severity": sev if sev in ("high", "medium", "low") else "medium",
                "prompt": str(data["prompt"]).strip()}
    what = {"project_rules": "rules for one project (they add to the user's global rules)",
            "global_rules": "global rules that apply to every project",
            "rule_set": "a reusable rule set (a named bundle of rules attached to projects or presets)"}[kind]
    reply = await backend.ask(system=RULES_SYSTEM, model=model,
                              prompt=RULES_PROMPT.format(what=what, description=description[:6000],
                                                         context=_context_block(goal),
                                                         current=_current_block(current)))
    return {"text": reply.strip()}


def draft(kind: str, description: str, backend: Backend, current: str = "", goal: str = "") -> dict[str, Any]:
    """Blocking wrapper for the desktop API.

    Runs on its own short-lived thread, so it works whether or not the
    calling thread already has an event loop.
    """
    import concurrent.futures

    def run() -> dict[str, Any]:
        return asyncio.run(draft_async(kind, description, backend, current, goal))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()
    except BackendError as exc:
        raise ValueError(f"Claude couldn't write a draft: {exc}") from exc
