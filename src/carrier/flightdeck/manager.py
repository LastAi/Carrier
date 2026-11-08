# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""FlightDeck — the API every front end uses to run Claude Code sessions.

Like Debrief's ``ReviewSession``, every public method takes and returns
plain JSON values and is safe to call from any thread: the deck owns a
private asyncio loop on a background thread, where all session runners
live, so several Claude Code sessions run truly in parallel.

The UI learns what happens through :meth:`poll` — an ordered stream of
events (``{seq, session, event}``) — rather than callbacks. Polling keeps
the desktop bridge simple and is exactly what a future network front end
would need.

Session statuses
----------------
starting   connecting to Claude Code
working    Claude is busy
needs_you  waiting for a permission decision
idle       turn finished — waiting for the user's next message
paused     Carrier was closed while the session was open; can be resumed
error      Claude Code reported an error (the session can still be resumed)
ended      closed by the user
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import threading
import traceback
import time
from collections import deque
from pathlib import Path
from typing import Any

from carrier.core import gitutil, usersettings
from carrier.flightdeck import coordination, worktrees
from carrier.readyroom import context as rr_context
from carrier.readyroom.manager import ReadyRoom, RoomError
from carrier.core.db import ACTIVE, Store, default_path, folder_kind
from carrier.core import models
from carrier.core.llm import EFFORTS
from carrier.flightdeck.runner import ClientFactory, SessionRunner, default_client_factory

POLICIES = ("warn", "ask", "block", "inform")
MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
MAX_EVENTS = 5000


class DeckError(Exception):
    """A user-facing problem; the message is written for the user."""


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


MAX_IMAGES = 6  # pasted images per message
TAKEOFF_RETRY_SECONDS = 300  # after a failed auto take-off, wait this long before trying that run again


class FlightDeck:
    def __init__(self, db_path: Path | None = None, client_factory: ClientFactory | None = None,
                 policy: str = "warn") -> None:
        """Create the deck. Only the process that *runs* sessions (the desktop
        app) should create one: on start it marks sessions left open by a
        previous run as "paused". Read-only tools (the CLI) use the Store."""
        self.store = Store(db_path or default_path())
        self.room = ReadyRoom(self.store)  # projects, tasks and rules live in the Ready Room
        self.client_factory = client_factory or default_client_factory
        self.policy = policy if policy in POLICIES else "warn"
        self.runners: dict[int, SessionRunner] = {}
        self._last_takeoff = 0.0
        self._takeoff_lock = threading.Lock()
        self._takeoff_errors: dict[int, str] = {}  # task id -> why auto take-off couldn't start it
        self._takeoff_failed: dict[int, float] = {}  # task id -> when it last failed to start
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._ev_lock = threading.Lock()
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="flightdeck")
        self._thread.start()
        self._mark_orphans_paused()

    # -- plumbing ------------------------------------------------------------

    def _run(self, coro: Any, timeout: float | None = 120) -> Any:
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)
        except concurrent.futures.TimeoutError as exc:
            raise DeckError("Claude Code did not respond in time.") from exc

    def _spawn(self, coro: Any) -> None:
        """Run in the background (e.g. a whole turn); errors become events."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def emit(self, session_id: int, event: dict[str, Any], persist: bool = True) -> None:
        event = {**event, "ts": time.time()}
        if persist:
            self.store.add_message(session_id, event)
        with self._ev_lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "session": session_id, "event": event})

    def _mark_orphans_paused(self) -> None:
        """Sessions that were open when Carrier last closed can be resumed."""
        for s in self.store.sessions(active_only=True):
            if s["status"] != "paused":
                self.store.update_session(s["id"], status="paused", last_activity=s["last_activity"])

    def close(self) -> None:
        """Disconnect every session (they become "paused", resumable) and stop
        the loop. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        for runner in list(self.runners.values()):
            try:
                self._run(runner.stop(), timeout=10)
            except Exception:
                pass
            self.store.update_session(runner.sid, status="paused")
        self.runners.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)

    def owners(self, session: dict[str, Any], file_path: str) -> list[tuple[int, str]]:
        """Every project whose *work* folders contain ``file_path``, with its name there.

        The session's own project comes first. A folder can be one project's
        main folder and another's work folder, so an edit there concerns both:
        each gets the edit in its own log (and its own coordination). Within a
        project the deepest folder wins. Files in an extra folder are named
        "<label>/<path>". A standalone session's own folder belongs to no
        project: its edits are tracked only where they land in a project.
        """
        target = _norm(file_path)
        best: dict[int, tuple[str, str, str]] = {}  # pid -> (norm root, real root, label)

        def consider(pid: int, root: str, label: str) -> None:
            norm_root = _norm(root)
            if target == norm_root or target.startswith(norm_root.rstrip(os.sep) + os.sep):
                if pid not in best or len(norm_root) > len(best[pid][0]):
                    best[pid] = (norm_root, root, label)

        own = session["project_id"]
        if own is not None:
            consider(own, session["cwd"], "")  # its worktree, if isolated
        for p in self.store.projects():
            for label, root in self.store.project_roots(p):
                consider(p["id"], root, label)
        out = []
        for pid in sorted(best, key=lambda x: x != own):
            _, real_root, label = best[pid]
            rel = Path(os.path.relpath(os.path.realpath(file_path), os.path.realpath(real_root))).as_posix()
            out.append((pid, f"{label}/{rel}" if label else rel))
        return out

    def locate(self, session: dict[str, Any], file_path: str) -> tuple[int, str] | None:
        """The main owner of ``file_path`` (see :meth:`owners`), or None if untracked."""
        found = self.owners(session, file_path)
        return found[0] if found else None

    def reference_folder(self, session: dict[str, Any], file_path: str) -> dict[str, Any] | None:
        """The session's reference folder containing ``file_path`` (edits there are
        blocked), unless the file is also inside one of its work folders."""
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        if project is None:
            return None
        target = _norm(file_path)
        inside = lambda root: target == _norm(root) or target.startswith(_norm(root).rstrip(os.sep) + os.sep)  # noqa: E731
        if inside(session["cwd"]) or any(inside(r) for _, r in self.store.project_roots(project)):
            return None
        return next((f for f in project["folders"] if folder_kind(f) == "reference" and inside(f["path"])), None)

    def workspace_target(self, session: dict[str, Any], file_path: str) -> tuple[dict[str, Any], Path | None] | None:
        """For a file outside the session's work folders but inside one of its workspace
        folders: ``(workspace, new folder)``, where the new folder is the first folder
        on the way to the file that doesn't exist yet (None: the file would go into an
        existing folder, which stays read only). None when no workspace is involved."""
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        if project is None:
            return None
        target = _norm(file_path)
        inside = lambda root: target == _norm(root) or target.startswith(_norm(root).rstrip(os.sep) + os.sep)  # noqa: E731
        if inside(session["cwd"]) or any(inside(r) for _, r in self.store.project_roots(project)):
            return None
        spaces = [f for f in project["folders"] if folder_kind(f) == "workspace" and inside(f["path"])]
        if not spaces:
            return None
        ws = max(spaces, key=lambda f: len(f["path"]))
        rel = Path(os.path.relpath(os.path.realpath(file_path), os.path.realpath(ws["path"])))
        current = Path(ws["path"])
        for part in rel.parts[:-1]:  # the folders on the way to the file
            current = current / part
            if not current.exists():
                return ws, current
        return ws, None

    def claim_folder(self, session: dict[str, Any], folder: Path) -> dict[str, Any]:
        """Create ``folder`` (new, inside a workspace folder) and add it to the session's project as a work folder."""
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        if project is None:
            raise DeckError("This session has no project.")
        if folder.exists():
            raise DeckError(f"{folder} already exists; only new folders can be added from a workspace.")
        folder.mkdir(parents=True)
        try:
            p = self.room.add_folder(project["id"], str(folder), kind="work")
        except Exception:
            try:
                folder.rmdir()
            except OSError:
                pass
            raise
        return next(f for f in p["folders"] if Path(f["path"]) == folder.resolve())

    # -- what Claude can read about other projects (Carrier's get_project tool) -------

    def projects_text(self) -> str:
        lines = []
        for p in self.store.projects():
            tasks = self.store.tasks(p["id"])
            done = sum(1 for t in tasks if t["status"] == "done")
            goal = (p["goal"] or "").strip().splitlines()
            lines.append(f"- {p['name']} — {p['path']} — {done}/{len(tasks)} tasks done"
                         + (f" — {goal[0][:160]}" if goal else ""))
        return "\n".join(lines) or "No projects yet."

    def project_text(self, name: str) -> str:
        from carrier.core import skills

        wanted = (name or "").strip().lower()
        p = next((x for x in self.store.projects() if x["name"].lower() == wanted), None)
        if p is None:
            raise DeckError(f"No project called {name}. Projects: "
                            + ", ".join(x["name"] for x in self.store.projects()))
        out = [f"# {p['name']}", f"Main folder: {p['path']}", f"\n## Goal\n{p['goal'] or '(none)'}",
               f"\n## Rules\n{p['rules'] or '(none)'}"]
        for rid in self.store.project_rule_set_ids(p["id"]):
            rs = self.store.rule_set(rid)
            if rs:
                out.append(f"\n## Rule set: {rs['name']}\n{rs['body']}")
        if p["folders"]:
            out.append("\n## Extra folders")
            out += [f"- {f['label']} ({folder_kind(f)}): {f['path']}" for f in p["folders"]]
        tasks = self.store.tasks(p["id"], statuses=("planned", "active"))
        if tasks:
            out.append("\n## Open tasks")
            out += [f"- {t['title']}" for t in tasks]
        found = skills.list_skills(p["path"])["project"]
        if found:
            out.append("\n## Skills (in .claude/skills)")
            for sk in found:
                out.append(f"### {sk['name']} — {sk['description']}\n{sk['body'][:4000]}")
        return "\n".join(out)

    def extra_dirs(self, session: dict[str, Any]) -> list[str]:
        """The session's project's extra folders that exist, work and reference
        (passed to Claude Code so it can read them; reference edits are blocked)."""
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        return [f["path"] for f in (project or {}).get("folders", []) if Path(f["path"]).is_dir()]

    def _runner(self, sid: int) -> SessionRunner:
        runner = self.runners.get(sid)
        if runner is None:
            raise DeckError("This session isn't running. Resume it first.")
        return runner

    # -- projects & links ------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        projects = self.store.projects()
        links = self.store.links()
        names = {p["id"]: p["name"] for p in projects}
        out = []
        for p in projects:
            sessions = self.store.sessions(p["id"])
            open_tasks = self.store.tasks(p["id"], statuses=("planned", "active"))
            main_queue = [t for t in open_tasks if not t.get("group_id")]
            groups = []
            for g in self.store.task_groups(p["id"]):
                q = [t for t in open_tasks if t.get("group_id") == g["id"]]
                groups.append({**g, "current_task": q[0] if q else None, "open": len(q)})
            out.append({
                **p,
                "current_task": main_queue[0] if main_queue else None,
                "groups": groups,
                "open_tasks": open_tasks,
                "is_repo": worktrees.can_isolate(Path(p["path"])),
                "links": [{"id": l["id"], "with": names.get(l["project_b"] if l["project_a"] == p["id"]
                                                            else l["project_a"]), "note": l["note"]}
                          for l in links if p["id"] in (l["project_a"], l["project_b"])],
                "sessions": [self._session_summary(s) for s in sessions],
            })
        standalone = []
        loose = [s for s in self.store.sessions() if s["project_id"] is None]
        for t in self.store.tasks(None, statuses=("planned", "active", "done")):
            ss = [self._session_summary(s) for s in loose if s.get("task_id") == t["id"]]
            if t["status"] == "done" and not ss and t["finished"] and time.time() - t["finished"] > 7 * 86400:
                continue  # finished more than a week ago and never assigned: hide
            standalone.append({**t, "sessions": ss})
        all_sessions = [s for p in out for s in p["sessions"]] + [s for t in standalone for s in t["sessions"]]
        from carrier.readyroom import routines

        return {"projects": out, "standalone": standalone, "links": [self._link_view(l, names) for l in links],
                "takeoff": [{**q, "error": self._takeoff_errors.get(q["task_id"], "")}
                            for q in routines.takeoff_queue(self.store)],
                "auto_takeoff": self.auto_takeoff,
                "needs_you": sum(1 for s in all_sessions if s["status"] == "needs_you"), "policy": self.policy,
                "active": sum(1 for s in all_sessions if s["status"] in ACTIVE),
                "presets": self.store.presets()}

    def _link_view(self, l: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
        return {**l, "name_a": names.get(l["project_a"]), "name_b": names.get(l["project_b"])}

    def _session_summary(self, s: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": s["id"], "project_id": s["project_id"], "task": s["task"], "status": s["status"],
            "worktree": bool(s["worktree"]), "branch": s["branch"], "cwd": s["cwd"], "mode": s["mode"],
            "model": s["model"], "effort": s.get("effort"), "cost": round(s["cost_usd"] or 0, 4), "started": s["started"],
            "last_activity": s["last_activity"], "running": s["id"] in self.runners,
            "task_id": s.get("task_id"), "policy": s.get("policy"),
            "alerts": len(self.store.pending_events(s["id"])),
        }

    def add_project(self, path: str, name: str | None = None) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise DeckError(f"Folder not found: {path}")
        try:  # use the repository root when the folder is inside one
            p = gitutil.repo_root(p)
        except gitutil.GitError:
            pass
        self.store.add_project(name or p.name, str(p))
        return self.overview()

    def rename_project(self, project_id: int, name: str) -> dict[str, Any]:
        if not name.strip():
            raise DeckError("A project needs a name.")
        self.store.rename_project(project_id, name.strip())
        return self.overview()

    def remove_project(self, project_id: int) -> dict[str, Any]:
        if self.store.sessions(project_id, active_only=True):
            raise DeckError("End this project's sessions first.")
        self.store.remove_project(project_id)
        return self.overview()

    def add_link(self, a: int, b: int, note: str = "", contracts_a: list[str] | None = None,
                 contracts_b: list[str] | None = None) -> dict[str, Any]:
        if a == b:
            raise DeckError("Pick two different projects to link.")
        if not (self.store.project(a) and self.store.project(b)):
            raise DeckError("Unknown project.")
        clean = lambda xs: [x.strip() for x in (xs or []) if x and x.strip()]  # noqa: E731
        self.store.add_link(a, b, note.strip(), clean(contracts_a), clean(contracts_b))
        return self.overview()

    def remove_link(self, link_id: int) -> dict[str, Any]:
        self.store.remove_link(link_id)
        return self.overview()

    # -- sessions --------------------------------------------------------------

    def isolation_choice(self, project_id: int) -> dict[str, Any]:
        """Does starting a session here need the "share or own copy" question?"""
        project = self.store.project(project_id)
        if project is None:
            raise DeckError("Unknown project.")
        sharing = [self._session_summary(s) for s in self.store.sessions(project_id, active_only=True)
                   if not s["worktree"]]
        can = worktrees.can_isolate(Path(project["path"]))
        uncommitted = can and worktrees.has_uncommitted(Path(project["path"]))
        return {"needs_choice": bool(sharing), "can_isolate": can, "sharing": sharing,
                "uncommitted": uncommitted}

    def start_session(self, project_id: int, task: str, isolation: str = "shared",
                      model: str | None = None, mode: str = "default", send_task: bool = True) -> dict[str, Any]:
        """Start a session on a *new* task in a project (added to its queue)."""
        if self.store.project(project_id) is None:
            raise DeckError("Unknown project.")
        if not task.strip():
            raise DeckError("Give the session a one-line task, so other sessions know what it's doing.")
        tid = self.store.add_task(task.strip(), project_id)
        return self.start_task(tid, isolation=isolation, model=model, mode=mode, send_task=send_task)

    def create_standalone(self, title: str, details: str = "", folder: str | None = None) -> dict[str, Any]:
        """A task with no project. ``folder=None`` makes a fresh scratch folder (git-tracked)."""
        title = title.strip()
        if not title:
            raise DeckError("Give the task a one-line title.")
        if folder:
            cwd = Path(folder).expanduser()
            if not cwd.is_dir():
                raise DeckError(f"Folder not found: {folder}")
        else:
            cwd = usersettings.config_dir() / "scratch" / f"{int(time.time())}-{worktrees.slug(title)}"
            cwd.mkdir(parents=True)
            gitutil.git(cwd, "init", "-q")
            env = {"GIT_AUTHOR_NAME": "Carrier", "GIT_AUTHOR_EMAIL": "carrier@localhost",
                   "GIT_COMMITTER_NAME": "Carrier", "GIT_COMMITTER_EMAIL": "carrier@localhost"}
            gitutil.git(cwd, "commit", "-q", "--allow-empty", "-m", "Scratch folder (created by Carrier)", env=env)
        tid = self.store.add_task(title, None, details=details.strip(), cwd=str(cwd.resolve()))
        return self.store.task(tid)

    def start_task(self, task_id: int, isolation: str = "shared", preset_id: int | None = None,
                   model: str | None = None, mode: str | None = None, send_task: bool = True,
                   effort: str | None = None, auto: bool = False) -> dict[str, Any]:
        """Start a Flight Deck session working on a task (project or standalone).

        Settings come from, in order: explicit arguments, the chosen preset,
        the project's default preset, then Claude Code's defaults.
        """
        task = self.store.task(task_id)
        if task is None:
            raise DeckError("Unknown task.")
        if task["status"] == "done":
            raise DeckError("This task is done. Reopen it in the Ready Room to work on it again.")
        from carrier.readyroom import routines

        try:
            routines.check_gate(self.store, task)  # e.g. "push" only once everything is reviewed
        except routines.RoutineError as exc:
            raise DeckError(str(exc)) from exc
        pid = task["project_id"]
        project = self.store.project(pid) if pid is not None else None
        if project is None and not task["cwd"]:
            raise DeckError("This task has no folder.")
        preset = self.store.preset(preset_id if preset_id else (project["preset_id"] if project else None))
        model = model or (preset["model"] if preset else None)
        effort = effort or (preset.get("effort") if preset else None)
        if not models.known(model):
            raise DeckError(f"Unknown model: {model}")
        if effort and effort not in EFFORTS:
            raise DeckError(f"Unknown effort: {effort}")
        routine = self.store.routine(task["routine_id"]) if task.get("routine_id") else None
        mode = mode or (routine or {}).get("mode") or (preset["mode"] if preset else "default")
        if (auto or routine) and mode == "bypassPermissions":
            mode = "default"  # a routine (and anything started on its own) never skips asking
        if mode not in MODES:
            raise DeckError(f"Unknown permission mode: {mode}")
        cwd = Path(project["path"] if project else task["cwd"])
        if not cwd.is_dir():
            raise DeckError(f"The folder {cwd} doesn't exist any more. Fix it in the Ready Room.")
        first_line = task["title"]
        sid = self.store.add_session(project_id=pid, task=first_line, cwd=str(cwd), model=model or None,
                                     mode=mode, task_id=task_id, policy=(preset or {}).get("policy"),
                                     preset_id=preset["id"] if preset else None, effort=effort or None)
        if isolation == "worktree":
            if not worktrees.can_isolate(cwd):
                self.store.update_session(sid, status="ended", ended=time.time())
                raise DeckError("An own copy needs a git repository with at least one commit.")
            try:
                folder, branch = worktrees.create(cwd, project["name"] if project else "standalone", sid, first_line)
            except gitutil.GitError as exc:
                self.store.update_session(sid, status="ended", ended=time.time())
                raise DeckError(f"Could not create the copy: {exc}") from exc
            self.store.update_session(sid, cwd=str(folder), worktree=1, branch=branch)
            cwd = folder
        # Remember where this session started, so Debrief can review just its work.
        if worktrees.can_isolate(cwd):
            try:
                base = gitutil.snapshot_worktree(gitutil.repo_root(cwd), keep_ref=f"refs/carrier/flightdeck/{sid}")
                self.store.update_session(sid, base_ref=base)
            except gitutil.GitError as exc:  # Debrief this won't have a start point; the session still runs
                self.emit(sid, {"type": "notice", "kind": "snapshot",
                                "text": f"Couldn't record a starting point for Debrief: {exc}"})
        self.store.update_task(task_id, status="active")
        runner = SessionRunner(self, sid, self.client_factory)
        self.runners[sid] = runner
        branch = self.store.session(sid)["branch"]
        self.emit(sid, {"type": "notice", "kind": "start",
                        "text": f"Session started in {cwd}" + (f" (own copy on branch {branch})" if branch else "")
                                + (f" with preset “{preset['name']}”" if preset else "")})
        try:
            self._run(runner.start())
        except Exception as exc:
            self.runners.pop(sid, None)
            self.store.update_session(sid, status="error")
            self._release_task(task_id)
            self.emit(sid, {"type": "error", "text": f"Could not start Claude Code: {exc}"})
            raise DeckError(f"Could not start Claude Code: {exc}") from exc
        if send_task:
            text = task["title"] + (f"\n\n{task['details']}" if task["details"].strip() else "")
            self.send(sid, text)
        return self._session_summary(self.store.session(sid))

    def context_for(self, session: dict[str, Any]) -> str:
        """Ready Room context (goal, rules, task, history) for a session's system prompt."""
        task = self.store.task(session["task_id"]) if session.get("task_id") else None
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        # The preset chosen when the session started; older sessions fall back
        # to the project's default preset.
        preset_id = session.get("preset_id") or (project["preset_id"] if project else None)
        preset = self.store.preset(preset_id) if preset_id else None
        return rr_context.build(self.store, session["project_id"], task, preset)

    def _release_task(self, task_id: int | None) -> None:
        """Put a task back to "planned" when no live session works on it any more."""
        if not task_id:
            return
        task = self.store.task(task_id)
        if task and task["status"] == "active":
            still = [s for s in self.store.sessions(active_only=True)
                     if s.get("task_id") == task_id and s["id"] in self.runners]
            if not still:
                self.store.update_task(task_id, status="planned")

    SUMMARY_PROMPT = ("[Carrier] The user marked this task as done. In 2–3 sentences, summarize what was done "
                      "for the task — key changes, decisions, and anything left open. Reply with the summary "
                      "only; it is saved to the project's history for future sessions.")

    def complete_task(self, sid: int, summary: str | None = None) -> dict[str, Any]:
        """Mark the session's task done.

        With ``summary`` it is saved right away; without it Claude is asked
        to write one (the session must be running and idle), and the task is
        marked done when the reply arrives.
        """
        s = self.store.session(sid)
        if s is None or not s.get("task_id"):
            raise DeckError("This session has no task.")
        tid = s["task_id"]
        if summary is not None:
            self.room.finish_task(tid, summary)
            self.emit(sid, {"type": "notice", "kind": "task_done", "text": "Task marked done."})
            return {"done": True, "task": self.store.task(tid)}
        runner = self._runner(sid)
        if not runner.claim():
            raise DeckError("Claude is still working. Wait for it, or press Stop.")

        def finished(text: str) -> None:
            self.room.finish_task(tid, text)
            self.emit(sid, {"type": "notice", "kind": "task_done",
                            "text": "Task marked done. Summary saved to the project history."})

        def failed() -> None:
            self.emit(sid, {"type": "notice", "kind": "task_not_done",
                            "text": "No summary was written, so the task was not marked done. "
                                    "Try ✓ Mark done again, or write the summary yourself."})

        self.emit(sid, {"type": "notice", "kind": "summary", "text": "Asking Claude for a short summary…"})
        self._spawn(runner.send(self.SUMMARY_PROMPT, on_done=finished, on_fail=failed))
        return {"done": False, "task": self.store.task(tid)}

    def upgrade_standalone(self, task_id: int, name: str, goal: str = "",
                           tags: list[str] | str | None = None) -> dict[str, Any]:
        self._no_live_session(task_id)
        try:
            return self.room.upgrade_standalone(task_id, name, goal, tags)
        except RoomError as exc:
            raise DeckError(str(exc)) from exc

    def assign_standalone(self, task_id: int, project_id: int, add_merge_task: bool = True) -> dict[str, Any]:
        self._no_live_session(task_id)
        try:
            return self.room.assign_standalone(task_id, project_id, add_merge_task)
        except RoomError as exc:
            raise DeckError(str(exc)) from exc

    def _no_live_session(self, task_id: int) -> None:
        """Refuse while a connected session works on the task; end paused ones.

        After a restart, sessions are "paused" with no connection. Turning
        their task into (or adding it to) a project ends them, as the user
        clearly has finished with them.
        """
        if any(r.row.get("task_id") == task_id for r in self.runners.values()):
            raise DeckError("End the task's session first.")
        for s in self.store.sessions(active_only=True):
            if s.get("task_id") == task_id:
                self.store.update_session(s["id"], status="ended", ended=time.time())
                self.emit(s["id"], {"type": "status", "status": "ended"})
        self._release_task(task_id)

    def resume_session(self, sid: int) -> dict[str, Any]:
        s = self.store.session(sid)
        if s is None:
            raise DeckError("Unknown session.")
        if sid in self.runners:
            return self._session_summary(s)
        if not Path(s["cwd"]).is_dir():
            raise DeckError("This session's folder no longer exists.")
        runner = SessionRunner(self, sid, self.client_factory)
        self.runners[sid] = runner
        try:
            self._run(runner.start(resume=s["sdk_session"]))
        except Exception as exc:
            self.runners.pop(sid, None)
            raise DeckError(f"Could not resume: {exc}") from exc
        self.emit(sid, {"type": "notice", "kind": "resume",
                        "text": "Session resumed." if s["sdk_session"] else
                        "Resumed as a new conversation (the previous one never got a Claude Code session id)."})
        return self._session_summary(self.store.session(sid))

    def send(self, sid: int, text: str, images: list[dict[str, Any]] | None = None) -> bool:
        """Send a message; ``images`` are pasted pictures ``{"mime", "data" (base64), "note"}``."""
        from carrier.core import images as I

        text = text.strip()
        clean = []
        for img in (images or [])[:MAX_IMAGES]:
            I.check_pasted(str(img.get("mime", "")), str(img.get("data", "")))  # raises ValueError
            clean.append({"mime": img["mime"], "data": img["data"], "note": str(img.get("note", "")).strip()[:2000]})
        if not text and not clean:
            raise DeckError("Type a message first.")
        row = self.store.session(sid)
        if clean and row and not self.room.images_allowed(row["project_id"]):
            raise DeckError("Images are turned off for this project (Ready Room → the project → Images).")
        runner = self._runner(sid)
        if not runner.claim():
            raise DeckError("Claude is still working. Wait for it, or press Stop.")
        self._spawn(runner.send(text or "(see the attached image)", images=clean))
        return True

    def interrupt(self, sid: int) -> bool:
        self._run(self._runner(sid).interrupt(), timeout=30)
        return True

    def answer(self, sid: int, req_id: str, decision: str, message: Any = "") -> bool:
        if decision not in ("allow", "allow_always", "deny", "answers", "skip", "work", "change", "decline"):
            raise DeckError(f"Unknown decision: {decision}")
        runner = self._runner(sid)

        async def _answer() -> bool:  # resolve the future on the deck's own loop
            return runner.answer(req_id, decision, message)

        if not self._run(_answer(), timeout=10):
            raise DeckError("That request is no longer waiting for an answer.")
        return True

    def set_mode(self, sid: int, mode: str) -> dict[str, Any]:
        if mode not in MODES:
            raise DeckError(f"Unknown permission mode: {mode}")
        if sid in self.runners:
            self._run(self.runners[sid].set_mode(mode), timeout=30)
        else:
            self.store.update_session(sid, mode=mode)
        return self._session_summary(self.store.session(sid))

    def set_model(self, sid: int, model: str | None) -> dict[str, Any]:
        """Switch the session's model; takes effect from the next message.

        Effort is fixed when Claude Code connects, so if the new model supports
        a different effort level than the old one, the session reconnects.
        """
        if not models.known(model):
            raise DeckError(f"Unknown model: {model}")
        if sid in self.runners:
            runner = self.runners[sid]
            if runner.busy:
                raise DeckError("Claude is still working. Change the model when it's waiting for you.")
            s = self.store.session(sid)
            before = models.effective_effort(s["model"], s.get("effort"))
            if models.effective_effort(model, s.get("effort")) != before:
                self.store.update_session(sid, model=model)
                self._reconnect(sid)
            else:
                self._run(runner.set_model(model), timeout=30)
        else:
            self.store.update_session(sid, model=model)
        self.emit(sid, {"type": "notice", "kind": "model",
                        "text": f"Model: {model or 'Claude Code default'} (from the next message)."})
        return self._session_summary(self.store.session(sid))

    def set_effort(self, sid: int, effort: str | None) -> dict[str, Any]:
        """Change the session's reasoning effort.

        Claude Code fixes effort when it connects, so a running session is
        reconnected and resumed (same conversation) with the new setting.
        """
        if effort and effort not in EFFORTS:
            raise DeckError(f"Unknown effort: {effort}")
        runner = self.runners.get(sid)
        if runner is not None and runner.busy:
            raise DeckError("Claude is still working. Change the effort when it's waiting for you.")
        self.store.update_session(sid, effort=effort)
        if runner is not None:
            self._reconnect(sid)
        used = models.effective_effort(self.store.session(sid)["model"], effort)
        if effort and used != effort:
            self.emit(sid, {"type": "notice", "kind": "effort",
                            "text": f"This model doesn't offer {effort}; it runs at {used or 'its default'}. "
                                    "Your choice is kept for models that do."})
        self.emit(sid, {"type": "notice", "kind": "effort",
                        "text": f"Effort: {effort or 'default'} (same conversation, from the next message)."})
        return self._session_summary(self.store.session(sid))

    def _reconnect(self, sid: int) -> None:
        """Reconnect a running session with its stored settings, same conversation."""
        runner = self.runners.pop(sid, None)
        if runner is not None:
            self._run(runner.stop(), timeout=30)
        fresh = SessionRunner(self, sid, self.client_factory)
        self.runners[sid] = fresh
        try:
            self._run(fresh.start(resume=self.store.session(sid)["sdk_session"]))
        except Exception as exc:
            self.runners.pop(sid, None)
            self.store.update_session(sid, status="paused")
            raise DeckError(f"Could not reconnect with the new settings: {exc}. Press Resume.") from exc

    # -- the user changed a task a session is working on ----------------------------------

    def notify_task_change(self, task_id: int, before: dict[str, Any], after: dict[str, Any]) -> list[int]:
        """Pause the running sessions on ``task_id`` (if mid-turn) and tell them how the task
        changed, so the work — and later the review — follow the task as it now reads."""
        from carrier.readyroom.manager import _diff_lines

        lines = []
        if before.get("title") != after.get("title"):
            lines.append(f"Title: “{before.get('title')}” → “{after.get('title')}”")
        diff = _diff_lines(str(before.get("details") or ""), str(after.get("details") or ""))
        if diff:
            lines.append("Details changed (- removed, + added):\n" + diff)
        if not lines:
            return []
        message = ("[Carrier Ready Room] The user just changed the task you are working on. Pause: re-read it "
                   "below, say briefly how this changes your plan (and anything already done that no longer "
                   "fits), then continue with the task as it now reads.\n\n" + "\n".join(lines)
                   + f"\n\nThe task now reads:\n{after.get('title')}\n{after.get('details') or ''}")
        touched = []
        for sid, runner in list(self.runners.items()):
            if runner.row.get("task_id") != task_id or runner.row["status"] == "ended":
                continue
            touched.append(sid)
            self.emit(sid, {"type": "notice", "kind": "task_changed",
                            "text": "You changed this task; Claude is pausing to take it in."})

            async def deliver(r: SessionRunner = runner) -> None:
                if r.busy:
                    await r.interrupt()
                    for _ in range(300):  # let the interrupted turn wind down (≤ 30 s)
                        if not r.busy:
                            break
                        await asyncio.sleep(0.1)
                if r.claim():
                    await r.send(message)
                else:
                    self.emit(r.sid, {"type": "notice", "kind": "task_changed",
                                      "text": "Claude was still busy, so the task change wasn't sent. Tell Claude about "
                                              "it in your next message."})

            self._spawn(deliver())
        return touched

    # -- take-off: queued routines, started automatically if the user lets them --------

    @property
    def auto_takeoff(self) -> bool:
        return bool(self.store.get_setting("fd_auto_takeoff", False))

    def set_auto_takeoff(self, on: bool) -> bool:
        self.store.set_setting("fd_auto_takeoff", bool(on))
        self._last_takeoff = 0.0
        if on:
            self.takeoff_tick()
        return bool(on)

    def takeoff_tick(self, every: float = 5.0) -> list[int]:
        """With auto take-off on, start queued routine runs whose gate is clear (not backlog
        ones). Called as the page polls, at most every ``every`` seconds. Returns started session ids."""
        if not self.auto_takeoff or time.time() - self._last_takeoff < every:
            return []
        if not self._takeoff_lock.acquire(blocking=False):
            return []
        try:
            self._last_takeoff = time.time()
            from carrier.readyroom import routines

            started = []
            busy_projects: set[int] = set()
            now = time.time()
            for q in routines.takeoff_queue(self.store):
                if q["backlog"] or q["blockers"] or q["project_id"] in busy_projects:
                    continue
                failed_at = self._takeoff_failed.get(q["task_id"], 0.0)
                if now - failed_at < TAKEOFF_RETRY_SECONDS:
                    continue  # it failed recently: don't pile up failing sessions
                busy_projects.add(q["project_id"])  # one start per project per check
                try:
                    started.append(self.start_task(q["task_id"], auto=True)["id"])
                    self._takeoff_errors.pop(q["task_id"], None)
                    self._takeoff_failed.pop(q["task_id"], None)
                except Exception as exc:  # shown in the take-off list; tried again later
                    self._takeoff_errors[q["task_id"]] = str(exc)
                    self._takeoff_failed[q["task_id"]] = now
            return started
        finally:
            self._takeoff_lock.release()

    def set_policy(self, policy: str) -> str:
        if policy not in POLICIES:
            raise DeckError(f"Unknown policy: {policy}")
        self.policy = policy
        return policy

    def end_session(self, sid: int) -> dict[str, Any]:
        runner = self.runners.pop(sid, None)
        if runner is not None:
            self._run(runner.stop(), timeout=30)
        self.store.update_session(sid, status="ended", ended=time.time())
        self._release_task(self.store.session(sid).get("task_id"))
        self.emit(sid, {"type": "status", "status": "ended"}, persist=False)
        self.emit(sid, {"type": "notice", "kind": "end", "text": "Session ended."})
        return self._session_summary(self.store.session(sid))

    def remove_worktree(self, sid: int, force: bool = False) -> dict[str, Any]:
        s = self.store.session(sid)
        if s is None or not s["worktree"]:
            raise DeckError("This session has no own copy.")
        if sid in self.runners:
            raise DeckError("End the session first.")
        folder = Path(s["cwd"])
        if folder.exists() and worktrees.has_uncommitted(folder) and not force:
            raise DeckError("The copy has uncommitted changes. Commit them (or ask Claude to), "
                            "or remove it anyway.")
        if folder.exists():
            main = gitutil.git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            worktrees.remove(Path(main).parent, folder, force=force)
        self.store.update_session(sid, worktree=0)
        return self._session_summary(self.store.session(sid))

    def bring_back(self, sid: int, action: str) -> dict[str, Any]:
        """Finish with a session's own copy (worktree): "merge" its branch into the main
        folder's branch, "keep" the branch (remove just the copy), or "discard" both."""
        s = self.store.session(sid)
        if s is None or not s.get("branch"):
            raise DeckError("This session has no own copy.")
        if sid in self.runners and self.runners[sid].row["status"] != "ended":
            raise DeckError("End the session first.")
        project = self.store.project(s["project_id"]) if s["project_id"] is not None else None
        if project is None:
            raise DeckError("Only project sessions can be brought back.")
        main = gitutil.repo_root(Path(project["path"]))
        branch = s["branch"]
        copy = Path(s["cwd"])
        if action == "keep":
            if s["worktree"] and copy.exists():
                self.remove_worktree(sid, force=False)
            text = f"Copy removed; the branch {branch} is kept."
        elif action == "discard":
            if s["worktree"] and copy.exists():
                self.remove_worktree(sid, force=True)
            gitutil.git(main, "branch", "-D", branch, check=False)
            self.store.update_session(sid, worktree=0)
            text = f"Copy and branch {branch} deleted."
        elif action == "merge":
            if s["worktree"] and copy.exists() and worktrees.has_uncommitted(copy):
                raise DeckError("The copy has changes that aren't committed. Commit them first (ask Claude to), "
                                "then bring it back.")
            target = gitutil.git(main, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip()
            if not target:
                raise DeckError("The main folder isn't on a branch (detached HEAD). Check out a branch first.")
            gitdir = Path(gitutil.git(main, "rev-parse", "--absolute-git-dir").strip())
            if any((gitdir / n).exists() for n in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")):
                raise DeckError("The main folder is in the middle of a merge or rebase. Finish or abort it first.")
            if gitutil.git(main, "status", "--porcelain", "--untracked-files=no", check=False).strip():
                raise DeckError("The main folder has uncommitted changes. Commit or stash them first, so the merge "
                                "can't mix with them.")
            ok = False
            try:
                out = gitutil.git(main, "merge", "--no-ff", "--no-edit", "-m",
                                  f"Merge Carrier session #{sid}: {s['task']}", branch, check=False)
                # Merged = no merge left open, and the copy's branch is now part of HEAD.
                ok = not (gitdir / "MERGE_HEAD").exists() and subprocess.run(
                    ["git", "merge-base", "--is-ancestor", branch, "HEAD"], cwd=main, capture_output=True).returncode == 0
                if not ok:
                    conflicts = gitutil.git(main, "diff", "--name-only", "--diff-filter=U", check=False).split()
                    reason = (f"The merge has conflicts (in {', '.join(conflicts[:8])})" if conflicts else
                              "Git couldn't merge: " + (out.strip().splitlines() or ["unknown error"])[-1])
                    raise DeckError(reason + ", so nothing was changed. Start a task to resolve it, or merge by hand.")
            finally:
                if not ok and (gitdir / "MERGE_HEAD").exists():
                    gitutil.git(main, "merge", "--abort", check=False)
            if s["worktree"] and copy.exists():
                self.remove_worktree(sid, force=False)
            gitutil.git(main, "branch", "-d", branch, check=False)
            text = f"Merged {branch} into {target}; the copy is removed."
        else:
            raise DeckError(f"Unknown action: {action}")
        self.store.update_session(sid, worktree=0, branch=None if action in ("merge", "discard") else branch)
        self.emit(sid, {"type": "notice", "kind": "copy", "text": text})
        if project:
            self.store.log_project_event(project["id"], "copy", text, session_id=sid)
        return {"text": text, **self._session_summary(self.store.session(sid))}

    def leftover_copies(self, pid: int) -> list[dict[str, Any]]:
        """Sessions of a project whose own copy (or its branch) hasn't been dealt with."""
        return [{"id": s["id"], "task": s["task"], "branch": s["branch"], "status": s["status"],
                 "folder": s["cwd"] if s["worktree"] else ""}
                for s in self.store.sessions(pid) if s.get("branch")]

    # -- reading ---------------------------------------------------------------

    def session(self, sid: int) -> dict[str, Any]:
        s = self.store.session(sid)
        if s is None:
            raise DeckError("Unknown session.")
        runner = self.runners.get(sid)
        pending: list[dict[str, Any]] = []
        if runner:
            async def _snapshot() -> list[dict[str, Any]]:  # read runner state on its own loop
                return runner.pending_list()

            pending = self._run(_snapshot(), timeout=10)
        # seq first, then the transcript: the page skips polled events up to
        # this seq, so nothing is drawn twice and nothing is missed.
        with self._ev_lock:
            seq = self._seq
        task = self.store.task(s["task_id"]) if s.get("task_id") else None
        project = self.store.project(s["project_id"]) if s["project_id"] is not None else None
        return {**self._session_summary(s), "project": project, "task_row": task, "seq": seq,
                "images": self.room.images_allowed(s["project_id"]),
                "transcript": self.store.messages(sid), "pending": pending, "busy": bool(runner and runner.busy),
                "files": self.store.session_files(sid), "awareness": self.awareness(sid)}

    def awareness(self, sid: int) -> dict[str, Any]:
        s = self.store.session(sid)
        if s is None:
            raise DeckError("Unknown session.")
        related = []
        for other in coordination.related_sessions(self.store, s):
            project = self.store.project(other["project_id"])
            related.append({"id": other["id"], "task": other["task"], "status": other["status"],
                            "project": project["name"] if project else "?",
                            "same_project": other["project_id"] == s["project_id"],
                            "files": self.store.session_files(other["id"])[:8]})
        return {"related": related,
                "pending": [e["text"] for e in self.store.pending_events(sid)],
                "recent": [{"text": e["text"], "ts": e["ts"], "delivered": bool(e["delivered"])}
                           for e in self.store.recent_events(sid, limit=10)]}

    def poll(self, since: int = 0) -> dict[str, Any]:
        try:
            self.takeoff_tick()
        except Exception:  # never break polling
            traceback.print_exc()
        with self._ev_lock:
            events = [e for e in self._events if e["seq"] > since]
            seq = self._seq
        return {"seq": seq, "events": events}

    def debrief_target(self, sid: int) -> dict[str, Any]:
        s = self.store.session(sid)
        if s is None:
            raise DeckError("Unknown session.")
        # A session in an own copy (worktree) is reviewed on its own: its
        # changes aren't in the project folder yet. Otherwise review the project.
        return {"path": s["cwd"], "from_ref": s["base_ref"], "task": s["task"],
                "project_id": None if s["worktree"] else s["project_id"]}
