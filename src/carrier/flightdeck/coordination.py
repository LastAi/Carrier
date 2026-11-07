# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Coordination: keeping parallel Claude Code sessions aware of each other.

This module is pure logic over the :class:`~carrier.core.db.Store`.
The session runner calls it from three hooks:

``check_edit``   (PreToolUse, before Claude edits a file)
    Is anyone else working on this? Returns human-readable notices. The
    session's *policy* decides what happens with them: tell Claude
    ("warn", the default), ask the user, or block the edit.

``record_edit``  (PostToolUse, after an edit)
    Store the edit. If the file is a *contract* — a file declared on a link
    between projects — queue a notice for every active session in the
    linked project(s).

``briefing``     (UserPromptSubmit, when the user sends a message)
    Collect queued notices plus a short "who else is working on related
    things" summary, and hand it to Claude as extra context.

What counts as related
----------------------
* other active sessions in the **same project**, and
* active sessions in projects **linked** to this one.

Unlinked projects are never mentioned to each other, so a project with no ties
to the others is never interrupted.

Notices are written for Claude *and* the human: short, factual, naming the
other session's task, the file, and how long ago.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from carrier.core.db import Store

# How far back an edit by another session still counts as "recent".
RECENT_SECONDS = 6 * 3600
MAX_BRIEFING_LINES = 12


@dataclass
class Notice:
    kind: str  # "same_file" | "contract" | "worktree_overlap"
    text: str
    other_session: int | None = None
    # True when another *active* session is actually involved. Only these
    # may trigger the "ask" / "block" policies; the rest is just information
    # (e.g. a contract file whose other side has no session running).
    conflict: bool = True


# --------------------------------------------------------------------------
# Glob matching for contract patterns
# --------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob with ``**`` support into a regex (POSIX paths).

    ``*`` matches within one folder, ``**/`` any number of folders, ``?`` one
    character. A pattern without a slash matches the file name anywhere
    (``*.schema.json``), like .gitignore.
    """
    pat = pattern.strip().replace("\\", "/").lstrip("/")
    if "/" not in pat:
        pat = "**/" + pat
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z", re.IGNORECASE)


def matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns if p.strip())


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def ago(ts: float, now: float | None = None) -> str:
    s = max(0, int((now or time.time()) - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def _cap(text: str) -> str:
    """Uppercase the first letter only (str.capitalize would lowercase names)."""
    return text[:1].upper() + text[1:]


def _label(store: Store, session: dict[str, Any]) -> str:
    project = store.project(session["project_id"])
    pname = project["name"] if project else "?"
    return f"the {pname} session #{session['id']} (task: “{session['task']}”)"


def _contract_sides(store: Store, project_id: int | None) -> list[tuple[dict[str, Any], list[str], int, list[str]]]:
    """For each link of a project: (link, my_globs, other_project_id, other_globs)."""
    out: list[tuple[dict[str, Any], list[str], int, list[str]]] = []
    if project_id is None:  # standalone tasks have no links
        return out
    for link in store.links(project_id):
        if link["project_a"] == project_id:
            out.append((link, link["contracts_a"], link["project_b"], link["contracts_b"]))
        else:
            out.append((link, link["contracts_b"], link["project_a"], link["contracts_a"]))
    return out


# --------------------------------------------------------------------------
# The three entry points
# --------------------------------------------------------------------------


def check_edit(store: Store, session: dict[str, Any], path: str,
               project_id: int | None = None) -> list[Notice]:
    """Notices for ``session`` about to edit ``path``.

    ``path`` is relative to the project that owns the file — normally the
    session's own project, but ``project_id`` can name another registered
    project when a session edits files outside its folder.
    """
    notices: list[Notice] = []
    now = time.time()
    pid = project_id if project_id is not None else session["project_id"]

    # 1. Another active session touched this file — from any project, since a
    #    session may edit files of another registered project.
    others = {s["id"]: s for s in store.sessions(active_only=True) if s["id"] != session["id"]}
    seen: set[int] = set()
    for e in store.edits(pid, path=path, since=now - RECENT_SECONDS, exclude_session=session["id"]):
        other = others.get(e["session_id"])
        if other is None or other["id"] in seen:
            continue
        seen.add(other["id"])
        if other["worktree"] or session["worktree"]:
            notices.append(Notice("worktree_overlap",
                                  f"{_cap(_label(store, other))} also changed {path} "
                                  f"{ago(e['ts'], now)} in a separate copy; the two versions will need "
                                  "merging later — keep the change focused.", other["id"]))
        else:
            notices.append(Notice("same_file",
                                  f"{_cap(_label(store, other))} changed {path} {ago(e['ts'], now)} "
                                  "in this same folder. Re-read the file before editing and don't undo "
                                  "its work.", other["id"]))

    # 2. The file is a shared contract with a linked project.
    for link, mine, other_pid, other_globs in _contract_sides(store, pid):
        if not matches(path, mine):
            continue
        other_project = store.project(other_pid)
        oname = other_project["name"] if other_project else "?"
        active = store.sessions(other_pid, active_only=True)
        who = "; ".join(f"#{s['id']} “{s['task']}”" for s in active) or "no session is active there right now"
        why = f" ({link['note']})" if link["note"] else ""
        notices.append(Notice("contract",
                              f"{path} is a shared contract with {oname}{why}. Active there: {who}. "
                              "Keep changes compatible, or say clearly in your reply what the other "
                              "side must update.", conflict=bool(active)))
    return notices


def record_edit(store: Store, session: dict[str, Any], path: str, tool: str,
                project_id: int | None = None) -> list[int]:
    """Store an edit; notify linked sessions if it touched a contract.

    Returns the ids of the sessions that were notified.
    """
    pid = project_id if project_id is not None else session["project_id"]
    store.add_edit(session["id"], pid, path, tool)
    notified: list[int] = []
    project = store.project(pid)
    pname = project["name"] if project else "?"
    for link, mine, other_pid, _ in _contract_sides(store, pid):
        if not matches(path, mine):
            continue
        for other in store.sessions(other_pid, active_only=True):
            if other["id"] == session["id"]:
                continue
            # One pending notice per (file, source) is enough — exact path match.
            dup = any(ev["source"] == session["id"] and ev["path"] == path
                      for ev in store.pending_events(other["id"]))
            if dup:
                continue
            store.add_event(other["id"], session["id"], "contract_changed",
                            f"{pname} session #{session['id']} (“{session['task']}”) changed the shared "
                            f"contract file {path}" + (f" — {link['note']}" if link["note"] else "") +
                            ". Check whether this project needs a matching update.", path=path)
            notified.append(other["id"])
    return notified


def related_sessions(store: Store, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Active sessions in the same project, in linked projects, or that have
    recently edited files of this project from elsewhere."""
    pid = session["project_id"]
    if pid is None:  # a standalone task is related to nothing
        return []
    pids = {pid} | {other for _, _, other, _ in _contract_sides(store, pid)}
    out = []
    for p in pids:
        out += [s for s in store.sessions(p, active_only=True) if s["id"] != session["id"]]
    seen = {s["id"] for s in out}
    visitors = {e["session_id"] for e in store.edits(pid, since=time.time() - RECENT_SECONDS)}
    for s in store.sessions(active_only=True):
        if s["id"] in visitors and s["id"] not in seen and s["id"] != session["id"]:
            out.append(s)
    return out


def briefing(store: Store, session: dict[str, Any]) -> str | None:
    """Context for Claude when the user sends a message, or None if nothing new.

    The first briefing of a session lists related work; later ones only
    report what changed since the last briefing.
    """
    lines: list[str] = []
    pending = store.pending_events(session["id"])
    # Deliver at most MAX_BRIEFING_LINES notices now; the rest stay pending
    # for the next message instead of being dropped.
    events = pending[:MAX_BRIEFING_LINES]
    for ev in events:
        lines.append(f"- {ev['text']}")
    if len(pending) > len(events):
        lines.append(f"- (+{len(pending) - len(events)} more notices will follow with the next message.)")

    first = not session["last_briefed"]
    related = related_sessions(store, session)
    room = max(0, MAX_BRIEFING_LINES - len(lines))
    extra: list[str] = []
    if first and related:
        for other in related:
            files = store.session_files(other["id"])[:5]
            touched = f" — has edited {', '.join(files)}" if files else ""
            extra.append(f"- {_cap(_label(store, other))} is active{touched}.")
    elif related:
        # Files related sessions edited since our last briefing (in any project).
        since = session["last_briefed"]
        for other in related:
            new = [e["path"] for e in store.edits_by_session(other["id"], since=since)]
            if new:
                uniq = list(dict.fromkeys(new))[:5]
                extra.append(f"- {_cap(_label(store, other))} edited {', '.join(uniq)} since your last message.")
    lines += extra[:room]

    store.mark_delivered([e["id"] for e in events])
    store.update_session(session["id"], last_briefed=time.time())
    if not lines:
        return None
    return ("[Carrier Flight Deck] Other Claude Code sessions are working on related projects. "
            "Take this into account; it is information, not a request from the user:\n" + "\n".join(lines))
