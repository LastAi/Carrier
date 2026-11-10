# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Guided conversations: the user sits down with Claude to go through a list together.

Two kinds share this engine:

* ``health`` — a walk-through of a project's health findings (Debrief): Claude groups
  them (same cause, same area, similar priority), goes through them group by group,
  shows each one it talks about, and proposes decisions — one task for several
  findings, adding them to an existing task, accepting or dismissing them.
* ``tasks`` — tidying a project's task queues (Ready Room): merging overlapping tasks,
  moving them between queues, re-ordering, splitting, dropping stale ones.

Claude never changes anything itself. Each reply is a message plus **proposals**; the
user applies a proposal (possibly unticking some of the findings or tasks in it) or
skips it. Applying goes through the same Ready Room and health functions as the rest
of Carrier, so task history records it ("by Claude, approved by you").

Each turn is one ``Backend.ask`` with the current state of the list (re-read every
turn, so decisions made elsewhere show up) and the conversation so far. In a health
walk-through Claude may read the project's files (read-only tools) to check a finding.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from carrier.core.db import Store
from carrier.core.jsonutil import extract_json
from carrier.core.llm import READ_ONLY_TOOLS, Backend

KINDS = ("health", "tasks")
MAX_TURNS_KEPT = 30          # conversation turns sent back each time
MAX_MESSAGE = 4000           # characters of the user's message
DEFAULT_MODEL = "sonnet"

COMMON = """Reply with JSON only:
{"message": "what you say to the user, markdown, short",
 "show": [ids of the items your message is about, most important first],
 "proposals": [ ... see below ... ]}
Refer to items by their ids exactly as written in the list ({ref}); they become links. Never claim a change was
made: proposals only take effect when the user applies them, and the list you get each turn shows what has
happened. Don't repeat proposals the user skipped unless they ask. Keep each reply to one group or one topic,
then wait for the user."""

HEALTH_SYSTEM = """You sit down with the owner of a software project to go through the findings of Debrief's
health checks (periodic checks of the whole project) and decide what to do with each — without letting the
task list bloat. You are calm, practical and brief.

How to work:
1. First reply: group the open findings — same root cause, same file or area, or small things of similar
   priority that can be fixed together. Give a short overview (how many groups, the most serious first) and
   start with the first group.
2. For each group: say what the problem is in plain words, how serious it really is, and propose what to do.
   Check the code with your read-only tools (Read, Grep, Glob) when a finding looks doubtful.
3. Prefer one task for a group over one task per finding, and adding to an existing open task when one
   already covers the area (the project's open tasks are listed).
4. Accept = real but fine as it is (stays in the report, not counted). Dismiss = not a real problem.

Proposals (each one is applied or skipped by the user as a whole; they may untick findings):
{"kind": "task", "ids": ["F12", "F15"], "title": "short task title", "details": "what to do and what done means",
 "project": "only when a LINKED project should fix it: its name"}
{"kind": "add_to_task", "ids": ["F12"], "task": "#31", "title": "what is added", "details": "what to do"}
{"kind": "accept", "ids": ["F3"], "why": "why it's fine as it is"}
{"kind": "dismiss", "ids": ["F9"], "why": "why it isn't a real problem"}

""" + COMMON.replace("{ref}", "findings F12, tasks #31")

TASKS_SYSTEM = """You sit down with the owner of a software project to tidy its task queues: fewer, clearer
tasks in a sensible order. You are calm, practical and brief. Nothing is lost: merged and dropped tasks stay in
the history.

How to work:
1. First reply: a short overview of what you see (duplicates and overlaps, tasks too big or too vague, stale
   ones, queues out of order) and start with the most useful change.
2. Go one topic at a time and ask when the owner's intent is unclear. Tasks being worked on (active, with a
   session) can't be merged, split or dropped.

Proposals (each one is applied or skipped by the user as a whole; they may untick tasks):
{"kind": "merge", "into": "#12", "ids": ["#14", "#20"], "title": "new title (optional)", "details": "new
  details for #12 (optional; the merged tasks' details are appended to it anyway)", "why": "..."}
{"kind": "move", "ids": ["#14"], "queue": "main" | "group name" | "new:Group name", "priority": "high|normal|low|backlog
  (new groups only)", "why": "..."}
{"kind": "order", "queue": "main" | "group name", "ids": ["#3", "#1", "#7"], "why": "..."}
{"kind": "split", "id": "#12", "keep": "what stays in #12", "title": "new task title", "details": "new task details",
  "why": "..."}
{"kind": "edit", "id": "#12", "title": "clearer title", "details": "clearer details", "why": "..."}
{"kind": "drop", "ids": ["#9"], "why": "why it isn't needed any more"}

""" + COMMON.replace("{ref}", "tasks #12")


@dataclass
class Proposal:
    id: int
    kind: str
    data: dict[str, Any]
    status: str = "open"         # open | applied | skipped | failed
    result: str = ""


@dataclass
class Turn:
    role: str                    # "user" | "claude"
    text: str
    show: list[str] = field(default_factory=list)
    proposals: list[int] = field(default_factory=list)
    at: float = field(default_factory=time.time)


class Guide:
    """One guided conversation for one project."""

    def __init__(self, store: Store, kind: str, pid: int, model: str | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"Unknown guide: {kind}")
        if store.project(pid) is None:
            raise ValueError("Unknown project.")
        self.store, self.kind, self.pid = store, kind, pid
        self.model = model or DEFAULT_MODEL
        self.turns: list[Turn] = []
        self.proposals: dict[int, Proposal] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.busy = False

    # -- the list Claude sees ------------------------------------------------------------

    def _room(self) -> Any:
        from carrier.readyroom.manager import ReadyRoom

        return ReadyRoom(self.store)

    def _open_tasks(self) -> list[dict[str, Any]]:
        return self.store.tasks(self.pid, statuses=("planned", "active"))

    def _queue_label(self, gid: int | None) -> str:
        g = self.store.task_group(gid) if gid is not None else None
        return g["name"] if g else "main"

    def state_text(self) -> str:
        lines: list[str] = []
        if self.kind == "health":
            from carrier.debrief import health

            rep = health.report(self.store, self.pid)
            for t in rep["themes"]:
                lines.append(f"## {t['theme']}")
                for f in t["findings"]:
                    if f["status"] not in ("open", "tasked"):
                        continue
                    where = f"{f['file']}:{f['line']}" if f["file"] and f["line"] else (f["file"] or "whole project")
                    extra = f" (task #{f['task_id']} made)" if f["status"] == "tasked" and f["task_id"] else ""
                    lines.append(f"- F{f['id']} [{f['severity']}{', new' if f['new'] else ''}{extra}] "
                                 f"{f['agent_title']} · {where} — {f['title']}: {f['detail'][:240]}")
            c = rep["counts"]
            lines.append(f"\n(Also {c['accepted']} accepted, {c['dismissed']} dismissed, {c['gone']} not found "
                         "again — not listed.)")
            from carrier.readyroom.context import linked_projects

            linked = linked_projects(self.store, self.pid)
            if linked:
                lines.append("\n## Linked projects (a task can go to one of these when the problem is theirs)")
                lines += [f"- {q['name']}: {q['note'] or '(no note)'}" for q in linked]
            lines.append("\n## The project's open tasks")
        groups = {g["id"]: g for g in self.store.task_groups(self.pid)}
        by_queue: dict[int | None, list[dict[str, Any]]] = {}
        for t in self._open_tasks():
            by_queue.setdefault(t.get("group_id"), []).append(t)
        for gid in [None] + list(groups):
            items = by_queue.get(gid, [])
            if gid is not None:
                g = groups[gid]
                head = f"### Queue “{g['name']}” (priority {g.get('priority', 'normal')})"
            else:
                head = "### Main queue"
            lines.append(head + ("" if items else " — empty"))
            for t in items:
                tags = (" [routine]" if t.get("routine_id") else "") + (" [review fix]" if t.get("fix_review") else "") \
                    + (" [active]" if t["status"] == "active" else "")
                details = (t.get("details") or "").strip().replace("\n", " ")
                lines.append(f"- #{t['id']}{tags} {t['title']}: {details[:300 if self.kind == 'tasks' else 120]}")
        return "\n".join(lines) or "(nothing)"

    # -- a turn ---------------------------------------------------------------------------

    def _history(self) -> str:
        out = []
        for t in self.turns[-MAX_TURNS_KEPT:]:
            if t.role == "user":
                out.append(f"USER: {t.text}")
            else:
                props = []
                for pid in t.proposals:
                    p = self.proposals[pid]
                    props.append(f"  proposal P{p.id} {p.kind} {json.dumps(p.data)[:300]} → {p.status}"
                                 + (f" ({p.result})" if p.result else ""))
                out.append(f"YOU: {t.text}" + ("\n" + "\n".join(props) if props else ""))
        return "\n\n".join(out) or "(this is the start)"

    def _prompt(self, text: str) -> str:
        p = self.store.project(self.pid) or {}
        return (f"# Project: {p.get('name', '')}\nGoal: {(p.get('goal') or '').strip() or '(none given)'}\n\n"
                f"# The list now\n{self.state_text()}\n\n# The conversation so far\n{self._history()}\n\n"
                f"# The user now says\n{text}")

    async def say(self, backend: Backend, text: str) -> dict[str, Any]:
        """The user's message (empty: "let's start") → Claude's reply with its proposals."""
        text = (text or "").strip()[:MAX_MESSAGE] or ("Let's start." if not self.turns else "Go on.")
        with self._lock:
            if self.busy:
                raise ValueError("Claude is still answering.")
            self.busy = True
        try:
            system = HEALTH_SYSTEM if self.kind == "health" else TASKS_SYSTEM
            kwargs: dict[str, Any] = {}
            if self.kind == "health":
                from carrier.debrief.projects import project_spec

                spec = project_spec(self.store, self.pid)
                kwargs = {"tools": READ_ONLY_TOOLS, "cwd": spec.path, "max_turns": 12,
                          "add_dirs": [str(p) for _lbl, p in spec.extras]}
            prompt = self._prompt(text)
            self.turns.append(Turn("user", text))  # kept even if Claude's answer fails, as the page shows it
            reply = await backend.ask(system=system, prompt=prompt, model=self.model, **kwargs)
            turn = self._parse(reply)
            self.turns.append(turn)
            return self.view_turn(turn)
        finally:
            self.busy = False

    def _parse(self, reply: str) -> Turn:
        try:
            data = extract_json(reply)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return Turn("claude", reply.strip()[:8000] or "(no answer)")
        message = str(data.get("message", "")).strip()[:8000]
        show = [str(x).strip() for x in (data.get("show") or []) if str(x).strip()][:30]
        turn = Turn("claude", message or "(no message)", show=show)
        for raw in (data.get("proposals") or [])[:12]:
            if not isinstance(raw, dict):
                continue
            try:
                kind, clean = self._check(raw)
            except ValueError as exc:
                turn.text += f"\n\n*(A proposal was left out: {exc})*"
                continue
            p = Proposal(next(self._ids), kind, clean)
            self.proposals[p.id] = p
            turn.proposals.append(p.id)
        return turn

    # -- checking and applying proposals ----------------------------------------------------

    def _finding_ids(self, raw: Any, statuses: tuple[str, ...]) -> list[int]:
        """Findings named in ``raw`` that are this project's and in one of ``statuses`` now."""
        rows = self.store._q("SELECT id, status FROM health_findings WHERE project_id=?", (self.pid,))
        valid = {r["id"] for r in rows if r["status"] in statuses}
        out = []
        for x in raw or []:
            s = str(x).strip().upper().lstrip("F")
            if s.isdigit() and int(s) in valid and int(s) not in out:
                out.append(int(s))
        return out

    def _task_id(self, raw: Any, editable: bool = False) -> int | None:
        """An open task of this project; with ``editable``, not one a session is working on."""
        s = str(raw or "").strip().lstrip("#")
        if not s.isdigit():
            return None
        t = self.store.task(int(s))
        if t is None or t["project_id"] != self.pid or t["status"] not in ("planned", "active"):
            return None
        if editable and self._room()._active_sessions(task_id=t["id"]):
            return None
        return t["id"]

    def _task_ids(self, raw: Any, editable: bool = True) -> list[int]:
        out = []
        for x in raw or []:
            tid = self._task_id(x, editable)
            if tid is not None and tid not in out:
                out.append(tid)
        return out

    def _check(self, raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = str(raw.get("kind", "")).strip()
        s = lambda k, n=4000: str(raw.get(k) or "").strip()[:n]  # noqa: E731
        if self.kind == "health":
            # A task only for open findings; accept/dismiss also for those made into tasks (as in the report).
            ids = self._finding_ids(raw.get("ids"), ("open",) if kind in ("task", "add_to_task") else ("open", "tasked"))
            if not ids:
                raise ValueError("it named no open findings in the list")
            if kind == "task":
                if not s("title"):
                    raise ValueError("a task needs a title")
                target = None
                if s("project"):
                    from carrier.readyroom.context import linked_projects

                    match = [q for q in linked_projects(self.store, self.pid)
                             if q["name"].lower() == s("project").lower()]
                    if not match:
                        raise ValueError(f"“{s('project')}” isn't a linked project")
                    target = {"id": match[0]["id"], "name": match[0]["name"]}
                return kind, {"ids": ids, "title": s("title", 120), "details": s("details"), "project": target}
            if kind == "add_to_task":
                tid = self._task_id(raw.get("task"), editable=True)
                if tid is None:
                    raise ValueError("it named no open task of this project that isn't being worked on")
                t = self.store.task(tid)
                return kind, {"ids": ids, "task": tid, "task_title": t["title"], "title": s("title", 120)
                              or "Health findings", "details": s("details")}
            if kind in ("accept", "dismiss"):
                return kind, {"ids": ids, "why": s("why", 600)}
            raise ValueError(f"unknown kind “{kind}”")
        if kind == "merge":
            into = self._task_id(raw.get("into"), editable=True)
            ids = [i for i in self._task_ids(raw.get("ids")) if i != into]
            if into is None or not ids:
                raise ValueError("a merge needs open tasks no session is working on: one to keep, others to merge")
            return kind, {"into": into, "into_title": self.store.task(into)["title"], "ids": ids,
                          "title": s("title", 120), "details": s("details", 8000), "why": s("why", 600)}
        if kind == "move":
            ids = self._task_ids(raw.get("ids"), editable=False)
            queue = s("queue", 80) or "main"
            if not ids:
                raise ValueError("a move names no open tasks")
            self._resolve_queue(queue, create=False)
            prio = s("priority", 10).lower()
            return kind, {"ids": ids, "queue": queue,
                          "priority": prio if prio in ("high", "normal", "low", "backlog") else "normal",
                          "why": s("why", 600)}
        if kind == "order":
            queue = s("queue", 80) or "main"
            gid = self._resolve_queue(queue, create=False)
            ids = self._task_ids(raw.get("ids"), editable=False)
            if gid == "new" or not ids:
                raise ValueError("an order needs an existing queue and its tasks")
            return kind, {"queue": queue, "ids": ids, "why": s("why", 600)}
        if kind == "split":
            tid = self._task_id(raw.get("id"), editable=True)
            if tid is None or not s("title") or not s("keep"):
                raise ValueError("a split needs an open task no session is working on, what stays in it, and a "
                                 "title for the new one")
            return kind, {"id": tid, "keep": s("keep", 8000), "title": s("title", 120), "details": s("details", 8000),
                          "why": s("why", 600)}
        if kind == "edit":
            tid = self._task_id(raw.get("id"), editable=True)
            if tid is None or not (s("title") or s("details")):
                raise ValueError("an edit needs an open task no session is working on, and a new title or details")
            return kind, {"id": tid, "title": s("title", 120), "details": s("details", 8000), "why": s("why", 600)}
        if kind == "drop":
            ids = self._task_ids(raw.get("ids"))
            if not ids:
                raise ValueError("a drop names no open tasks that no session is working on")
            return kind, {"ids": ids, "why": s("why", 600) or "No longer needed."}
        raise ValueError(f"unknown kind “{kind}”")

    def _resolve_queue(self, name: str, create: bool) -> int | None | str:
        """A queue name → group id (None = main). ``new:Name`` → "new" (or created, with ``create``)."""
        name = name.strip()
        if name.lower() in ("", "main", "main queue"):
            return None
        if name.lower().startswith("new:"):
            return "new"
        for g in self.store.task_groups(self.pid):
            if g["name"].strip().lower() == name.lower():
                return g["id"]
        raise ValueError(f"there's no queue called “{name}”")

    def _as_raw(self, kind: str, d: dict[str, Any]) -> dict[str, Any]:
        """A proposal's data back in Claude's form, to check it again as it is now."""
        raw: dict[str, Any] = {"kind": kind, **{k: v for k, v in d.items() if k not in ("ids", "into", "id", "task",
                                                                                     "project")}}
        pre = "F" if self.kind == "health" else "#"
        if "ids" in d:
            raw["ids"] = [f"{pre}{i}" for i in d["ids"]]
        for k in ("into", "id", "task"):
            if k in d:
                raw[k] = f"#{d[k]}"
        if d.get("project"):
            raw["project"] = d["project"]["name"]
        return raw

    def apply(self, prop_id: int, edits: dict[str, Any] | None = None) -> dict[str, Any]:
        """Apply a proposal; ``edits`` may narrow ``ids`` and change text fields. It is checked again
        first (tasks and findings may have changed since it was proposed): if it no longer holds,
        nothing changes and it stays open, with the reason as the error."""
        with self._lock:
            p = self.proposals.get(int(prop_id))
            if p is None:
                raise ValueError("Unknown proposal.")
            if p.status != "open":
                raise ValueError(f"That proposal was {p.status} already.")
            p.status = "applying"
        try:
            d = dict(p.data)
            edits = edits or {}
            if "ids" in edits and "ids" in d:
                keep = {int(str(x).lstrip("#F")) for x in edits["ids"]}
                d["ids"] = [i for i in d["ids"] if i in keep]
                if not d["ids"]:
                    raise ValueError("Nothing is ticked.")
            for k in ("title", "details", "keep", "why"):
                if k in edits and k in d:
                    d[k] = str(edits[k]).strip()
            kind, checked = self._check(self._as_raw(p.kind, d))  # as things are now
            dropped = [i for i in d.get("ids", []) if i not in checked.get("ids", [])]
            if dropped:
                pre = "F" if self.kind == "health" else "#"
                raise ValueError("No longer possible for " + ", ".join(f"{pre}{i}" for i in dropped)
                                 + " (decided, closed or being worked on since). Untick them, or skip this.")
            d = checked
        except Exception:
            p.status = "open"  # nothing was changed
            raise
        try:
            p.result = self._apply(kind, d)
            p.status = "applied"
        except Exception as exc:
            p.status, p.result = "failed", str(exc)
            raise
        p.data = d
        return self.view_proposal(p)

    def skip(self, prop_id: int) -> dict[str, Any]:
        with self._lock:
            p = self.proposals.get(int(prop_id))
            if p is None:
                raise ValueError("Unknown proposal.")
            if p.status == "open":
                p.status = "skipped"
        return self.view_proposal(p)

    def _apply(self, kind: str, d: dict[str, Any]) -> str:
        room = self._room()
        if self.kind == "health":
            from carrier.debrief import health

            if kind in ("accept", "dismiss"):
                done = health.act_many(self.store, self.pid, d["ids"], kind)
                return f"{'Accepted' if kind == 'accept' else 'Dismissed'} {done} finding(s)."
            listing = health.findings_text(self.store, self.pid, d["ids"])
            if kind == "task":
                details = (d["details"] + "\n\n" if d["details"] else "") + listing
                dest = (d.get("project") or {}).get("id") or self.pid
                if dest != self.pid:
                    details += f"\n\n(Found by a health check of {room._project_name(self.pid)}.)"
                proj = room.add_task(dest, d["title"], details, by="health check",
                                     note="Planned with Claude in a health walk-through")
                tid = max(t["id"] for t in proj["tasks"] if t["title"] == d["title"])
            else:
                tid = d["task"]
                room.add_to_task(tid, d["title"], (d["details"] + "\n\n" if d["details"] else "") + listing,
                                 by="claude", note="From a health walk-through, approved by you")
            health.mark_tasked(self.store, self.pid, d["ids"], tid)
            where = "" if kind != "task" or not d.get("project") else f" in {d['project']['name']}"
            return f"{len(d['ids'])} finding(s) → task #{tid}{where}."
        note = ("Tidying with Claude: " + d.get("why", "")).strip().rstrip(":")
        if kind == "merge":
            for other in d["ids"]:
                room.merge_tasks(d["into"], other, by="claude", note=note)
            if d.get("title") or d.get("details"):
                t = self.store.task(d["into"])
                room.update_task(d["into"], title=d.get("title") or None,
                                 details=(d["details"] + "\n\n" + t["details"]).strip() if d.get("details") else None,
                                 by="claude", note=note)
            return f"Merged {', '.join('#' + str(i) for i in d['ids'])} into #{d['into']}."
        if kind == "move":
            gid = self._resolve_queue(d["queue"], create=True)
            if gid == "new":
                name = d["queue"].split(":", 1)[1].strip()
                if not name:
                    raise ValueError("The new queue needs a name.")
                room.save_group(self.pid, name, d.get("priority", "normal"))
                gid = max(g["id"] for g in self.store.task_groups(self.pid) if g["name"] == name)
            for tid in d["ids"]:
                room.set_task_group(tid, gid)
            return f"Moved {len(d['ids'])} task(s) to {room._queue_name(self.pid, gid)}."
        if kind == "order":
            gid = self._resolve_queue(d["queue"], create=False)
            room.reorder_queue(self.pid, gid, d["ids"])
            return f"Re-ordered the {room._queue_name(self.pid, gid)}."
        if kind == "split":
            t = room.split_task(d["id"], d["keep"], d["title"], d["details"], by="claude", note=note)
            return f"Split: new task #{t['id']} “{t['title']}”."
        if kind == "edit":
            room.update_task(d["id"], title=d.get("title") or None, details=d.get("details") or None,
                             by="claude", note=note)
            return f"Edited #{d['id']}."
        if kind == "drop":
            for tid in d["ids"]:
                room.drop_task(tid, d["why"], by="claude", note=note)
            return f"Dropped {len(d['ids'])} task(s); they stay in the history."
        raise ValueError(f"Unknown proposal: {kind}")

    # -- views ------------------------------------------------------------------------

    def view_proposal(self, p: Proposal) -> dict[str, Any]:
        return {"id": p.id, "kind": p.kind, "data": p.data, "status": p.status, "result": p.result}

    def view_turn(self, t: Turn) -> dict[str, Any]:
        return {"role": t.role, "text": t.text, "show": t.show,
                "proposals": [self.view_proposal(self.proposals[i]) for i in t.proposals]}

    def view(self) -> dict[str, Any]:
        return {"kind": self.kind, "pid": self.pid, "busy": self.busy, "model": self.model,
                "turns": [self.view_turn(t) for t in self.turns]}
