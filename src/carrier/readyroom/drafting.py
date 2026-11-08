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

KINDS = ("project_rules", "global_rules", "rule_set", "agent", "skill", "task", "routine")

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


SKILL_SYSTEM = """You write Claude Code skills. A skill is a reusable procedure Claude Code loads when its
description matches the work at hand: e.g. "release a mod", "run the build and tests", "write a forum post".

From the user's description, write:
- name: kebab-case, 2-4 words;
- description: ONE sentence saying when to use it (Claude Code decides from this alone), starting "Use when";
- body: markdown instructions Claude can follow step by step: numbered steps, exact commands in code
  blocks, checks to run, what to report back. Keep every requirement the user gave; invent no paths or
  commands they didn't give — write <placeholders> instead.

Reply with JSON only: {"name": "...", "description": "...", "body": "..."}"""


TASK_SYSTEM = """You write tasks for Claude Code, an AI coding agent, from the user's rough description.
A task is one piece of work a single session can finish. Write:
- title: short and specific, imperative, under 80 characters ("Add retry to save loading");
- details: what to do and what "done" means — the concrete steps or requirements the user gave, files or
  areas involved if they named them, and how to check it works. Markdown bullets; no filler.
Keep every requirement the user gave; invent no file names, paths or commands they didn't give.
Reply with JSON only: {"title": "...", "details": "..."}"""

ROUTINE_SYSTEM = """You write routines for Carrier: repeatable tasks a project runs again and again, done by
Claude Code (an AI coding agent) — e.g. "update the documentation after each task", "push to GitHub once
everything is reviewed". From the user's description, write:
- name: short, under 50 characters;
- instructions: what Claude should do each time, as steps it can follow on its own; say what to check and
  what to leave alone; markdown bullets;
- trigger: "after_task" if it should run after every finished task, else "manual";
- gate: "reviewed" if it should only run once all changes have been reviewed (anything that publishes,
  pushes, releases or deploys), else "none".
Keep every requirement the user gave; invent no paths or commands they didn't give.
Reply with JSON only: {"name": "...", "instructions": "...", "trigger": "manual|after_task", "gate": "none|reviewed"}"""


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
    if kind in ("task", "routine"):
        system = TASK_SYSTEM if kind == "task" else ROUTINE_SYSTEM
        reply = await backend.ask(system=system, model=model,
                                  prompt=f"## What the user wants\n{description[:6000]}\n"
                                         f"{_context_block(goal)}{_current_block(current)}")
        try:
            data = extract_json(reply)
        except ValueError:
            data = None
        if kind == "task":
            if not isinstance(data, dict) or not str(data.get("title", "")).strip():
                return {"title": description.strip().splitlines()[0][:80], "details": reply.strip()}
            return {"title": str(data["title"]).strip()[:200], "details": str(data.get("details", "")).strip()}
        if not isinstance(data, dict) or not str(data.get("instructions", "")).strip():
            return {"name": "", "instructions": reply.strip(), "trigger": "manual", "gate": "none"}
        trigger, gate = str(data.get("trigger", "")), str(data.get("gate", ""))
        return {"name": str(data.get("name", "")).strip()[:80], "instructions": str(data["instructions"]).strip(),
                "trigger": trigger if trigger in ("manual", "after_task") else "manual",
                "gate": gate if gate in ("none", "reviewed") else "none"}
    if kind == "skill":
        reply = await backend.ask(system=SKILL_SYSTEM, model=model,
                                  prompt=f"## What the user wants this skill to do\n{description[:6000]}\n"
                                         f"{_context_block(goal)}{_current_block(current)}")
        try:
            data = extract_json(reply)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not str(data.get("body", "")).strip():
            return {"name": "", "description": "", "body": reply.strip()}
        return {"name": str(data.get("name", "")).strip()[:64], "description": str(data.get("description", "")).strip(),
                "body": str(data["body"]).strip()}
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
