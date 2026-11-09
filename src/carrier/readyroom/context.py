# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""What Claude is told at the start of a Flight Deck session.

Ready Room stores goals, rules and tasks; this module turns them into one
block of text that Flight Deck appends to Claude Code's system prompt. The
same function powers the "What Claude will be told" preview, so the user
always sees exactly what is sent.

Order and priority (stated in the text, so Claude knows how to weigh it)::

    the user's own messages
      > the current task
        > project rules (including attached rule sets)
          > global rules (including attached rule sets)

The project's CLAUDE.md files still load as usual (Claude Code does that);
Ready Room never writes into the repository.

Sizes are capped so a long rule list or history cannot crowd out the
conversation; the preview marks anything that was shortened.
"""

from __future__ import annotations

import time
from typing import Any

from carrier.core.db import Store, folder_kind

MAX_SECTION_CHARS = 6000
HISTORY_ITEMS = 5


def _cap(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n… (shortened)"


def _rule_sets(store: Store, ids: list[int]) -> list[dict[str, Any]]:
    out = []
    for rid in ids:
        rs = store.rule_set(rid)
        if rs and rs["body"].strip():
            out.append(rs)
    return out


def _rules_block(own: str, sets: list[dict[str, Any]]) -> str:
    parts = []
    if own.strip():
        parts.append(own.strip())
    for rs in sets:
        parts.append(f"({rs['name']})\n{rs['body'].strip()}")
    return _cap("\n\n".join(parts)) if parts else ""


def build(store: Store, project_id: int | None, task: dict[str, Any] | None,
          preset: dict[str, Any] | None = None, for_review: bool = False) -> str:
    """The context text for a session on ``task`` in ``project_id`` (None = standalone).

    ``for_review=True`` words it for Debrief's agents instead: what the
    project is and the rules its work should follow.
    """
    sections: list[str] = [
        "[Carrier Ready Room] What this project is and the rules its work should follow, as set up by "
        "the user. Judge the changes against these." if for_review else
        "[Carrier Ready Room] Context for this session, set up by the user. Priority if anything "
        "conflicts: the user's messages, then the current task, then project rules, then global rules."
    ]
    project = store.project(project_id) if project_id is not None else None

    if project:
        head = f"## Project: {project['name']}"
        if project["goal"].strip():
            head += f"\nLong-term goal (what this project is):\n{_cap(project['goal'], 3000)}"
        work = [f for f in project.get("folders") or [] if folder_kind(f) == "work"]
        ref = [f for f in project.get("folders") or [] if folder_kind(f) == "reference"]
        if work:
            head += ("\nThis project also works in these folders (read and edit them when the task "
                     "needs it):\n" + "\n".join(f"- {f['label']}: {f['path']}" for f in work))
        if ref:
            head += ("\nReference folders — READ ONLY. Look things up there to understand or complete "
                     "the task, but never create, edit or delete files in them (edits are blocked):\n"
                     + "\n".join(f"- {f['label']}: {f['path']}" for f in ref))
        sections.append(head)
    else:
        sections.append("## Standalone task\nThis task is not part of a project.")

    if task:
        t = f"## Current task\n{task['title']}"
        if task.get("details", "").strip():
            t += f"\n{_cap(task['details'], 3000)}"
        if (task.get("handoff") or "").strip():
            t += ("\n\n### Where the last session on this task left off\n"
                  "(Written when that session ended. The files may have changed since; check before relying on it.)\n"
                  f"{_cap(task['handoff'], 3000)}")
        if task.get("group_id") and (g := store.task_group(task["group_id"])):
            t += (f"\n\n(This task is in the task group “{g['name']}”, which runs alongside the project's other "
                  "work: other sessions may be working in the same folders at the same time.)")
        sections.append(t)

    if project:
        ids = store.project_rule_set_ids(project["id"])
        block = _rules_block(project["rules"], _rule_sets(store, ids))
        if block:
            sections.append("## Project rules\n" + block)

    preset_sets = _rule_sets(store, [preset["rule_set_id"]]) if preset and preset.get("rule_set_id") else []
    if preset_sets:
        sections.append("## Session rules (preset: " + preset["name"] + ")\n" + _rules_block("", preset_sets))

    global_block = _rules_block(store.get_setting("global_rules", ""),
                                _rule_sets(store, store.get_setting("global_rule_sets", [])))
    if global_block:
        sections.append("## Global rules\n" + global_block)

    if project:
        done = store.tasks(project["id"], statuses=("done",))[:HISTORY_ITEMS]
        if done:
            lines = []
            for d in done:
                when = time.strftime("%Y-%m-%d", time.localtime(d["finished"])) if d["finished"] else ""
                origin = (f" [done as a standalone task; its files are in {d['origin_path']}]"
                          if d["origin"] == "standalone" else "")
                summary = f": {d['summary'].strip()}" if d["summary"].strip() else ""
                lines.append(f"- {d['title']} ({when}){summary}{origin}")
            sections.append("## Recent work in this project (newest first)\n" + _cap("\n".join(lines), 3000))

    return "\n\n".join(sections)
