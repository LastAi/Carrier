# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""DesktopApi — the methods the desktop page can call.

Debrief's methods (reviews, unprefixed), Flight Deck's (``fd_*``) and
Ready Room's (``rr_*``), plus shared setup/settings. Each delegates to its
tool package, so the page keeps one bridge.

pywebview exposes every public method of this object to JavaScript as
``window.pywebview.api.<name>(...)``, returning a Promise. Each call runs
on its own worker thread, so slow work (preparing, asking the guide) never
freezes the window.

Conventions
-----------
* Every method returns ``{"ok": True, "data": ...}`` or
  ``{"ok": False, "error": "message for the user"}``. The page shows errors
  in a banner instead of crashing.
* Arguments and results are plain JSON values (see ``session.py``).
* Python → page notifications (progress messages) go through ``emit``,
  which ``main.py`` connects to ``window.evaluate_js``.

Nothing here imports pywebview, so tests can drive this class directly or
through a real browser (``tests/test_desktop.py``).
"""

from __future__ import annotations

import json
import os
import threading
import time
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from carrier.core import gitutil
from carrier.debrief import session as S
from carrier.flightdeck.manager import DeckError
from carrier.readyroom.manager import RoomError
from carrier.core.llm import Backend

Emit = Callable[[str, Any], None]


def _ok(data: Any = None) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg}


def _guard(fn: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    """Wrap an API method so any exception becomes an error result."""

    def wrapper(self: "DesktopApi", *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return _ok(fn(self, *args, **kwargs))
        except (S.SessionError, DeckError, RoomError, ValueError) as exc:  # user-facing messages
            return _err(str(exc))
        except Exception as exc:  # unexpected: keep the app alive, log details
            traceback.print_exc()
            return _err(f"Unexpected error: {exc}")

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


class DesktopApi:
    def __init__(self, backend_factory: Callable[[], Backend | None] | None = None,
                 pick_folder: Callable[[], str | None] | None = None,
                 deck_factory: Callable[[], Any] | None = None) -> None:
        # Attributes starting with "_" are NOT exposed to JavaScript.
        self._session: S.ReviewSession | None = None
        self._deck: Any = None  # Flight Deck, created on first use
        self._deck_factory = deck_factory
        self._deck_lock = threading.Lock()  # JS calls arrive on parallel threads
        self._emit: Emit = lambda _kind, _data: None
        self._backend_factory = backend_factory
        self._pick_folder = pick_folder
        self._pick_file: Callable[[str, str], str | None] | None = None

    def _bind(self, emit: Emit, pick_folder: Callable[[], str | None],
              pick_file: Callable[[str, str], str | None] | None = None) -> None:
        """Called by main.py once the window exists."""
        self._emit = emit
        self._pick_folder = pick_folder
        self._pick_file = pick_file

    def _need(self) -> S.ReviewSession:
        if self._session is None:
            raise S.SessionError("Open a project first.")
        return self._session

    # -- setup ---------------------------------------------------------------

    @_guard
    def attention(self) -> bool:
        """Ask the OS to draw the user's eye to the window (taskbar flash on Windows)."""
        fn = getattr(self, "_attention", None)
        if fn is not None:
            fn()
        return fn is not None

    @_guard
    def upgrade_notice(self) -> str | None:
        """Where the pre-upgrade backup went, if this start upgraded the database."""
        backup = getattr(self._store(), "backup", None)
        return str(backup) if backup else None

    @_guard
    def checks(self) -> dict[str, Any]:
        return S.setup_checks()

    @_guard
    def test_claude(self) -> dict[str, Any]:
        backend = self._backend_factory() if self._backend_factory else None
        return S.test_claude(backend)

    @_guard
    def settings(self) -> dict[str, Any]:
        return S.get_settings()

    @_guard
    def save_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        return S.set_settings(changes)

    # -- choosing what to review ------------------------------------------------
    # Debrief reviews Ready Room projects: the main folder plus any extra
    # folders, together. (The terminal version still reviews any folder.)

    def _store(self) -> Any:
        return self._fd().store

    def _project_folders(self, pid: int) -> list[dict[str, Any]]:
        p = self._store().project(pid)
        if p is None:
            raise S.SessionError("Unknown project.")
        out = []
        for label, path in self._store().project_roots(p):
            folder = Path(path)
            info: dict[str, Any] = {"label": label, "path": path, "exists": folder.is_dir()}
            info["is_repo"] = info["exists"] and gitutil.in_repo(folder)
            out.append(info)
        return out

    @_guard
    def db_projects(self) -> list[dict[str, Any]]:
        """Projects for the Debrief start screen, with each one's open review."""
        out = []
        for p in self._store().projects():
            folders = self._project_folders(p["id"])
            main = folders[0]
            info = S.inspect_folder(main["path"], str(p["id"])) if main["is_repo"] else {"is_repo": False}
            out.append({"id": p["id"], "name": p["name"], "path": p["path"], "folders": folders,
                        "open_review": info.get("open_review") if main["is_repo"] else None,
                        "ready": all(f["is_repo"] for f in folders)})
        return out

    @_guard
    def db_project(self, pid: int) -> dict[str, Any]:
        """One project: its folders' status and the main folder's history (for "Review changes")."""
        p = self._store().project(pid)
        folders = self._project_folders(pid)
        main = folders[0]
        info = (S.inspect_folder(main["path"], str(pid)) if main["is_repo"]
                else {"is_repo": False, "path": main["path"]})
        return {**info, "id": pid, "name": p["name"], "folders": folders,
                "ready": all(f["is_repo"] for f in folders)}

    def _worktree_path(self, sid: int) -> str:
        """The folder of a Flight Deck session that works in its own copy.

        The page passes a session id, never a path, so it can't point Debrief
        at an arbitrary folder.
        """
        s = self._store().session(sid)
        if s is None or not s["worktree"]:
            raise S.SessionError("Only a session working in its own copy is reviewed on its own.")
        return s["cwd"]

    @_guard
    def db_worktree(self, sid: int) -> dict[str, Any]:
        """Start-screen details for a Flight Deck session's own copy."""
        return S.inspect_folder(self._worktree_path(sid))

    @_guard
    def db_track(self, pid: int, path: str) -> dict[str, Any]:
        """Start tracking one of the project's folders with git."""
        if path not in [f["path"] for f in self._project_folders(pid)]:
            raise S.SessionError("That folder isn't part of this project.")
        S.start_tracking(path)
        return self.db_project.__wrapped__(self, pid)

    def _backend(self, use_ai: bool) -> Backend | None | str:
        if not use_ai:
            return None
        return self._backend_factory() if self._backend_factory else "auto"

    @_guard
    def start_project(self, pid: int, use_ai: bool = True, from_ref: str | None = None,
                      fresh: bool = False) -> dict[str, Any]:
        """Start or resume a review of a project (all its folders)."""
        from carrier.debrief.projects import project_spec

        spec = project_spec(self._store(), pid)
        if self._session is not None:
            self._session.close()
            self._session = None
        self._session = S.ReviewSession(spec.path, backend=self._backend(use_ai), project=spec)
        return self._session.start(from_ref=from_ref or None, fresh=fresh,
                                   progress=lambda msg: self._emit("progress", msg))

    @_guard
    def db_scan_agents(self, pid: int) -> list[dict[str, Any]]:
        """The project's review agents for a whole-project scan, with the user's last choice ticked."""
        from carrier.debrief.projects import project_spec

        spec = project_spec(self._store(), pid)
        picked = set(self._store().get_setting(f"project:{pid}:scan_agents", ["security"]))
        return [{"name": c.name, "title": c.title, "severity": c.severity, "picked": c.name in picked}
                for c in spec.criteria or []]

    @_guard
    def start_scan(self, pid: int, agents: list[str], use_ai: bool = True) -> dict[str, Any]:
        """Review the whole project as it is now (no history) with the chosen agents."""
        from carrier.debrief.projects import project_spec

        if not use_ai:
            raise ValueError("A scan needs the AI agents.")
        self._store().set_setting(f"project:{pid}:scan_agents", list(agents))
        spec = project_spec(self._store(), pid)
        if self._session is not None:
            self._session.close()
            self._session = None
        self._session = S.ReviewSession(spec.path, backend=self._backend(True), project=spec)
        return self._session.start(scan=True, agents=list(agents),
                                   progress=lambda msg: self._emit("progress", msg))

    @_guard
    def start_worktree(self, sid: int, use_ai: bool = True, from_ref: str | None = None,
                       fresh: bool = False) -> dict[str, Any]:
        """Review a Flight Deck session's own copy (worktree) on its own."""
        path = self._worktree_path(sid)
        if self._session is not None:
            self._session.close()
            self._session = None
        self._session = S.ReviewSession(path, backend=self._backend(use_ai))
        return self._session.start(from_ref=from_ref or None, fresh=fresh,
                                   progress=lambda msg: self._emit("progress", msg))

    @_guard
    def cancel_start(self) -> bool:
        """Cancel a running start() — the Cancel button while preparing."""
        session = self._session
        return session.cancel_start() if session is not None else False

    # -- reviewing -----------------------------------------------------------

    @_guard
    def overview(self) -> dict[str, Any]:
        return self._need().overview()

    @_guard
    def item(self, item_id: str, split: bool = False) -> dict[str, Any]:
        return self._need().item(item_id, split)

    @_guard
    def explain(self, item_id: str) -> dict[str, Any] | None:
        return self._need().explain(item_id)

    @_guard
    def selection(self, item_id: str, split: bool, first: int, last: int,
                  side: str | None = None) -> dict[str, Any] | None:
        return self._need().selection(item_id, split, first, last, side)

    @_guard
    def ask(self, item_id: str, question: str, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._need().ask(item_id, question, selection)

    @_guard
    def decide(self, item_id: str, action: str, instruction: str = "") -> dict[str, Any]:
        return self._need().decide(item_id, action, instruction)

    @_guard
    def note(self, item_id: str, text: str) -> dict[str, Any]:
        return self._need().add_note(item_id, text)

    @_guard
    def finish(self) -> dict[str, Any]:
        from carrier.readyroom import routines

        session = self._need()
        out = session.finish()
        routines.invalidate(session.project.id if session.project is not None else None)
        if session.project is not None and session.review is not None:
            r = session.review
            n_fix = sum(1 for i in r.items if i.decision and i.decision.action in ("fix", "revert"))
            self._store().log_project_event(
                session.project.id, "review", ("finished a scan" if r.scan else "finished a review") + f" ({r.id})",
                detail=f"{len(r.items)} change(s); {n_fix} sent back as fixes")
        return out

    @_guard
    def open_file(self, path: str) -> bool:
        """Open the review record or fix list in the OS's default app.

        Only the exact files that finish() just wrote (both .md) may be
        opened, so the page can't be used to launch other programs — not
        even files an agent might have dropped into .carrier/.
        """
        s = self._need()
        target = Path(path).resolve()
        if not s.is_finished_file(path) or target.suffix != ".md":
            raise S.SessionError("Only this review's record and fix list can be opened from here.")
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True

    # -- Flight Deck ------------------------------------------------------------
    # Every fd_* method delegates to carrier.flightdeck.manager.FlightDeck.

    def _fd(self) -> Any:
        with self._deck_lock:  # exactly one deck, even if two calls arrive at once
            if self._deck is None:
                if self._deck_factory is not None:
                    self._deck = self._deck_factory()
                else:
                    from carrier.core import usersettings
                    from carrier.flightdeck.manager import FlightDeck

                    self._deck = FlightDeck(policy=usersettings.load()["fd_policy"])
            return self._deck

    @_guard
    def fd_overview(self) -> dict[str, Any]:
        return self._fd().overview()

    @_guard
    def pick_folder(self) -> str | None:
        """Native folder picker; returns the path (or None if cancelled)."""
        if self._pick_folder is None:
            raise S.SessionError("Folder picker unavailable.")
        return self._pick_folder()

    @_guard
    def fd_isolation(self, project_id: int) -> dict[str, Any]:
        return self._fd().isolation_choice(project_id)

    @_guard
    def fd_start_task(self, task_id: int, isolation: str = "shared", preset_id: int | None = None,
                      model: str | None = None, mode: str | None = None, send_task: bool = True,
                      effort: str | None = None, confirm_usage: bool = False) -> dict[str, Any]:
        """Start a session. Near or at a usage limit the result is {"usage_hold": why} instead,
        and the page asks the user before calling again with ``confirm_usage``."""
        from carrier.flightdeck.manager import UsageHold

        try:
            return self._fd().start_task(task_id, isolation, preset_id or None, model or None, mode or None,
                                         send_task, effort or None, confirm_usage=bool(confirm_usage))
        except UsageHold as exc:
            return {"usage_hold": str(exc)}

    @_guard
    def fd_new_task(self, project_id: int, title: str, details: str = "") -> dict[str, Any]:
        """Add a task to a project's queue (from the "start another task" dialog)."""
        project = self._fd().room.add_task(project_id, title, details)
        return project["tasks"][-1]

    @_guard
    def fd_standalone(self, title: str, details: str = "", folder: str | None = None) -> dict[str, Any]:
        return self._fd().create_standalone(title, details, folder or None)

    @_guard
    def fd_complete(self, sid: int, summary: str | None = None) -> dict[str, Any]:
        return self._fd().complete_task(sid, summary)

    @_guard
    def fd_upgrade(self, task_id: int, name: str, goal: str = "", tags: str = "") -> dict[str, Any]:
        return self._fd().upgrade_standalone(task_id, name, goal, tags)

    @_guard
    def fd_assign(self, task_id: int, project_id: int, add_merge_task: bool = True) -> dict[str, Any]:
        return self._fd().assign_standalone(task_id, project_id, add_merge_task)

    @_guard
    def fd_finish_standalone(self, task_id: int, summary: str = "") -> dict[str, Any]:
        """Mark a standalone task done without a running session."""
        return self._fd().room.finish_task(task_id, summary)

    @_guard
    def fd_resume(self, sid: int) -> dict[str, Any]:
        return self._fd().resume_session(sid)

    @_guard
    def fd_session(self, sid: int) -> dict[str, Any]:
        return self._fd().session(sid)

    @_guard
    def fd_send(self, sid: int, text: str, images: list[dict[str, Any]] | None = None) -> bool:
        return self._fd().send(sid, text, images)

    @_guard
    def fd_interrupt(self, sid: int) -> bool:
        return self._fd().interrupt(sid)

    @_guard
    def fd_answer(self, sid: int, req_id: str, decision: str, message: Any = "") -> bool:
        """Answer a permission request, or Claude's questions (decision "answers", message {question: answer})."""
        return self._fd().answer(sid, req_id, decision, message)

    @_guard
    def rule_sets(self) -> list[dict[str, Any]]:
        return self._store().rule_sets()

    @_guard
    def fd_apply_rule(self, sid: int, proposal_id: str, text: str, target: str, rule_set_id: int | None = None,
                      new_rule_set: str = "") -> str:
        """Save a rule Claude proposed (as the user edited it) to the project, global rules or a rule set."""
        deck = self._fd()
        s = deck.store.session(sid)
        if s is None:
            raise ValueError("Unknown session.")
        result = self._rr().apply_rule(text, target, s["project_id"], rule_set_id or None, new_rule_set)
        deck.emit(sid, {"type": "rule_done", "id": proposal_id, "decision": "applied", "text": result})
        return result

    @_guard
    def fd_dismiss_rule(self, sid: int, proposal_id: str) -> bool:
        self._fd().emit(sid, {"type": "rule_done", "id": proposal_id, "decision": "dismissed"})
        return True

    @_guard
    def fd_set_mode(self, sid: int, mode: str) -> dict[str, Any]:
        return self._fd().set_mode(sid, mode)

    @_guard
    def fd_set_model(self, sid: int, model: str | None) -> dict[str, Any]:
        return self._fd().set_model(sid, model or None)

    @_guard
    def fd_set_effort(self, sid: int, effort: str | None) -> dict[str, Any]:
        return self._fd().set_effort(sid, effort or None)

    @_guard
    def fd_set_policy(self, policy: str) -> str:
        from carrier.core import usersettings

        result = self._fd().set_policy(policy)
        usersettings.update(fd_policy=result)
        return result

    @_guard
    def fd_end(self, sid: int, handoff: str | None = None) -> dict[str, Any]:
        """End for good; ``handoff``: a note for the next session on the task (None keeps the old one)."""
        return self._fd().end_session(sid, handoff=handoff)

    @_guard
    def fd_end_with_note(self, sid: int) -> dict[str, Any]:
        """Claude writes the handoff note, then the session ends."""
        return self._fd().end_with_note(sid)

    @_guard
    def fd_pause(self, sid: int) -> dict[str, Any]:
        return self._fd().pause_session(sid)

    @_guard
    def fd_remove_worktree(self, sid: int, force: bool = False) -> dict[str, Any]:
        return self._fd().remove_worktree(sid, force)

    @_guard
    def fd_poll(self, since: int) -> dict[str, Any]:
        return self._fd().poll(since)

    @_guard
    def fd_debrief_target(self, sid: int) -> dict[str, Any]:
        return self._fd().debrief_target(sid)

    # -- Ready Room ---------------------------------------------------------------
    # Every rr_* method delegates to carrier.readyroom.manager.ReadyRoom, which
    # shares Flight Deck's store.

    def _rr(self) -> Any:
        return self._fd().room

    @_guard
    def rr_overview(self, search: str = "", tag: str | None = None) -> dict[str, Any]:
        return self._rr().overview(search, tag or None)

    @_guard
    def rr_project(self, pid: int) -> dict[str, Any]:
        return self._rr().project(pid)

    @_guard
    def rr_create_project(self, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = ("name", "path", "goal", "first_task", "first_task_details", "tags", "rules", "rule_set_ids",
                   "preset_id", "template_id")
        return self._rr().create_project(**{k: v for k, v in fields.items() if k in allowed})

    @_guard
    def rr_update_project(self, pid: int, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = ("name", "path", "goal", "tags", "rules", "rule_set_ids", "preset_id")
        return self._rr().update_project(pid, **{k: v for k, v in fields.items() if k in allowed})

    @_guard
    def rr_remove_project(self, pid: int) -> dict[str, Any]:
        return self._rr().remove_project(pid)

    @_guard
    def rr_add_task(self, pid: int, title: str, details: str = "", first: bool = False,
                    group_id: int | None = None) -> dict[str, Any]:
        return self._rr().add_task(pid, title, details, first, group_id=group_id)

    @_guard
    def rr_update_task(self, tid: int, title: str, details: str) -> dict[str, Any]:
        before = dict(self._store().task(tid) or {})
        out = self._rr().update_task(tid, title, details)
        after = self._store().task(tid) or {}
        if before.get("status") == "active":  # a session may be working on it: tell it
            self._fd().notify_task_change(tid, before, after)
        return out

    @_guard
    def rr_delete_task(self, tid: int) -> dict[str, Any]:
        return self._rr().delete_task(tid)

    @_guard
    def rr_move_task(self, tid: int, direction: str) -> dict[str, Any]:
        return self._rr().move_task(tid, direction)

    @_guard
    def rr_reopen_task(self, tid: int) -> dict[str, Any]:
        return self._rr().reopen_task(tid)

    @_guard
    def rr_add_link(self, a: int, b: int, note: str, mine: str, theirs: str) -> dict[str, Any]:
        return self._rr().add_link(a, b, note, mine, theirs)

    @_guard
    def rr_add_folder(self, pid: int, kind: str = "work") -> dict[str, Any] | None:
        """Pick a folder and add it to the project as an extra work or reference folder (None if cancelled)."""
        if self._pick_folder is None:
            raise S.SessionError("Folder picker unavailable.")
        path = self._pick_folder()
        if not path:
            return None
        room = self._rr()
        if kind == "work" and (owner := room._main_of_other(pid, str(Path(path).expanduser().resolve()))):
            # Another project's main folder is that project's alone: offer it as a reference folder.
            return {"needs_reference": owner["name"], "path": path}
        return room.add_folder(pid, path, kind=kind)

    @_guard
    def rr_add_reference(self, pid: int, path: str) -> dict[str, Any]:
        """Add a folder picked earlier as a reference folder (see rr_add_folder)."""
        return self._rr().add_folder(pid, path, kind="reference")

    @_guard
    def rr_set_folder_kind(self, pid: int, path: str, kind: str) -> dict[str, Any]:
        return self._rr().set_folder_kind(pid, path, kind)

    @_guard
    def rr_set_folder_options(self, pid: int, path: str, options: dict[str, Any]) -> dict[str, Any]:
        """A work folder's sharing (own / shared + this project's part), reviews and note."""
        o = dict(options or {})
        return self._rr().set_folder_options(pid, path, share=o.get("share"), part=o.get("part"),
                                             review=o.get("review"), note=o.get("note"))

    @_guard
    def rr_remove_folder(self, pid: int, path: str) -> dict[str, Any]:
        return self._rr().remove_folder(pid, path)

    @_guard
    def rr_remove_link(self, link_id: int, pid: int) -> dict[str, Any]:
        return self._rr().remove_link(link_id, pid)

    @_guard
    def rr_set_global(self, rules: str, rule_set_ids: list[int]) -> dict[str, Any]:
        return self._rr().set_global_rules(rules, rule_set_ids)

    @_guard
    def rr_save_rule_set(self, name: str, body: str, rid: int | None = None) -> list[dict[str, Any]]:
        return self._rr().save_rule_set(name, body, rid)

    @_guard
    def rr_delete_rule_set(self, rid: int) -> list[dict[str, Any]]:
        return self._rr().delete_rule_set(rid)

    @_guard
    def rr_save_preset(self, fields: dict[str, Any], pid: int | None = None) -> list[dict[str, Any]]:
        allowed = ("name", "model", "effort", "mode", "policy", "rule_set_id")
        return self._rr().save_preset(pid=pid, **{k: v for k, v in fields.items() if k in allowed})

    @_guard
    def rr_delete_preset(self, pid: int) -> list[dict[str, Any]]:
        return self._rr().delete_preset(pid)

    @_guard
    def rr_save_template(self, fields: dict[str, Any], tid: int | None = None) -> list[dict[str, Any]]:
        allowed = ("name", "goal", "rules", "rule_set_ids", "tags", "first_task", "preset_id")
        return self._rr().save_template(tid=tid, **{k: v for k, v in fields.items() if k in allowed})

    @_guard
    def rr_delete_template(self, tid: int) -> list[dict[str, Any]]:
        return self._rr().delete_template(tid)

    @_guard
    def rr_template_from_project(self, pid: int, name: str) -> list[dict[str, Any]]:
        return self._rr().template_from_project(pid, name)

    # Review agents (Debrief criteria) are managed in the Ready Room; the
    # logic lives in carrier.debrief.projects. ``pid`` None = global agents.

    @_guard
    def rr_agents(self, pid: int | None = None) -> dict[str, Any]:
        from carrier.debrief import projects as DP

        return DP.agents_view(self._store(), pid)

    @_guard
    def rr_save_agent(self, fields: dict[str, Any], aid: int | None = None,
                      pid: int | None = None) -> dict[str, Any]:
        from carrier.debrief import projects as DP

        if aid is not None:
            a = self._store().review_agent(aid)
            if a is None or a["project_id"] != pid:
                raise S.SessionError("Unknown review agent.")
        DP.save_agent(self._store(), fields, aid, pid)
        return DP.agents_view(self._store(), pid)

    @_guard
    def rr_delete_agent(self, aid: int, pid: int | None = None) -> dict[str, Any]:
        from carrier.debrief import projects as DP

        a = self._store().review_agent(aid)
        if a is None or a["project_id"] != pid:
            raise S.SessionError("Unknown review agent.")
        self._store().delete_review_agent(aid)
        return DP.agents_view(self._store(), pid)

    @_guard
    def rr_builtin(self, name: str, enabled: bool, pid: int | None = None) -> dict[str, Any]:
        """Switch a built-in review agent on/off, globally or for one project."""
        from carrier.debrief import projects as DP

        DP.set_builtin_enabled(self._store(), name, bool(enabled), pid)
        return DP.agents_view(self._store(), pid)

    @_guard
    def models(self) -> dict[str, Any]:
        """Models Claude Code offers and their effort levels (see core.models)."""
        from carrier.core import models

        return {**models.catalog(), "efforts": list(models.EFFORT_ORDER)}

    @_guard
    def rr_builtin_option(self, name: str, key: str, value: str | None, pid: int | None = None) -> dict[str, Any]:
        """Set a built-in agent's severity, model or effort (empty = inherit), globally or per project."""
        from carrier.debrief import projects as DP

        DP.set_builtin_option(self._store(), name, key, value, pid)
        return DP.agents_view(self._store(), pid)

    @_guard
    def rr_draft(self, kind: str, description: str, current: str = "", pid: int | None = None,
                 folder: str = "") -> dict[str, Any]:
        """Draft rules, a review agent, a task, a routine, a skill or a whole new project from the
        user's description (nothing is saved). For a project, ``folder`` gives Claude a glimpse of it."""
        from carrier.core.llm import AgentSDKBackend
        from carrier.readyroom import drafting

        backend = self._backend_factory() if self._backend_factory else None
        if backend is None:
            backend = AgentSDKBackend(concurrency=1)
        project = self._store().project(pid) if pid is not None else None
        context = (project or {}).get("goal", "")
        if project and kind in ("task", "routine"):
            # The project's queue, so a new task fits in and doesn't repeat one.
            open_tasks = self._store().tasks(pid, statuses=("planned", "active"))
            context = (f"Project: {project['name']}\nGoal: {context or '(none)'}\n"
                       + ("Tasks already queued:\n" + "\n".join(f"- {t['title']}" for t in open_tasks)
                          if open_tasks else ""))
        if kind == "project":
            context = drafting.folder_context(folder)
        return drafting.draft(kind, description, backend, current, context)

    # -- import existing work (readyroom/importer.py) ---------------------------------

    def _llm(self) -> Backend:
        from carrier.core.llm import AgentSDKBackend

        backend = self._backend_factory() if self._backend_factory else None
        return backend if backend is not None else AgentSDKBackend(concurrency=1)

    @_guard
    def im_pick_export(self) -> str | None:
        """Pick a claude.ai data export (.zip or conversations.json)."""
        if self._pick_file is None:
            raise S.SessionError("File picker unavailable.")
        return self._pick_file("Claude data export", "*.zip;*.json")

    @_guard
    def im_scan(self, path: str, export: str = "") -> dict[str, Any]:
        """What an import of ``path`` would look at (reads only)."""
        from carrier.readyroom import importer as I

        ws = I.scan(path, export)
        mains = {p["path"]: p["name"] for p in self._store().projects()}
        overview = I.scan_overview(ws)
        overview["projects_inside"] = [name for path_, name in mains.items()
                                       if Path(path_) == Path(ws["root"]) or Path(ws["root"]) in Path(path_).parents]
        return overview

    @_guard
    def im_start(self, path: str, mode: str, export: str = "", chat_ids: list[str] | None = None,
                 model: str = "sonnet") -> dict[str, Any]:
        """Start the conversation: Claude says what it found and proposes a split."""
        from carrier.readyroom import importer as I

        chat = I.ImportChat.start(path, mode, export, chat_ids, model)
        chat.save()
        return chat.turn(self._llm())

    @_guard
    def im_send(self, iid: str, text: str) -> dict[str, Any]:
        from carrier.readyroom import importer as I

        if not text.strip():
            raise ValueError("Write something first.")
        return I.ImportChat.load(iid).turn(self._llm(), text)

    @_guard
    def im_get(self, iid: str) -> dict[str, Any]:
        from carrier.readyroom import importer as I

        return I.ImportChat.load(iid).view()

    @_guard
    def im_recent(self) -> list[dict[str, Any]]:
        from carrier.readyroom import importer as I

        return I.ImportChat.recent()

    @_guard
    def im_discard(self, iid: str) -> bool:
        from carrier.readyroom import importer as I

        I.ImportChat.load(iid).discard()
        return True

    @_guard
    def im_rule_done(self, iid: str, message: int, index: int, status: str) -> dict[str, Any]:
        """A rule Claude proposed during an import was added to the plan, or dismissed."""
        from carrier.readyroom import importer as I

        return I.ImportChat.load(iid).rule_done(message, index, status)

    @_guard
    def im_check(self, iid: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Save the user's edited plan and list what's wrong with it."""
        from carrier.readyroom import importer as I

        chat = I.ImportChat.load(iid)
        chat.set_plan(plan)
        store = self._store()
        clean, problems = I.validate_plan(chat.root, plan, {p["path"] for p in store.projects()},
                                          {r["name"] for r in store.rule_sets()},
                                          allow_moves=chat.mode != "in_place")
        return {"plan": I.public(clean), "problems": problems}

    @_guard
    def im_history(self, iid: str) -> dict[str, Any]:
        """Whether the workspace has git history yet, and the .gitignore lines to suggest."""
        from carrier.readyroom import importer as I

        root = Path(I.ImportChat.load(iid).root)
        has = gitutil.in_repo(root) and gitutil.has_head(gitutil.repo_root(root))
        return {"has_history": has, "suggest": [] if has else I.suggest_gitignore(root)}

    @_guard
    def im_apply(self, iid: str, plan: dict[str, Any], baseline: bool = True,
                 first_commit: bool = False, ignore_lines: list[str] | None = None) -> dict[str, Any]:
        """Create the projects (after the user confirmed, and approved any moves)."""
        from carrier.readyroom import importer as I

        chat = I.ImportChat.load(iid)
        chat.set_plan(plan)
        if chat.mode == "in_place" and plan.get("moves"):
            raise ValueError("This import works in place; remove the moves first.")
        note = ""
        if first_commit:
            I.validate_plan(chat.root, plan)  # don't commit for a plan that can't be applied
            note = I.first_commit(chat.root, ignore_lines)
        out = I.apply_plan(self._rr(), chat.root, plan, baseline=baseline)
        if note:
            out["warnings"] = [note, *out.get("warnings", [])]
        chat.discard()  # done: the projects hold everything now
        return out

    # -- routines (readyroom/routines.py) --------------------------------------------

    @_guard
    def rr_routines(self, pid: int) -> dict[str, Any]:
        """A project's routines, and whether its review is clear (for gated ones)."""
        from carrier.readyroom import routines as R

        from carrier.core import secrets

        p = self._store().project(pid)
        files = [dict(f, label=lbl) for lbl, folder in self._store().project_roots(p)
                 for f in secrets.unignored_secret_files(Path(folder))]
        return {"routines": self._store().routines(pid), "blockers": R.review_blockers(self._store(), pid),
                "secrets": R.secret_hits(self._store(), pid), "secret_files": files}

    @_guard
    def rr_start_history(self, pid: int) -> dict[str, Any]:
        """Treat the project as reviewed up to now (for work done before it was in Carrier)."""
        from carrier.readyroom import importer as I
        from carrier.readyroom import routines as R

        warnings = I.mark_reviewed(self._store(), pid)
        self._store().log_project_event(pid, "review", "started review history from now")
        R.invalidate(pid)
        out = self.rr_routines.__wrapped__(self, pid)
        out["warnings"] = warnings
        return out

    @_guard
    def rr_mark_secret_safe(self, pid: int, fingerprint: str) -> dict[str, Any]:
        from carrier.readyroom import routines as R

        R.mark_secret_safe(self._store(), pid, fingerprint)
        return self.rr_routines.__wrapped__(self, pid)

    @_guard
    def rr_ignore_secret_files(self, pid: int) -> dict[str, Any]:
        """Add the usual secret-file patterns to .gitignore in each of the project's repositories."""
        from carrier.core import secrets
        from carrier.readyroom import routines as R

        p = self._store().project(pid)
        done = set()
        for _lbl, folder in self._store().project_roots(p):
            try:
                repo, _ = gitutil.locate(Path(folder))
            except gitutil.GitError:
                continue
            if str(repo) not in done:
                done.add(str(repo))
                if secrets.ignore_secret_files(Path(folder)):
                    self._store().log_project_event(pid, "secrets", "added secret-file patterns to .gitignore",
                                                    detail=str(repo / ".gitignore"))
        R.invalidate(pid)
        return self.rr_routines.__wrapped__(self, pid)

    @_guard
    def fd_check_secrets(self, text: str) -> list[dict[str, Any]]:
        """Likely secrets in a message about to be sent (checked on this computer)."""
        from carrier.core import secrets

        return secrets.scan_text(text or "")

    @_guard
    def rr_save_routine(self, pid: int, fields: dict[str, Any], rid: int | None = None) -> dict[str, Any]:
        from carrier.readyroom import routines as R

        gid = fields.get("group_id")
        R.save(self._rr(), pid, str(fields.get("name", "")), str(fields.get("instructions", "")),
               str(fields.get("trigger", "manual")), str(fields.get("gate", "none")),
               bool(fields.get("enabled", True)), rid, int(gid) if gid not in (None, "") else None,
               str(fields.get("mode") or ""))
        return self.rr_routines.__wrapped__(self, pid)

    @_guard
    def rr_delete_routine(self, pid: int, rid: int) -> dict[str, Any]:
        r = self._store().routine(rid)
        if r is None or r["project_id"] != pid:
            raise ValueError("Unknown routine.")
        self._store().delete_routine(rid)
        self._store().log_project_event(pid, "routine", f"deleted the routine {r['name']}")
        return self.rr_routines.__wrapped__(self, pid)

    @_guard
    def rr_set_images(self, pid: int, on: bool) -> dict[str, Any]:
        return self._rr().set_images_allowed(pid, bool(on))

    @_guard
    def rr_set_watch_refs(self, pid: int, on: bool) -> dict[str, Any]:
        return self._rr().set_watch_refs(pid, bool(on))

    @_guard
    def rr_set_handoff(self, tid: int, text: str) -> dict[str, Any]:
        return self._rr().set_handoff(tid, text)

    @_guard
    def rr_set_allow(self, pid: int, changes: dict[str, Any]) -> dict[str, Any]:
        """Change a project's pre-approved actions (see readyroom.manager.set_allowances)."""
        return self._rr().set_allowances(pid, **dict(changes or {}))

    @_guard
    def rr_project_log(self, pid: int) -> list[dict[str, Any]]:
        """The project's activity log: every task change and project event, newest first."""
        return self._rr().project_log(pid)

    @_guard
    def rr_task_history(self, tid: int) -> list[dict[str, Any]]:
        """Every recorded change to a task."""
        return self._rr().task_history(tid)

    @_guard
    def rr_save_group(self, pid: int, fields: dict[str, Any], gid: int | None = None) -> dict[str, Any]:
        return self._rr().save_group(pid, str(fields.get("name", "")), str(fields.get("priority", "normal")),
                                     fields.get("tags"), gid)

    @_guard
    def rr_delete_group(self, pid: int, gid: int) -> dict[str, Any]:
        return self._rr().delete_group(pid, gid)

    @_guard
    def rr_set_task_group(self, tid: int, gid: int | None) -> dict[str, Any]:
        return self._rr().set_task_group(tid, gid)

    @_guard
    def fd_bring_back(self, sid: int, action: str) -> dict[str, Any]:
        """Merge, keep or discard a session's own copy."""
        return self._fd().bring_back(sid, action)

    @_guard
    def rr_copies(self, pid: int) -> list[dict[str, Any]]:
        return self._fd().leftover_copies(pid)

    @_guard
    def fd_set_auto_takeoff(self, on: bool) -> bool:
        return self._fd().set_auto_takeoff(bool(on))

    # -- usage and limits (core/usage.py) ----------------------------------------------

    @_guard
    def usage(self) -> dict[str, Any]:
        """The Usage panel. Reading new lines of Claude Code's logs carries on in the background."""
        from carrier.core import usage

        store = self._store()
        st = usage.scan_state(store)
        if not st["running"] and (not st["done"] or time.time() - st["last"] > 30):
            threading.Thread(target=self._scan_usage, args=(store,), daemon=True, name="usage-scan").start()
        return usage.summary(store)

    @staticmethod
    def _scan_usage(store: Any) -> None:
        from carrier.core import usage

        files = usage.find_logs()
        for _ in range(2000):  # a few seconds at a time, so other work isn't starved
            st = usage.scan(store, budget=1.5, files=files)
            if st.get("done") or st.get("off"):
                return
            time.sleep(0.05)

    @_guard
    def fd_continue(self, sid: int) -> dict[str, Any]:
        """Carry on a session that stopped at a usage limit."""
        return self._fd().continue_session(sid)

    @_guard
    def rr_run_routine(self, rid: int) -> dict[str, Any]:
        """Queue the routine as the project's next task."""
        from carrier.readyroom import routines as R

        return R.run(self._rr(), rid)

    # -- skills (core/skills.py) ----------------------------------------------------

    def _skill_root(self, pid: int | None) -> str | None:
        return self._store().project(pid)["path"] if pid is not None and self._store().project(pid) else None

    @_guard
    def rr_skills(self, pid: int | None = None) -> dict[str, Any]:
        """A project's skills (if ``pid``) and the user's personal ones."""
        from carrier.core import skills

        return skills.list_skills(self._skill_root(pid))

    @_guard
    def rr_save_skill(self, pid: int | None, name: str, description: str, body: str, scope: str = "project",
                      old_name: str = "") -> dict[str, Any]:
        from carrier.core import skills

        saved = skills.save(self._skill_root(pid), name, description, body, scope, old_name)
        if pid is not None and scope == "project":
            self._store().log_project_event(pid, "skill", f"saved the skill {saved['name']}", detail=description)
        return skills.list_skills(self._skill_root(pid))

    @_guard
    def rr_delete_skill(self, pid: int | None, name: str, scope: str = "project") -> dict[str, Any]:
        from carrier.core import skills

        skills.delete(self._skill_root(pid), name, scope)
        if pid is not None and scope == "project":
            self._store().log_project_event(pid, "skill", f"deleted the skill {name}")
        return skills.list_skills(self._skill_root(pid))

    @_guard
    def fd_apply_skill(self, sid: int, proposal_id: str, name: str, description: str, body: str,
                       scope: str = "project") -> str:
        """Save a skill Claude proposed (as the user edited it)."""
        from carrier.core import skills

        deck = self._fd()
        s = deck.store.session(sid)
        if s is None:
            raise ValueError("Unknown session.")
        root = self._skill_root(s["project_id"]) if scope == "project" else None
        if scope == "project" and root is None:
            raise ValueError("This session has no project; save it as a personal skill instead.")
        saved = skills.save(root, name, description, body, scope)
        if scope == "project":
            self._store().log_project_event(s["project_id"], "skill", f"saved the skill {saved['name']} Claude suggested",
                                            by="claude", detail=description, session_id=sid)
        where = "this project's skills" if scope == "project" else "your personal skills"
        text = f"Saved {saved['name']} to {where}."
        deck.emit(sid, {"type": "skill_done", "id": proposal_id, "decision": "applied", "text": text})
        return text

    @_guard
    def fd_dismiss_skill(self, sid: int, proposal_id: str) -> bool:
        self._fd().emit(sid, {"type": "skill_done", "id": proposal_id, "decision": "dismissed"})
        return True

    @_guard
    def rr_preview(self, pid: int | None, task_id: int | None = None, preset_id: int | None = None) -> str:
        return self._rr().preview(pid, task_id, preset_id)

    def _shutdown(self) -> None:
        if self._session is not None:
            self._session.close()
        if self._deck is not None:
            self._deck.close()


def dumps(kind: str, data: Any) -> str:
    """JS snippet that delivers an event to the page (used by main.py)."""
    return f"window.carrierEvent && window.carrierEvent({json.dumps(kind)}, {json.dumps(data)})"
