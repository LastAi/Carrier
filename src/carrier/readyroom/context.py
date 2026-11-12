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


def permissions_section(store: Store, project_id: int | None) -> str:
    """What runs without asking in this project, so Claude can work without needless prompts.

    Generated from the project's pre-approved actions (Ready Room). It is guidance: what
    actually runs without asking is still decided by Carrier's checks
    (flightdeck/sdk.preapproved and flightdeck/shellcheck)."""
    from carrier.flightdeck.sdk import ALLOW_DEFAULT, group_entries
    from carrier.flightdeck.shellcheck import valid_entries

    saved = store.get_setting(f"project:{project_id}:allow", {}) if project_id is not None else {}
    allow = {**ALLOW_DEFAULT, **{k: v for k, v in (saved or {}).items() if k in ALLOW_DEFAULT}}
    allow["commands"] = valid_entries(list(allow.get("commands") or []))
    free = []
    if allow.get("read"):
        free.append("reading and searching files in the project's folders")
    if allow.get("edit"):
        free.append("creating and editing files in its work folders (not .git, .claude, .vscode and the like)")
    if allow.get("web_search"):
        free.append("web searches")
    if allow.get("web_fetch"):
        sites = allow.get("sites") or []
        free.append("fetching web pages" + (f" from {', '.join(sites)}" if sites else ""))
    programs = group_entries(allow)
    if allow.get("tools"):
        free.append("these tools: " + ", ".join(allow["tools"]))
    lines = ["## Permissions in this project"]
    if free or programs:
        lines.append("These run without asking the user: " + ("; ".join(free) if free else "no file or web actions")
                     + ".")
        if programs:
            lines.append("Shell programs that run without asking (pipes and && between them are fine): "
                         + ", ".join(programs) + ".")
    else:
        lines.append("Nothing is pre-approved for this project.")
    if programs:
        lines.append("A shell command still asks the user if it uses backticks, $(( )), a loop other than "
                     "for over fixed words, if or while, a here-document, $( ) whose output is passed to a program "
                     "other than echo or printf, a variable it doesn't set itself, cd outside the project's folders, settings "
                     "such as PATH=, JAVA_HOME= or GIT_…=, or any program not listed above.")
    else:
        lines.append("Shell commands ask the user (unless the session's permission mode allows them).")
    lines += [
        "So: use the Read, Grep, Glob and Edit tools for files rather than cat, grep or sed in the shell; keep "
        "commands to one job with literal paths (write a tool's full path rather than looking it up with $( )); "
        "and only use something that needs approval when the task requires it, or when it saves many steps — "
        "then say why in one line before the command.",
    ]
    return "\n".join(lines)


def _rules_block(own: str, sets: list[dict[str, Any]]) -> str:
    parts = []
    if own.strip():
        parts.append(own.strip())
    for rs in sets:
        parts.append(f"({rs['name']})\n{rs['body'].strip()}")
    return _cap("\n\n".join(parts)) if parts else ""


def linked_projects(store: Store, pid: int) -> list[dict[str, Any]]:
    """Projects linked to this one (Linked projects), with the note and each side's shared files."""
    out = []
    for l in store.links(pid):
        mine_a = l["project_a"] == pid
        other = l["project_b"] if mine_a else l["project_a"]
        p = store.project(other)
        if p is None:
            continue
        out.append({"id": other, "name": p["name"], "path": p["path"], "note": l["note"],
                    "mine": l["contracts_a"] if mine_a else l["contracts_b"],
                    "theirs": l["contracts_b"] if mine_a else l["contracts_a"]})
    return out


def _linked_section(store: Store, pid: int, for_review: bool) -> str:
    """Linked projects (Ready Room → Linked projects): who depends on whom, and how to hand work over."""
    linked = linked_projects(store, pid)
    if not linked:
        return ""
    lines = []
    for q in linked:
        line = f"- {q['name']} (main folder {q['path']})" + (f": {q['note']}" if q["note"] else "")
        if q["mine"] or q["theirs"]:
            line += (f"\n  Shared files — this project's: {', '.join(q['mine']) or 'none listed'}; "
                     f"theirs: {', '.join(q['theirs']) or 'none listed'}")
        lines.append(line)
    if for_review:
        return ("## Linked projects\nOther projects that depend on this one, or it on them (their changes are not "
                "this project's work):\n" + "\n".join(lines))
    return ("## Linked projects\nThese projects depend on this one, or it on them. Don't edit their files. When your "
            "work changes something they rely on (above all the shared files), or needs something from them, propose "
            "the work for them with propose_work (project = their name); list_tasks (project = their name) shows "
            "their queues. The user approves it first.\n" + "\n".join(lines))


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
        own_work = [f for f in work if f.get("share") != "shared"]
        shared = [f for f in work if f.get("share") == "shared"]
        if own_work:
            head += ("\nThis project also works in these folders (read and edit them when the task "
                     "needs it):\n" + "\n".join(f"- {f['label']}: {f['path']}" for f in own_work))
        if shared:
            lines = []
            for f in shared:
                part = ", ".join(f.get("part") or []) or "none listed"
                lines.append(f"- {f['label']}: {f['path']} — this project's part: {part}")
            head += ("\nShared spaces — other projects work in these folders too. This project owns only its part "
                     "(and new files it creates there, which become its own). Changing other existing files there "
                     "needs the user's OK each time, so stay within your part unless the task truly needs more:\n"
                     + "\n".join(lines))
        from carrier.readyroom import ownership

        foreign = ownership.foreign_mains(store, project)
        if foreign:
            head += ("\nOther projects' folders inside this project's folders — READ ONLY (each belongs to its "
                     "project alone; edits are blocked):\n" + "\n".join(f"- {q['name']}: {q['path']}" for q in foreign))
        if ref:
            head += ("\nReference folders — READ ONLY. Look things up there to understand or complete "
                     "the task, but never create, edit or delete files in them (edits are blocked):\n"
                     + "\n".join(f"- {f['label']}: {f['path']}" for f in ref))
        sections.append(head)
        linked = _linked_section(store, project["id"], for_review)
        if linked:
            sections.append(linked)
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
        links = store.task_links(task["id"]) if task.get("id") else []
        if links:
            rows = []
            for l in links:
                where = f"{l['other_project_name'] or 'standalone'} #{l['other_id']} “{l['other_title']}” ({l['other_status']})"
                if l["kind"] == "waits_on":
                    rows.append(f"- Waits on {where}" if l["direction"] == "out" else f"- {where} waits on this task")
                else:
                    rows.append(f"- This task's work opened {where}" if l["direction"] == "out"
                                else f"- Opened by the work on {where}: read it (list_tasks / get_project) to know why")
            t += "\n\n### Linked tasks\n" + "\n".join(rows)
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

    if not for_review:
        sections.append(permissions_section(store, project["id"] if project else None))

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
