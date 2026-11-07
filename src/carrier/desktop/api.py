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

    def _bind(self, emit: Emit, pick_folder: Callable[[], str | None]) -> None:
        """Called by main.py once the window exists."""
        self._emit = emit
        self._pick_folder = pick_folder

    def _need(self) -> S.ReviewSession:
        if self._session is None:
            raise S.SessionError("Open a project first.")
        return self._session

    # -- setup ---------------------------------------------------------------

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
            info["is_repo"] = info["exists"] and gitutil.is_repo(folder)
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
        return self._need().finish()

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
                      effort: str | None = None) -> dict[str, Any]:
        return self._fd().start_task(task_id, isolation, preset_id or None, model or None, mode or None, send_task,
                                     effort or None)

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
    def fd_send(self, sid: int, text: str) -> bool:
        return self._fd().send(sid, text)

    @_guard
    def fd_interrupt(self, sid: int) -> bool:
        return self._fd().interrupt(sid)

    @_guard
    def fd_answer(self, sid: int, req_id: str, decision: str, message: str = "") -> bool:
        return self._fd().answer(sid, req_id, decision, message)

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
    def fd_end(self, sid: int) -> dict[str, Any]:
        return self._fd().end_session(sid)

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
        allowed = ("name", "path", "goal", "first_task", "tags", "rules", "rule_set_ids", "preset_id", "template_id")
        return self._rr().create_project(**{k: v for k, v in fields.items() if k in allowed})

    @_guard
    def rr_update_project(self, pid: int, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = ("name", "path", "goal", "tags", "rules", "rule_set_ids", "preset_id")
        return self._rr().update_project(pid, **{k: v for k, v in fields.items() if k in allowed})

    @_guard
    def rr_remove_project(self, pid: int) -> dict[str, Any]:
        return self._rr().remove_project(pid)

    @_guard
    def rr_add_task(self, pid: int, title: str, details: str = "", first: bool = False) -> dict[str, Any]:
        return self._rr().add_task(pid, title, details, first)

    @_guard
    def rr_update_task(self, tid: int, title: str, details: str) -> dict[str, Any]:
        return self._rr().update_task(tid, title, details)

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
        return self._rr().add_folder(pid, path, kind=kind) if path else None

    @_guard
    def rr_set_folder_kind(self, pid: int, path: str, kind: str) -> dict[str, Any]:
        return self._rr().set_folder_kind(pid, path, kind)

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
    def rr_draft(self, kind: str, description: str, current: str = "", pid: int | None = None) -> dict[str, Any]:
        """Draft rules or a review agent from the user's description (nothing is saved)."""
        from carrier.core.llm import AgentSDKBackend
        from carrier.readyroom import drafting

        backend = self._backend_factory() if self._backend_factory else None
        if backend is None:
            backend = AgentSDKBackend(concurrency=1)
        project = self._store().project(pid) if pid is not None else None
        return drafting.draft(kind, description, backend, current, (project or {}).get("goal", ""))

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
