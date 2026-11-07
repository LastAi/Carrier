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
        self.always_allow: set[str] = set()  # sdk.allow_key() values allowed for the rest of the session
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
        options = sdk.build_options(cwd=self.cwd, model=row["model"], mode=row["mode"], resume=resume,
                                    effort=row.get("effort"),
                                    hooks=hooks, can_use_tool=self._can_use_tool,
                                    context=self.deck.context_for(row),
                                    add_dirs=self.deck.extra_dirs(row))
        self.cost_base = float(row["cost_usd"] or 0.0)
        self.client = self.client_factory(options)
        await self.client.connect()
        self.set_status("idle")

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
                   on_fail: Callable[[], None] | None = None) -> None:
        """Send one user message and stream the reply (call :meth:`claim` first).

        ``on_done`` receives Claude's reply text when the turn finishes
        successfully (used to collect a task summary); ``on_fail`` is called
        instead if the turn was interrupted, failed, or ended without a result.
        """
        self.busy = True
        self.interrupted = False
        self.emit({"type": "user", "text": text})
        reply: list[str] = []
        ok = False
        try:
            await self.client.query(text)
            async for msg in self.client.receive_response():
                for ev in sdk.normalize(msg, self.cwd):
                    if ev["type"] == "text":
                        reply.append(ev["text"])
                    if ev["type"] == "result":
                        ok = not ev.get("is_error")
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
        if self.row["status"] not in ("ended", "error"):
            self.set_status("idle")
        if ok and not self.interrupted and reply:
            if on_done is not None:
                on_done("\n\n".join(reply).strip())
        elif on_fail is not None:
            on_fail()

    def _handle(self, ev: dict[str, Any]) -> None:
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

    async def _can_use_tool(self, tool: str, tool_input: dict[str, Any], context: Any) -> Any:
        key, allow_label = sdk.allow_key(tool, tool_input)
        if key in self.always_allow:
            return sdk.allow(tool_input)
        req_id = f"p{next(_req_ids)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str]] = loop.create_future()
        summary = sdk.summarize_tool(tool, tool_input, self.cwd)
        reason = getattr(context, "decision_reason", None) or ""
        self.pending[req_id] = {"future": future, "tool": tool, "summary": summary, "reason": reason}
        self.emit({"type": "permission", "req_id": req_id, "tool": tool, "summary": summary,
                   "reason": reason, "input": sdk._trim_input(tool_input), "allow_label": allow_label})
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
        if decision in ("allow", "allow_always"):
            if decision == "allow_always":
                self.always_allow.add(key)
            return sdk.allow(tool_input)
        if decision == "interrupt":
            return sdk.deny(message, interrupt=True)
        return sdk.deny(message or "The user declined this action.")

    def pending_list(self) -> list[dict[str, Any]]:
        """Snapshot of waiting requests (call on the deck loop)."""
        return [{"req_id": k, "tool": v["tool"], "summary": v["summary"], "reason": v["reason"]}
                for k, v in self.pending.items()]

    def answer(self, req_id: str, decision: str, message: str = "") -> bool:
        req = self.pending.get(req_id)
        if req is None or req["future"].done():
            return False
        req["future"].set_result((decision, message))
        return True

    # -- coordination hooks ---------------------------------------------------

    async def _on_prompt(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        text = coordination.briefing(self.deck.store, self.row)
        if text:
            items = [ln[2:] if ln.startswith("- ") else ln for ln in text.split("\n")[1:]]
            self.emit({"type": "notice", "kind": "briefing",
                       "text": "Shared with Claude:\n" + "\n".join(f"• {i}" for i in items)})
        return sdk.prompt_output(text)

    def _file_path(self, data: dict[str, Any]) -> str | None:
        inp = data.get("tool_input") or {}
        return inp.get("file_path") or inp.get("notebook_path")

    async def _before_edit(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        path = self._file_path(data)
        if not path:
            return {}
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
        path = self._file_path(data)
        owners = self._owners(path) if path else []
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

