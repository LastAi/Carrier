# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""SessionRunner — one live Claude Code conversation in Flight Deck.

A runner owns one SDK client (``ClaudeSDKClient``) and lives on the Flight
Deck event loop. It:

* sends the user's messages and streams Claude's replies as events,
* shows Claude Code's permission prompts in the UI (``can_use_tool``) and
  waits for the user's answer,
* runs Flight Deck's coordination **hooks in-process** — no extra scripts in
  the project:

  ===================  ======================================================
  UserPromptSubmit     add a briefing about related sessions' work
  PreToolUse (edits)   check for conflicts; warn Claude / ask / block
  PostToolUse (edits)  record the edit; notify linked sessions on contracts
  ===================  ======================================================

The runner never decides policy itself: conflicts are found by
``coordination.py`` and the session's ``policy`` (warn / ask / block)
decides the response.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from carrier.flightdeck import coordination, sdk

if TYPE_CHECKING:  # pragma: no cover
    from carrier.flightdeck.manager import FlightDeck

ClientFactory = Callable[[Any], Any]
_req_ids = itertools.count(1)


class SessionRunner:
    def __init__(self, deck: "FlightDeck", session_id: int, client_factory: ClientFactory) -> None:
        self.deck = deck
        self.sid = session_id
        self.client_factory = client_factory
        self.client: Any = None
        self.busy = False
        self.interrupted = False  # set by interrupt() for the turn in progress
        self.pending: dict[str, dict[str, Any]] = {}  # req_id -> {future, tool, summary}
        self._routed_asks = 0  # AskUserQuestion calls that reached Carrier, not yet matched to a result
        self._ask_uses: dict[str, Any] = {}  # AskUserQuestion tool-use id -> its questions
        self.always_allow: set[str] = set()  # sdk.allow_key() values allowed for the rest of the session
        self.session_programs: list[str] = []  # shell programs allowed for the rest of the session
        self._claims: dict[str, dict[str, Any]] = {}  # new files being written in shared spaces
        self._claim_lock = threading.Lock()
        # Claude Code reports cost per connection (and restarts it on /clear);
        # the stored total is base + the current connection's running cost.
        self.cost_base = 0.0

    # -- helpers -------------------------------------------------------------

    @property
    def row(self) -> dict[str, Any]:
        row = self.deck.store.session(self.sid)
        assert row is not None
        return row

    @property
    def cwd(self) -> Path:
        return Path(self.row["cwd"])

    def emit(self, event: dict[str, Any], persist: bool = True) -> None:
        self.deck.emit(self.sid, event, persist=persist)

    def set_status(self, status: str) -> None:
        self.deck.store.update_session(self.sid, status=status)
        self.emit({"type": "status", "status": status}, persist=False)

    def _owners(self, file_path: str) -> list[tuple[int, str]]:
        """[(project_id, project-relative path)] for an absolute file path (own project first)."""
        return self.deck.owners(self.row, file_path)

    # -- lifecycle -------------------------------------------------------------

    async def start(self, resume: str | None = None) -> None:
        row = self.row
        hooks = {
            "UserPromptSubmit": [sdk.hook_matcher(None, self._on_prompt)],
            "PreToolUse": [sdk.hook_matcher("|".join(sorted(sdk.EDIT_TOOLS)), self._before_edit)],
            "PostToolUse": [sdk.hook_matcher("|".join(sorted(sdk.EDIT_TOOLS)), self._after_edit)],
        }
        hooks["PreToolUse"].append(sdk.hook_matcher("Read", self._before_read))
        options = sdk.build_options(cwd=self.cwd, model=row["model"], mode=row["mode"], resume=resume,
                                    effort=row.get("effort"),
                                    hooks=hooks, can_use_tool=self._can_use_tool,
                                    context=self.deck.context_for(row),
                                    add_dirs=self.deck.extra_dirs(row),
                                    carrier_server=sdk.carrier_tools(self.tool_handlers()))
        self.cost_base = float(row["cost_usd"] or 0.0)
        self.client = self.client_factory(options)
        await self.client.connect()
        # Reconnecting (new model or effort) while waiting out a usage limit keeps the wait.
        self.set_status("limited" if self.row["status"] == "limited" else "idle")

    def claim(self) -> bool:
        """Mark the session busy *before* a turn is scheduled.

        Called synchronously by the deck so that a second message can't slip
        in, and the status reads "working" immediately, while :meth:`send`
        is still waiting to start on the event loop.
        """
        with self._claim_lock:  # API threads may call this concurrently
            if self.busy:
                return False
            self.busy = True
        self.set_status("working")
        return True

    async def send(self, text: str, on_done: Callable[[str], None] | None = None,
                   on_fail: Callable[[], None] | None = None, images: list[dict[str, Any]] | None = None) -> None:
        """Send one user message and stream the reply (call :meth:`claim` first).

        ``on_done`` receives Claude's reply text when the turn finishes
        successfully (used to collect a task summary); ``on_fail`` is called
        instead if the turn was interrupted, failed, or ended without a result.
        """
        self.busy = True
        self.interrupted = False
        self._routed_asks = 0  # a new turn: unmatched counts from the last one don't carry over
        self._ask_uses.clear()
        self.emit({"type": "user", "text": text, **({"images": images} if images else {})})
        reply: list[str] = []
        ok = False
        limit_signs: list[str] = []  # why this turn looks stopped by a usage limit
        try:
            await self.client.query(sdk.user_message(text, images) if images else text)
            async for msg in self.client.receive_response():
                for ev in sdk.normalize(msg, self.cwd):
                    if ev["type"] == "text":
                        reply.append(ev["text"])
                    if ev["type"] == "result":
                        ok = not ev.get("is_error")
                        if ev.get("api_status") == 429:
                            limit_signs.append(ev.get("text") or "429")
                    if ev["type"] == "error" and ev.get("code") == "rate_limit":
                        limit_signs.append(ev.get("text") or "rate_limit")
                    if ev["type"] == "rate_limit" and ev.get("status") == "rejected" \
                            and ev.get("overage_status") not in ("allowed", "allowed_warning"):
                        limit_signs.append("rejected")
                    self._handle(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the deck alive; show the problem in the session
            traceback.print_exc()
            self.emit({"type": "error", "text": f"Claude Code error: {exc}"})
            self.set_status("error")
            if on_fail is not None:
                on_fail()
            return
        finally:
            self.busy = False
        limited = bool(limit_signs) and not ok and not self.interrupted
        if limited and self.row["status"] not in ("ended", "error"):
            self.deck.limit_hit(self.sid, "\n".join(limit_signs + reply))
        elif self.row["status"] not in ("ended", "error"):
            self.set_status("idle")
        if ok and (self.row.get("limit_tries") or 0):
            self.deck.store.update_session(self.sid, limit_tries=0)  # past the limit
        if ok and not self.interrupted and reply:
            if on_done is not None:
                on_done("\n\n".join(reply).strip())
        elif on_fail is not None:
            on_fail()

    def _handle(self, ev: dict[str, Any]) -> None:
        # Fallback: Claude Code asked a question without routing it to Carrier (e.g. a
        # changed version, or a mode that skips the permission callback). Show it in the
        # chat so the user can still answer, as a normal message.
        if ev["type"] == "tool_use" and ev.get("name") == "AskUserQuestion" and not ev.get("sub"):
            self._ask_uses[ev["id"]] = (ev.get("input") or {}).get("questions")
        elif ev["type"] == "tool_result" and ev.get("id") in self._ask_uses:
            questions = self._ask_uses.pop(ev["id"])
            if self._routed_asks > 0:
                self._routed_asks -= 1
            else:
                from carrier.readyroom.importer import clean_questions

                self.emit({"type": "question_fallback", "questions": clean_questions(questions)})
        if ev["type"] == "rate_limit":
            self.deck.on_rate_limit(self.sid, ev)  # recorded and, if it changed, a notice
            return
        if ev["type"] == "system":
            from carrier.core import usage

            usage.record_account(self.deck.store, ev.get("key_source"))
        if ev["type"] == "system" and ev.get("session"):
            self.deck.store.update_session(self.sid, sdk_session=ev["session"])
        if ev["type"] == "reset":
            self.cost_base = float(self.row["cost_usd"] or 0.0)
        if ev["type"] == "result":
            if ev.get("session"):
                self.deck.store.update_session(self.sid, sdk_session=ev["session"])
            total = self.cost_base + float(ev.get("cost") or 0.0)
            self.deck.store.update_session(self.sid, cost_usd=total)
            ev = {**ev, "cost": total}
        self.emit(ev, persist=ev["type"] != "live")

    async def interrupt(self) -> None:
        if self.client is not None and self.busy:
            # Release waiting permission prompts first, so the CLI is never
            # left waiting on an answer while we wait on the interrupt.
            for req in list(self.pending.values()):
                if not req["future"].done():
                    req["future"].set_result(("interrupt", "Interrupted by the user."))
            self.interrupted = True
            await self.client.interrupt()
            self.emit({"type": "notice", "kind": "interrupt", "text": "Interrupted."})

    async def stop(self) -> None:
        for req in list(self.pending.values()):
            if not req["future"].done():
                req["future"].set_result(("deny", "Session ended."))
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None

    async def set_model(self, model: str | None) -> None:
        if self.client is not None:
            await self.client.set_model(model)
        self.deck.store.update_session(self.sid, model=model)

    async def set_mode(self, mode: str) -> None:
        if self.client is not None:
            await self.client.set_permission_mode(mode)
        self.deck.store.update_session(self.sid, mode=mode)

    # -- permissions -----------------------------------------------------------

    def tool_handlers(self) -> dict[str, Any]:
        """Carrier's tools for this session (see sdk.TOOLS)."""
        return {
            "propose_rule": lambda a: (self._on_propose_rule(a),
                                       "Shown to the user as a rule card to edit and approve. Carry on.")[1],
            "list_projects": lambda a: self.deck.projects_text(),
            "get_project": lambda a: self.deck.project_text(str(a.get("name", ""))),
            "create_work_folder": self._create_work_folder,
            "propose_skill": self._on_propose_skill,
            "show_image": self._show_image,
            "ask_user": self._ask_user_tool,
            "list_tasks": lambda a: self._tasks_text(),
            "propose_work": self._propose_work,
            "propose_task_change": self._propose_task_change,
        }

    # -- reshaping work: Claude proposes, the user decides (the tool waits) -------------

    def _queues(self) -> list[dict[str, Any]]:
        pid = self.row["project_id"]
        room = self.deck.room
        out = [{"group_id": None, "name": "Main queue", "priority": "", "tags": [],
                "tasks": [{"id": t["id"], "title": t["title"]} for t in room.queue(pid, None)]}]
        for g in self.deck.store.task_groups(pid):
            out.append({"group_id": g["id"], "name": g["name"], "priority": g["priority"], "tags": g["tags"],
                        "tasks": [{"id": t["id"], "title": t["title"]} for t in room.queue(pid, g["id"])]})
        return out

    def _tasks_text(self) -> str:
        pid = self.row["project_id"]
        if pid is None:
            return "This is a standalone task: it has no project queues."
        store = self.deck.store
        lines = [f"This session's task: #{self.row.get('task_id')} {self.row['task']}"]
        for q in self._queues():
            head = f"\n## {q['name']}" + (f" (priority {q['priority']})" if q["priority"] else "") + \
                (f" tags: {', '.join(q['tags'])}" if q["tags"] else "")
            lines.append(head)
            for i, t in enumerate(store.task(x["id"]) for x in q["tasks"]):
                d = (t["details"] or "").strip().splitlines()
                lines.append(f"{i + 1}. #{t['id']} {t['title']} [{t['status']}]" + (f" — {d[0][:140]}" if d else ""))
            if not q["tasks"]:
                lines.append("(empty)")
        routines = store.routines(pid)
        if routines:
            lines.append("\n## Routines")
            groups = {g["id"]: g["name"] for g in store.task_groups(pid)}
            for r in routines:
                lines.append(f"- {r['name']}: {r['trigger']}, gate {r['gate']}"
                             + (f", group {groups.get(r['group_id'], '?')}" if r.get("group_id") else "")
                             + ("" if r["enabled"] else " (paused)"))
        return "\n".join(lines)

    async def _wait_user(self, event: dict[str, Any]) -> tuple[str, str, Any]:
        """Show a card and wait for the user's decision (like a permission prompt)."""
        req_id = f"w{next(_req_ids)}"
        future: asyncio.Future[tuple[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[req_id] = {"future": future, "tool": event["type"], "summary": event.get("title", ""),
                                "reason": ""}
        self.emit({**event, "req_id": req_id})
        self.set_status("needs_you")
        try:
            decision, data = await future
            return req_id, decision, data
        except asyncio.CancelledError:
            self.emit({"type": "proposal_done", "req_id": req_id, "text": "Withdrawn by Claude Code."})
            raise
        finally:
            self.pending.pop(req_id, None)
            if self.row["status"] == "needs_you" and not self.pending:
                self.set_status("working")

    async def _propose_work(self, args: dict[str, Any]) -> str:
        if self.row["project_id"] is None:
            return "This is a standalone task: there are no queues. Tell the user about the extra work instead."
        task = self.deck.store.task(self.row["task_id"]) if self.row.get("task_id") else None
        req_id, decision, choice = await self._wait_user({
            "type": "work_proposal", "title": str(args.get("title", "")).strip()[:200],
            "details": str(args.get("details", "")).strip()[:6000], "why": str(args.get("why", "")).strip()[:500],
            "suggest": {k: args.get(k) for k in ("suggest", "group", "new_group", "new_group_priority", "position",
                                                 "reasoning")},
            "current_task": {"id": task["id"], "title": task["title"]} if task else None,
            "queues": self._queues()})
        try:
            return self._apply_work(req_id, decision, choice if isinstance(choice, dict) else {}, task)
        except Exception as exc:
            self.emit({"type": "proposal_done", "req_id": req_id, "text": f"Couldn't add it: {exc}"})
            raise

    def _apply_work(self, req_id: str, decision: str, c: dict[str, Any], task: dict[str, Any] | None) -> str:
        room, sid, pid = self.deck.room, self.sid, self.row["project_id"]
        title, details = str(c.get("title", "")).strip(), str(c.get("details", "")).strip()
        if decision != "work" or not title:
            self.emit({"type": "proposal_done", "req_id": req_id, "text": "Not added."})
            return "The user didn't add this work anywhere. Leave it out; mention it in your summary if relevant."
        note = str(c.get("note", "")).strip() or "Came up during this task"
        where = c.get("where")
        if where == "current":
            if task is None:
                raise ValueError("This session has no task.")
            room.add_to_task(task["id"], title, details, by="claude", note=note, session_id=sid)
            text = f"Added to the current task (#{task['id']}). Do this work as part of it."
        else:
            gid = c.get("group_id")
            new_group = {"name": c.get("new_group", ""), "priority": c.get("new_group_priority", "normal")} \
                if where == "new_group" else None
            t = room.place_task(pid, title, details, group_id=int(gid) if gid not in (None, "") else None,
                                new_group=new_group, position=str(c.get("position") or "end"), by="claude",
                                note=note, session_id=sid)
            q = room._queue_name(pid, t.get("group_id"))
            text = f"Added as task #{t['id']} “{t['title']}” in the {q}. Don't do it in this session."
        self.emit({"type": "proposal_done", "req_id": req_id, "text": text})
        return text

    async def _propose_task_change(self, args: dict[str, Any]) -> str:
        if self.row["project_id"] is None or not self.row.get("task_id"):
            return "This session has no project task to change."
        store = self.deck.store
        task = store.task(self.row["task_id"])
        kind = str(args.get("kind", ""))
        other = store.task(int(args["task_id"])) if args.get("task_id") not in (None, "") else None
        if kind in ("pull_in", "pull_part") and (other is None or other["project_id"] != task["project_id"]):
            raise ValueError("Give the id of another task in this project (see list_tasks).")
        req_id, decision, c = await self._wait_user({
            "type": "change_proposal", "kind": kind, "why": str(args.get("why", "")).strip()[:500],
            "current_task": {"id": task["id"], "title": task["title"], "details": task["details"]},
            "other_task": {"id": other["id"], "title": other["title"], "details": other["details"],
                           "status": other["status"]} if other else None,
            "part": str(args.get("part", "")), "remaining": str(args.get("remaining", "")),
            "keep": str(args.get("keep", "")), "new_title": str(args.get("new_title", "")),
            "new_details": str(args.get("new_details", "")), "group": str(args.get("group", "")),
            "queues": self._queues()})
        if decision != "change" or not isinstance(c, dict):
            self.emit({"type": "proposal_done", "req_id": req_id, "text": "Tasks left as they were."})
            return "The user kept the tasks as they are. Stay within this task's scope."
        try:
            text = self._apply_change(kind, task, other, c, str(args.get("why", "")))
        except Exception as exc:
            self.emit({"type": "proposal_done", "req_id": req_id, "text": f"Couldn't change the tasks: {exc}"})
            raise
        self.emit({"type": "proposal_done", "req_id": req_id, "text": text})
        return text

    def _apply_change(self, kind: str, task: dict[str, Any], other: dict[str, Any] | None, c: dict[str, Any],
                      why: str) -> str:
        room, sid = self.deck.room, self.sid
        note = str(c.get("note", "")).strip() or why.strip()
        if kind == "pull_in":
            room.merge_tasks(task["id"], other["id"], by="claude", note=note, session_id=sid)
            text = f"Task “{other['title']}” is now part of this task; do its work here."
        elif kind == "pull_part":
            room.merge_tasks(task["id"], other["id"], part=str(c.get("part", "")), remaining=str(c.get("remaining", "")),
                             by="claude", note=note, session_id=sid)
            text = f"That part of “{other['title']}” is now part of this task; the rest stays there."
        elif kind == "split":
            gid = c.get("group_id")
            t = room.split_task(task["id"], str(c.get("keep", "")), str(c.get("new_title", "")),
                                str(c.get("new_details", "")), group_id=int(gid) if gid not in (None, "") else None,
                                position=str(c.get("position") or "next"), by="claude", note=note, session_id=sid)
            text = f"Split: “{t['title']}” is now task #{t['id']}; this task keeps the rest. Don't do the split-off work here."
        else:
            raise ValueError(f"Unknown change: {kind}")
        return text

    async def _call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Run one of Carrier's tools as Claude Code would (errors come back as text)."""
        result = await sdk.carrier_tool(name, self.tool_handlers()[name]).handler(args)
        return result["content"][0]["text"]

    def _create_work_folder(self, args: dict[str, Any]) -> str:
        path = Path(str(args.get("path", ""))).expanduser()
        if not path.is_absolute():
            raise ValueError("Give the new folder's absolute path.")
        claim = self.deck.workspace_target(self.row, str(path / "x"))
        if claim is None or claim[1] is None:
            raise ValueError("That isn't a new folder inside one of this project's workspace folders.")
        f = self.deck.claim_folder(self.row, claim[1])  # the first new folder on the way
        path.mkdir(parents=True, exist_ok=True)
        self.emit({"type": "notice", "kind": "folder",
                   "text": f"Added the new folder {f['label']} ({f['path']}) to this project as a work folder."})
        return f"Created {f['path']}; it's now a work folder of this project (files there are named {f['label']}/…)."

    def _on_propose_skill(self, args: dict[str, Any]) -> str:
        from carrier.core import skills

        name = skills.clean_name(str(args.get("name", "")))
        self.emit({"type": "skill_proposal", "id": f"k{next(_req_ids)}", "name": name,
                   "description": str(args.get("description", "")).strip()[:1000],
                   "body": str(args.get("body", "")).strip()[:skills.MAX_BODY],
                   "why": str(args.get("why", "")).strip()[:500]})
        return "Shown to the user as a skill card to review, edit and approve. Carry on."

    def _images_off(self) -> bool:
        return not self.deck.room.images_allowed(self.row["project_id"])

    async def _before_read(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        """With images off, Claude may not open image files (that would send them to Anthropic)."""
        path = str((data.get("tool_input") or {}).get("file_path") or "")
        if self._images_off() and Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return sdk.pre_tool_output(decision="deny", reason="[Carrier Flight Deck] Images are turned off for "
                                       "this project, so image files can't be opened. Work from text, or ask the user.")
        return {}

    def _show_image(self, args: dict[str, Any]) -> str:
        from carrier.core import images

        if self._images_off():
            return "Images are turned off for this project; describe it in text instead."

        img = images.load(str(args.get("path", "")))
        self.emit({"type": "image", "from": "claude", "mime": img["mime"], "data": img["data"],
                   "name": img["name"], "caption": str(args.get("caption", "")).strip()[:500]})
        return f"Shown {img['name']} to the user."

    def _on_propose_rule(self, args: dict[str, Any]) -> None:
        """Claude proposed a rule: show it as an editable card (nothing is saved yet)."""
        scope = str(args.get("scope") or "project")
        self.emit({"type": "rule_proposal", "id": f"r{next(_req_ids)}",
                   "text": str(args.get("text", "")).strip()[:4000],
                   "scope": scope if scope in ("project", "global", "rule_set") else "project",
                   "rule_set": str(args.get("rule_set", "")).strip()[:200],
                   "why": str(args.get("why", "")).strip()[:500]})

    async def _ask_user(self, tool_input: dict[str, Any]) -> Any:
        """Claude Code's AskUserQuestion: show the questions as option cards and return the answers."""
        self._routed_asks += 1  # this question reached Carrier (see _handle's fallback)
        decision, answers = await self._collect_answers(tool_input.get("questions"))
        if decision != "answers":
            return sdk.deny("The user skipped these questions; carry on with your best judgement or ask in text.")
        return sdk.allow({**tool_input, "answers": answers})

    async def _ask_user_tool(self, args: dict[str, Any]) -> str:
        """Carrier's own ask_user tool: the same question cards, independent of Claude Code's routing."""
        decision, answers = await self._collect_answers(args.get("questions"))
        if decision != "answers":
            return "The user skipped these questions; carry on with your best judgement."
        return "The user answered:\n" + "\n".join(f"- {q} → {a}" for q, a in answers.items())

    async def _collect_answers(self, raw: Any) -> tuple[str, dict[str, str]]:
        from carrier.readyroom.importer import clean_questions

        questions = clean_questions(raw)
        req_id = f"q{next(_req_ids)}"
        future: asyncio.Future[tuple[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[req_id] = {"future": future, "tool": "AskUserQuestion", "summary": "Questions for you",
                                "reason": ""}
        self.emit({"type": "question", "req_id": req_id, "questions": questions})
        self.set_status("needs_you")
        try:
            decision, answers = await future
        except asyncio.CancelledError:
            self.emit({"type": "question_done", "req_id": req_id, "decision": "cancelled"})
            raise
        finally:
            self.pending.pop(req_id, None)
            if self.row["status"] == "needs_you" and not self.pending:
                self.set_status("working")
        if decision != "answers" or not isinstance(answers, dict):
            self.emit({"type": "question_done", "req_id": req_id, "decision": "skipped"})
            return "skip", {}
        answers = {str(k): str(v) for k, v in answers.items()}
        self.emit({"type": "question_done", "req_id": req_id, "decision": "answered", "answers": answers})
        return "answers", answers

    async def _can_use_tool(self, tool: str, tool_input: dict[str, Any], context: Any) -> Any:
        if tool == "AskUserQuestion":
            return await self._ask_user(tool_input)
        key, allow_label = sdk.allow_key(tool, tool_input)
        not_ours = False
        if tool in sdk.EDIT_TOOLS:
            path = self._file_path({"tool_input": tool_input})
            not_ours = bool(path) and self.deck.file_owner(self.row, path)["kind"] in ("foreign", "other")
        if not_ours:
            # Another project's file (or outside this project's part of a shared space): ask every
            # time — no "for this session" approval and no pre-approval covers it.
            key, allow_label = "", ""
        elif key in self.always_allow or (tool == "Bash" and "Bash=" + str(tool_input.get("command", "")) in self.always_allow):
            return sdk.allow(tool_input)
        elif self.deck.preapproved(self.row, tool, tool_input):  # the project's pre-approved actions
            return sdk.allow(tool_input)
        session_add: list[str] = []
        if tool == "Bash":  # "for this session" means programs, as in the project's list
            from carrier.flightdeck import shellcheck

            sc = self.deck.shell_scope(self.row)
            v = shellcheck.check(str(tool_input.get("command", "")), sc["entries"] + self.session_programs,
                                 self.cwd, sc["readable"], sc["editable"], sc["can_edit"], sc["can_read"],
                                 no_write=sc.get("no_write"))
            if v.ok:
                return sdk.allow(tool_input)
            if v.missing and sdk._only_programs_missing(v):
                session_add = v.missing
                allow_label = "Allow " + ", ".join(f"`{m}`" for m in session_add) + " for this session"
            else:
                key, allow_label = "Bash=" + str(tool_input.get("command", "")), "Allow this exact command for this session"
        proj = self.deck.project_allow(self.row, tool, tool_input)
        req_id = f"p{next(_req_ids)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str]] = loop.create_future()
        summary = sdk.summarize_tool(tool, tool_input, self.cwd)
        reason = getattr(context, "decision_reason", None) or ""
        self.pending[req_id] = {"future": future, "tool": tool, "summary": summary, "reason": reason}
        self.emit({"type": "permission", "req_id": req_id, "tool": tool, "summary": summary,
                   "reason": reason, "input": sdk._trim_input(tool_input), "allow_label": allow_label,
                   "no_always": not_ours,
                   "project_label": (proj or {}).get("label", "") if (proj or {}).get("change") else "",
                   "programs": (proj or {}).get("programs") or [], "why": (proj or {}).get("reason", "")})
        self.set_status("needs_you")
        try:
            decision, message = await future
        except asyncio.CancelledError:
            # Claude Code withdrew the request (e.g. interrupted): close the card.
            self.emit({"type": "permission_done", "req_id": req_id, "decision": "cancelled"})
            raise
        finally:
            self.pending.pop(req_id, None)
            if self.row["status"] == "needs_you" and not self.pending:
                self.set_status("working")
        self.emit({"type": "permission_done", "req_id": req_id, "decision": decision})
        if decision in ("allow", "allow_always", "allow_project"):
            if decision == "allow_always" and not not_ours:
                if session_add:
                    self.session_programs += [m for m in session_add if m not in self.session_programs]
                else:
                    self.always_allow.add(key)
            if decision == "allow_project" and proj and proj.get("change"):
                try:
                    self.deck.room.add_allowance(self.row["project_id"], proj["change"])
                    self.emit({"type": "notice", "kind": "allow",
                               "text": f"Pre-approved for this project: "
                                       f"{proj['label'].removeprefix('Always allow ').removesuffix(' in this project')}. "
                                       "Change it in the Ready Room."})
                except Exception as exc:  # still allow this one
                    self.emit({"type": "notice", "kind": "allow", "text": f"Couldn't save the pre-approval: {exc}"})
            return sdk.allow(tool_input)
        if decision == "interrupt":
            return sdk.deny(message, interrupt=True)
        return sdk.deny(message or "The user declined this action.")

    def pending_list(self) -> list[dict[str, Any]]:
        """Snapshot of waiting requests (call on the deck loop)."""
        return [{"req_id": k, "tool": v["tool"], "summary": v["summary"], "reason": v["reason"]}
                for k, v in self.pending.items()]

    def answer(self, req_id: str, decision: str, message: Any = "") -> bool:
        """Resolve a waiting request: a permission (``message`` = why) or questions
        (``decision`` "answers" with ``message`` = {question: answer}, or "skip")."""
        req = self.pending.get(req_id)
        if req is None or req["future"].done():
            return False
        req["future"].set_result((decision, message))
        return True

    # -- coordination hooks ---------------------------------------------------

    async def _on_prompt(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        try:  # reference folders this project watches: what changed since the last message
            await asyncio.to_thread(self.deck.refs_check, self.sid)
        except Exception:
            traceback.print_exc()
        text = coordination.briefing(self.deck.store, self.row)
        if text:
            items = [ln[2:] if ln.startswith("- ") else ln for ln in text.split("\n")[1:]]
            self.emit({"type": "notice", "kind": "briefing",
                       "text": "Shared with Claude:\n" + "\n".join(f"• {i}" for i in items)})
        return sdk.prompt_output(text)

    def _file_path(self, data: dict[str, Any]) -> str | None:
        inp = data.get("tool_input") or {}
        path = inp.get("file_path") or inp.get("notebook_path")
        if path and not os.path.isabs(str(path)):  # Claude Code resolves it in the session's folder
            path = os.path.join(str(self.cwd), str(path))
        return path

    async def _before_edit(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        path = self._file_path(data)
        if not path:
            return {}
        claim = self.deck.workspace_target(self.row, path)
        if claim is not None:
            ws, new_folder = claim
            if new_folder is None:
                self.emit({"type": "notice", "kind": "reference",
                           "text": f"Blocked an edit in the workspace folder {ws['label']} (its existing folders "
                                   "aren't this project's)."})
                return sdk.pre_tool_output(
                    decision="deny",
                    reason=f"[Carrier Flight Deck] {ws['label']} ({ws['path']}) is a workspace folder: its existing "
                           "contents belong to other work and are read only for this project. To keep work "
                           "separate from the main folder, create a NEW folder there (writing a file into a new "
                           "folder, or the create_work_folder tool, adds it to this project as a work folder).")
            try:
                f = self.deck.claim_folder(self.row, new_folder)
            except Exception as exc:
                return sdk.pre_tool_output(decision="deny", reason=f"[Carrier Flight Deck] Couldn't add {new_folder} "
                                                                   f"as a work folder: {exc}")
            self.emit({"type": "notice", "kind": "folder",
                       "text": f"Added the new folder {f['label']} ({f['path']}) to this project as a work folder."})
            return sdk.pre_tool_output(context=f"[Carrier Flight Deck] The new folder {f['path']} is now one of this "
                                               f"project's work folders (files there are named {f['label']}/…).")
        owner = self.deck.file_owner(self.row, path)
        if owner["kind"] == "foreign":
            self.emit({"type": "notice", "kind": "reference",
                       "text": f"Blocked an edit in {owner['owner']}'s main folder (it belongs to that project)."})
            return sdk.pre_tool_output(
                decision="deny",
                reason=f"[Carrier Flight Deck] {owner['owner_path']} is the main folder of the project "
                       f"{owner['owner']}: it belongs to that project alone and is read only here. If the task "
                       "really needs a change there, tell the user.")
        if owner["kind"] == "other":
            f = owner["folder"]
            if (self.deck.store.session(self.sid) or self.row).get("mode") == "bypassPermissions":
                # "Never ask" mode may not stop to ask: block it rather than let it through.
                self.emit({"type": "notice", "kind": "shared",
                           "text": f"Blocked an edit of {f['label']}/{owner['rel']}: it's outside this project's "
                                   f"part of the shared space {f['label']}, and this session never asks."})
                return sdk.pre_tool_output(
                    decision="deny",
                    reason=f"[Carrier Flight Deck] {f['label']}/{owner['rel']} is outside this project's part of the "
                           f"shared folder {f['label']}; editing it needs the user's OK, and this session is in "
                           "never-ask mode. Tell the user what you need to change there.")
            self.emit({"type": "notice", "kind": "shared",
                       "text": f"{f['label']}/{owner['rel']} is outside this project's part of the shared space "
                               f"{f['label']}: asking you first."})
            return sdk.pre_tool_output(
                decision="ask",
                reason=f"Outside this project's part of the shared folder {f['label']} ({f['label']}/{owner['rel']}).")
        if owner["kind"] == "new" and tool_use_id:
            self._claims[tool_use_id] = owner  # claimed in _after_edit, for this very call only
        ref = self.deck.reference_folder(self.row, path)
        if ref is not None:
            self.emit({"type": "notice", "kind": "reference",
                       "text": f"Blocked an edit in the reference folder {ref['label']} (read only)."})
            return sdk.pre_tool_output(
                decision="deny",
                reason=f"[Carrier Flight Deck] {ref['label']} ({ref['path']}) is a reference folder for this "
                       "project: read it, but don't change it. If the task really needs a change there, "
                       "tell the user.")
        notices: list[Any] = []
        seen: set[str] = set()
        for pid, rel in self._owners(path):
            for n in coordination.check_edit(self.deck.store, self.row, rel, project_id=pid):
                if n.text not in seen:
                    seen.add(n.text)
                    notices.append(n)
        if not notices:
            return {}
        text = "\n".join(f"- {n.text}" for n in notices)
        for n in notices:
            self.emit({"type": "notice", "kind": n.kind, "text": n.text})
        policy = self.row.get("policy") or self.deck.policy  # a session preset may override
        header = "[Carrier Flight Deck] Before you edit this file:\n"
        if not any(n.conflict for n in notices) and policy in ("block", "ask"):
            policy = "warn"  # nothing is actually in the way: inform Claude, don't stop it
        if policy == "block":
            return sdk.pre_tool_output(decision="deny", reason=header + text +
                                       "\nThe edit was blocked by Flight Deck. Explain the conflict to the user.")
        if policy == "ask":
            return sdk.pre_tool_output(decision="ask", reason="Possible conflict:\n" + text)
        if policy == "inform":
            return {}
        return sdk.pre_tool_output(context=header + text)  # "warn" (default)

    async def _after_edit(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        from carrier.readyroom import routines

        path = self._file_path(data)
        owners = self._owners(path) if path else []
        for pid, _rel in owners:
            routines.invalidate(pid)  # the review gate may have changed
        if not owners:
            return {}
        notified: list[int] = []
        for pid, rel_here in owners:  # every project working in this folder logs the edit
            for n in coordination.record_edit(self.deck.store, self.row, rel_here, data.get("tool_name", "Edit"),
                                              project_id=pid):
                if n not in notified:
                    notified.append(n)
        rel = owners[0][1]
        self.deck.store.update_session(self.sid, last_activity=time.time())
        claim = self._claims.pop(tool_use_id, None) if tool_use_id else None
        if claim and path and os.path.exists(path) and self.row["project_id"] is not None:  # created in a shared space
            from carrier.readyroom import ownership

            ownership.claim(self.deck.store, self.row["project_id"], claim["folder"]["path"], claim["rel"])
        try:
            self.deck.notify_ref_watchers(self.row, path)
        except Exception:
            traceback.print_exc()
        if notified:
            names = ", ".join(f"#{n}" for n in notified)
            self.emit({"type": "notice", "kind": "contract_sent",
                       "text": f"{rel} is a shared contract — told session(s) {names}."})
            for other in notified:
                self.deck.emit(other, {"type": "notice", "kind": "contract_changed",
                                       "text": f"Session #{self.sid} changed shared file {rel}. Claude will be "
                                               "told on your next message."})
        return {}


def default_client_factory(options: Any) -> Any:
    return sdk.make_client(options)

