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

from carrier.core import gitutil, usage, usersettings
from carrier.flightdeck import coordination, folderscan, sdk, worktrees
from carrier.readyroom import ownership
from carrier.readyroom import context as rr_context
from carrier.readyroom.manager import ReadyRoom, RoomError
from carrier.core.db import ACTIVE, Store, default_path, folder_kind
from carrier.core import models
from carrier.core.llm import EFFORTS
from carrier.flightdeck.runner import ClientFactory, SessionRunner, default_client_factory

POLICIES = ("warn", "ask", "block", "inform")
MODES = ("default", "acceptEdits", "plan", "bypassPermissions", sdk.LIVE)
MAX_EVENTS = 5000


class DeckError(Exception):
    """A user-facing problem; the message is written for the user."""


class UsageHold(DeckError):
    """Starting this now would run into a usage limit: ask the user first (start_task's ``confirm_usage``)."""


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


MAX_IMAGES = 6  # pasted images per message
TAKEOFF_RETRY_SECONDS = 300  # after a failed auto take-off, wait this long before trying that run again
LIMIT_RETRY_SECONDS = (600, 1200, 2400, 3600)  # usage limit hit with no reset time known: try again after
LIMIT_SLACK_SECONDS = 60  # continue this long after a limit's reset time
CONTINUE_FAILURES = 3  # couldn't continue after a limit this many times in a row: pause and say so
CONTINUE_PROMPT = ("[Carrier] The limit that stopped your last reply has reset. Continue the task from where "
                   "you stopped (your last reply was cut off).")


def _clock(ts: float | None) -> str:
    if not ts:
        return "?"
    d = time.localtime(ts)
    return time.strftime("%H:%M" if ts - time.time() < 20 * 3600 else "%a %H:%M", d)


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
        self._takeoff_hold = ""  # why auto take-off is holding (usage limits), shown in the list
        self._last_usage_tick = 0.0
        self._usage_lock = threading.Lock()
        self._continue_lock = threading.Lock()
        self._refs_scanned: dict[int, float] = {}  # session -> when its reference folders were last scanned
        self._refs_touched: set[int] = set()  # sessions whose reference folders another session edited
        self._continue_failed: dict[int, int] = {}  # session id -> failed continues in a row
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
            if s["status"] == "limited":
                continue  # waiting for a usage limit: continued when it resets (usage_tick), or by the user
            if s["status"] != "paused":
                self.store.update_session(s["id"], status="paused", last_activity=s["last_activity"])

    def close(self) -> None:
        """Disconnect every session (they become "paused", resumable) and stop
        the loop. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        scans: dict[tuple[str, ...], bytes] = {}  # sessions on the same folders share one scan
        deadline = time.time() + 15  # don't hold up quitting for long: later sessions just miss the catch-up
        for runner in list(self.runners.values()):
            if time.time() < deadline:
                self._snapshot_for_wake(runner.sid, scans)  # so it hears what changed while Carrier was closed
            try:
                self._run(runner.stop(), timeout=10)
            except Exception:
                pass
            row = self.store.session(runner.sid)
            if not (row and row["status"] == "limited"):
                self.store.update_session(runner.sid, status="paused")  # paused_at: only a deliberate pause
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

    def _allow_scope(self, session: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]] | None:
        """The session's project pre-approvals and its (readable, editable) folders."""
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        if project is None:
            return None
        editable = [session["cwd"]] + [r for _, r in self.store.project_roots(project)]
        readable = editable + [r for _, r in self.store.project_roots(project, include_reference=True)]
        return self.room.allowances(project["id"]), readable, editable

    def shell_scope(self, session: dict[str, Any]) -> dict[str, Any]:
        """For checking a session's shell commands: the project's pre-approved command
        entries, its folders, and whether reading and editing there are pre-approved."""
        scope = self._allow_scope(session)
        if scope is None:  # a standalone task: its own folder only, nothing pre-approved
            return {"entries": [], "readable": [session["cwd"]], "editable": [session["cwd"]],
                    "can_edit": False, "can_read": False, "no_write": None}
        allow, readable, editable = scope
        return {"entries": sdk.group_entries(allow), "readable": readable, "editable": editable,
                "can_edit": bool(allow.get("edit")), "can_read": bool(allow.get("read")),
                "no_write": self._no_write(session)}

    def file_owner(self, session: dict[str, Any], path: str) -> dict[str, Any]:
        """Whose a file is, for this session's project (readyroom/ownership.classify)."""
        project = self.store.project(session["project_id"]) if session["project_id"] is not None else None
        if project is None:
            return {"kind": "own", "folder": None, "rel": "", "owner": ""}
        path = os.path.expanduser(path)
        if not os.path.isabs(path):  # relative to the session's folder, as Claude Code reads it
            path = os.path.join(session["cwd"], path)
        return ownership.classify(self.store, project, path)

    def _no_write(self, session: dict[str, Any]) -> Any:
        """For shell commands: writing here would change another project's (or the shared space's) files."""
        return lambda p: self.file_owner(session, p)["kind"] in ("foreign", "other")

    def live_check(self, session: dict[str, Any], tool: str, tool_input: dict[str, Any],
                   blocked: str | None = None) -> str:
        """"Danger Zone": "" if the call may run without asking, else why the folder rules ask."""
        scope = self._allow_scope(session)
        readable, editable = ([session["cwd"]], [session["cwd"]]) if scope is None else (scope[1], scope[2])
        return sdk.live_check(tool, tool_input, Path(session["cwd"]), readable, editable,
                              no_write=self._no_write(session) if scope is not None else None, blocked=blocked)

    def preapproved(self, session: dict[str, Any], tool: str, tool_input: dict[str, Any]) -> str:
        """Which of the session's project pre-approvals covers this tool call ("" = ask)."""
        scope = self._allow_scope(session)
        if scope is None:
            return ""
        allow, readable, editable = scope
        if tool in sdk.EDIT_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if path and self.file_owner(session, str(path))["kind"] in ("foreign", "other"):
                return ""  # another project's files: always ask
        return sdk.preapproved(tool, tool_input, allow, Path(session["cwd"]), readable, editable,
                               no_write=self._no_write(session))

    def project_allow(self, session: dict[str, Any], tool: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
        """For a permission card: what "always allow in this project" would add, and for a
        shell command which of its programs are pre-approved already."""
        scope = self._allow_scope(session)
        if scope is None:
            return None
        allow, readable, editable = scope
        if tool in sdk.EDIT_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if path and self.file_owner(session, str(path))["kind"] in ("foreign", "other"):
                return None  # can't be pre-approved: another project's files
        return sdk.project_allow(tool, tool_input, allow, Path(session["cwd"]), readable, editable,
                                 no_write=self._no_write(session))

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

    # Files in a workspace folder a session may change (after asking) though the rest is read only:
    # Claude Code's instruction files, which apply to every project in the workspace.
    WORKSPACE_EDITABLE = ("CLAUDE.md", "CLAUDE.local.md")

    def workspace_instructions(self, session: dict[str, Any], file_path: str) -> dict[str, Any] | None:
        """The workspace folder, when ``file_path`` is one of its instruction files (``WORKSPACE_EDITABLE``)
        in an existing folder of it — and not in another project's main folder."""
        if os.path.basename(file_path) not in self.WORKSPACE_EDITABLE:
            return None
        claim = self.workspace_target(session, file_path)
        if claim is None or claim[1] is not None:
            return None
        if self.file_owner(session, file_path)["kind"] == "foreign":
            return None
        return claim[0]

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
                "takeoff_hold": self._hold_reason("takeoff") or self._hold_reason("routines"),
                "usage": usage.brief(self.store),
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
            "agents_working": bool((r := self.runners.get(s["id"])) and r.agents_since is not None),
            "task_id": s.get("task_id"), "policy": s.get("policy"),
            "limit_until": s.get("limit_until"),
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
                   effort: str | None = None, auto: bool = False, confirm_usage: bool = False) -> dict[str, Any]:
        """Start a Flight Deck session working on a task (project or standalone).

        Settings come from, in order: explicit arguments, the chosen preset,
        the project's default preset, then Claude Code's defaults. Raises
        :class:`UsageHold` when a usage limit is reached (or, for a routine, close)
        unless ``confirm_usage``.
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
        waits = [l for l in self.store.task_links(task_id) if l["kind"] == "waits_on" and l["direction"] == "out"
                 and l["other_status"] in ("planned", "active")]
        if waits and auto:  # started on its own: leave it until what it waits on is done
            w = waits[0]
            raise DeckError(f"This task waits on {w['other_project_name'] or 'standalone'} #{w['other_id']} "
                            f"“{w['other_title']}”, which isn't done yet.")
        for other in self.store.sessions(active_only=True):
            if other.get("task_id") == task_id:  # finish what's on the plate first
                raise DeckError(f"This task already has a session ({other['status'].replace('_', ' ')}). "
                                "Resume it, or end it to start a new one.")
        if not confirm_usage:
            lv = usage.level(self.store, usage.settings())
            if lv["state"] == "reached":
                raise UsageHold(f"{lv['text']} Claude can't work until then; "
                                "the task would wait and continue after the reset.")
            if task.get("routine_id") and (why := self._hold_reason("routines")):
                raise UsageHold(f"{why} You asked Carrier to hold routines this close to a limit.")
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
        if (auto or routine) and mode in ("bypassPermissions", sdk.LIVE):
            mode = "default"  # a routine (and anything started on its own) never skips asking
        if mode == sdk.LIVE:  # only ever turned on with the phrase, in a running session
            mode = "default"
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
        for w in waits:  # started by hand: say what it waits on
            self.emit(sid, {"type": "notice", "kind": "waiting",
                            "text": f"This task waits on {w['other_project_name'] or 'standalone'} #{w['other_id']} "
                                    f"“{w['other_title']}”, which isn't done yet."})
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
                     if s.get("task_id") == task_id and (s["id"] in self.runners or s["status"] in ("paused", "limited"))]
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
            if s.get("task_id") == task_id and s["status"] in ("paused", "limited") and s.get("paused_at"):
                raise DeckError("The task's session is paused. End it first (you can leave a note).")
        for s in self.store.sessions(active_only=True):
            if s.get("task_id") == task_id:
                self.store.update_session(s["id"], status="ended", ended=time.time())
                self.store.drop_manifest(s["id"])
                self.emit(s["id"], {"type": "status", "status": "ended"})
        self._release_task(task_id)

    def resume_session(self, sid: int) -> dict[str, Any]:
        s = self.store.session(sid)
        if s is None:
            raise DeckError("Unknown session.")
        old = self.runners.get(sid)
        if old is not None:
            dead = old._reader is None or old._reader.done() or s["status"] == "error"
            if not dead:
                return self._session_summary(s)
            # Its connection to Claude Code is gone (a message too big to read, a crash): reconnect.
            self.runners.pop(sid, None)
            try:
                self._run(old.stop(), timeout=15)
            except Exception:
                pass
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
        try:
            self._wake_up(sid)
        except Exception as exc:  # the session still works; it just isn't told
            self.emit(sid, {"type": "notice", "kind": "wake", "text": f"Couldn't check what changed while paused: {exc}"})
        self.store.update_session(sid, paused_at=None)
        return self._session_summary(self.store.session(sid))

    # -- pausing: asleep, then told what changed -----------------------------------------

    def _wake_roots(self, row: dict[str, Any]) -> list[str]:
        """Folders whose changes a paused session hears about when it resumes."""
        roots = [row["cwd"]]
        project = self.store.project(row["project_id"]) if row["project_id"] is not None else None
        if project:
            # A session in its own copy works there, not in the main folder: skip the main folder.
            roots += [r for label, r in self.store.project_roots(project) if not (row.get("worktree") and label == "")]
            roots += self._ref_roots(project)
        return list(dict.fromkeys(roots))

    def _ref_roots(self, project: dict[str, Any]) -> list[str]:
        """The reference and workspace folders this project's sessions hear about (each folder's
        own setting, or the project's switch)."""
        work = {os.path.normcase(r) for _, r in self.store.project_roots(project)}
        return [r for r in self.room.watched_refs(project["id"]) if os.path.normcase(r) not in work]

    def _snapshot_for_wake(self, sid: int, scans: dict[tuple[str, ...], bytes] | None = None) -> None:
        row = self.store.session(sid)
        if row is None:
            return
        try:
            roots = self._wake_roots(row)
            key = tuple(roots)
            if scans is not None and key in scans:
                blob = scans[key]
            else:
                blob = folderscan.pack(folderscan.manifest(roots))
                if scans is not None:
                    scans[key] = blob
            self.store.save_manifest(sid, "wake", blob)
        except Exception:
            traceback.print_exc()

    def pause_session(self, sid: int) -> dict[str, Any]:
        """Put a session to sleep: disconnect it (Resume carries on the same conversation), keep its
        task, and take note of its folders so that on waking Claude hears what changed meanwhile.
        Other sessions stop treating it as active."""
        row = self.store.session(sid)
        if row is None:
            raise DeckError("Unknown session.")
        if row["status"] in ("ended", "paused"):
            raise DeckError("This session is already " + ("ended." if row["status"] == "ended" else "paused."))
        runner = self.runners.get(sid)
        if runner is not None:
            # Take the session first, so no message (yours, a limit continue, a task change) can
            # start between this check and stopping it.
            with runner._claim_lock:
                if runner.busy:
                    raise DeckError("Claude is working. Wait for it, or press Stop, then pause.")
                runner.busy = True
            self.runners.pop(sid, None)
        self._snapshot_for_wake(sid)
        if runner is not None:
            try:
                self._run(runner.stop(), timeout=30)
            except Exception:
                traceback.print_exc()
        self.store.update_session(sid, status="paused", paused_at=time.time(), limit_until=None)
        self.emit(sid, {"type": "status", "status": "paused"}, persist=False)
        self.emit(sid, {"type": "notice", "kind": "pause",
                        "text": "Paused. Other sessions no longer treat this one as active. Press Resume to carry "
                                "on: Claude will be told what changed in its folders meanwhile."})
        return self._session_summary(self.store.session(sid))

    def _wake_up(self, sid: int) -> None:
        old = folderscan.unpack(self.store.manifest(sid, "wake"))
        self.store.drop_manifest(sid, "wake")
        row = self.store.session(sid)
        if not old or row is None:
            return
        new = folderscan.manifest(old["roots"])
        # This note covers what changed while it slept; the usual briefing picks up from now on
        # (otherwise the same edits would be reported twice).
        self.store.update_session(sid, last_briefed=time.time())
        project = self.store.project(row["project_id"]) if row["project_id"] is not None else None
        if project and self.store.manifest(sid, "refs") is not None:
            refs = self._ref_roots(project)
            if refs and all(r in old["roots"] for r in refs):  # covered here: don't report them twice
                self.store.save_manifest(sid, "refs", folderscan.pack(folderscan.manifest(refs)))
        d = folderscan.diff(old, new)
        n = len(d["changed"]) + len(d["added"]) + len(d["removed"])
        if not n:
            self.emit(sid, {"type": "notice", "kind": "wake", "text": "Nothing changed in its folders while paused."})
            return
        text = self._change_note(d, old["roots"], old["taken"],
                                 f"While this session was paused ({coordination.ago(old['taken'])}), "
                                 "these files in its folders changed")
        self.store.add_event(sid, None, "while_away", text)
        self.emit(sid, {"type": "notice", "kind": "wake",
                        "text": f"While paused, {n} file{'s' if n != 1 else ''} changed in its folders. "
                                "Claude will be told with your next message."})

    MAX_CHANGE_LINES = 40

    def _change_note(self, d: dict[str, Any], roots: list[str], since: float, head: str) -> str:
        """A note for Claude listing changed files, and who changed them where Carrier knows."""
        who = self._editors_since(since, roots)
        lines = []
        for kind, paths in (("changed", d["changed"]), ("new", d["added"]), ("deleted", d["removed"])):
            for p in paths:
                by = who.get(p)
                lines.append(f"  - {self._show_path(p, roots)} ({kind}" + (f", by {by})" if by else ")"))
        more = len(lines) - self.MAX_CHANGE_LINES
        lines = lines[:self.MAX_CHANGE_LINES] + ([f"  - … and {more} more"] if more > 0 else [])
        if d.get("truncated"):
            lines.append("  - (Some folders are too big to check completely.)")
        return (f"{head}:\n" + "\n".join(lines) +
                "\n  Re-read any of these before relying on what you remember of them, and before editing them.")

    @staticmethod
    def _show_path(p: str, roots: list[str]) -> str:
        for r in sorted(roots, key=len, reverse=True):
            rn = os.path.normcase(r).rstrip(os.sep)
            if p.startswith(rn + os.sep):
                rel = p[len(rn) + 1:].replace(os.sep, "/")
                return rel if r == roots[0] else f"{Path(r).name}/{rel}"
        return p

    def _editors_since(self, since: float, roots: list[str]) -> dict[str, str]:
        """Changed file (normcase absolute path) -> which Flight Deck session edited it."""
        out: dict[str, str] = {}
        sessions: dict[int, dict[str, Any]] = {}
        watched = {os.path.normcase(r) for r in roots}
        for e in self.store.edits_since(since):  # newest first: the latest editor wins
            project = self.store.project(e["project_id"])
            if project is None:
                continue
            if e["session_id"] not in sessions:
                sessions[e["session_id"]] = self.store.session(e["session_id"]) or {}
            s = sessions[e["session_id"]]
            if s.get("worktree") and os.path.normcase(s.get("cwd", "")) not in watched:
                continue  # made in that session's own copy, not in these folders
            who = coordination._label(self.store, s) if s else f"session #{e['session_id']}"
            path = e["path"]
            for label, root in self.store.project_roots(project):
                if label == "":  # the main folder: paths as they are (or the copy's own folder)
                    base = s["cwd"] if s.get("worktree") else root
                    full = os.path.join(base, path.replace("/", os.sep))
                elif path.startswith(label + "/"):  # an extra folder logs "<label>/<path>"
                    full = os.path.join(root, path[len(label) + 1:].replace("/", os.sep))
                else:
                    continue
                out.setdefault(os.path.normcase(full), who)
                break
        return out

    REFS_RESCAN_SECONDS = 60

    def refs_check(self, sid: int) -> None:
        """Before a message: if the project watches its reference folders, note what changed there
        since the last message (any source) for Claude. Runs off the event loop."""
        row = self.store.session(sid)
        project = self.store.project(row["project_id"]) if row and row["project_id"] is not None else None
        if project is None:
            return
        roots = self._ref_roots(project)
        if not roots:
            return
        now = time.time()
        if sid not in self._refs_touched and now - self._refs_scanned.get(sid, 0) < self.REFS_RESCAN_SECONDS:
            return  # scanned moments ago and no session has edited there since: changes keep until next time
        self._refs_touched.discard(sid)
        self._refs_scanned[sid] = now
        new = folderscan.manifest(roots)
        old = folderscan.unpack(self.store.manifest(sid, "refs"))
        self.store.save_manifest(sid, "refs", folderscan.pack(new))
        if not old or old.get("roots") != new["roots"]:
            return  # the first look: nothing to compare with yet
        d = folderscan.diff(old, new)
        if d["changed"] or d["added"] or d["removed"]:
            self.store.add_event(sid, None, "refs_changed",
                                 self._change_note(d, [project["path"]] + roots, old["taken"],
                                                   "Since your last message, these files changed in this project's "
                                                   "reference folders (the projects it depends on)"))

    def notify_ref_watchers(self, source: dict[str, Any], path: str) -> None:
        """Another session edited ``path``: tell live sessions watching it as a reference folder."""
        p = os.path.normcase(os.path.abspath(path))
        for sid, runner in list(self.runners.items()):
            if sid == source["id"]:
                continue
            row = runner.row
            project = self.store.project(row["project_id"]) if row["project_id"] is not None else None
            if project is None:
                continue
            roots = self._ref_roots(project)
            if any(p == os.path.normcase(r) or p.startswith(os.path.normcase(r).rstrip(os.sep) + os.sep) for r in roots):
                self._refs_touched.add(sid)  # look again with the next message
                roots = [project["path"]] + roots  # so the folder's name is shown
                self.emit(sid, {"type": "notice", "kind": "refs_changed",
                                "text": f"{coordination._cap(coordination._label(self.store, source))} changed "
                                        f"{self._show_path(p, roots)} in a reference folder. Claude will be told "
                                        "with your next message."})

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
        if row and row.get("limit_until"):
            self.store.update_session(sid, limit_until=None)  # the user carried on themselves
        self._spawn(runner.send(text or "(see the attached image)", images=clean))
        return True

    def interrupt(self, sid: int) -> bool:
        self._run(self._runner(sid).interrupt(), timeout=30)
        return True

    def answer(self, sid: int, req_id: str, decision: str, message: Any = "") -> bool:
        if decision not in ("allow", "allow_always", "allow_project", "deny", "answers", "skip", "work", "change", "decline"):
            raise DeckError(f"Unknown decision: {decision}")
        runner = self._runner(sid)

        async def _answer() -> bool:  # resolve the future on the deck's own loop
            return runner.answer(req_id, decision, message)

        if not self._run(_answer(), timeout=10):
            raise DeckError("That request is no longer waiting for an answer.")
        return True

    def set_mode(self, sid: int, mode: str, confirm: str | None = None) -> dict[str, Any]:
        if mode not in MODES:
            raise DeckError(f"Unknown permission mode: {mode}")
        typed = " ".join((confirm or "").split()).lower()
        if mode == sdk.LIVE and typed != sdk.LIVE_PHRASE.lower():
            raise DeckError(f"To enter the Danger Zone, type “{sdk.LIVE_PHRASE}”.")
        if mode == sdk.FIRE and typed != sdk.FIRE_PHRASE.lower():
            raise DeckError(f"To set the world on fire, type “{sdk.FIRE_PHRASE}”.")
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

            self._takeoff_hold = self._hold_reason("takeoff") or self._hold_reason("routines")
            if self._takeoff_hold:
                return []  # close to (or at) a usage limit: nothing starts on its own
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

    # -- usage limits: hold automatic starts, wait out a limit, continue after it ---------

    def _hold_reason(self, what: str) -> str:
        try:
            return usage.hold_reason(self.store, what)
        except Exception:  # a broken settings file must not stop the deck
            traceback.print_exc()
            return ""

    def on_rate_limit(self, sid: int, ev: dict[str, Any]) -> None:
        """Claude Code reported a limit window's state (from a session's stream)."""
        state, prev = usage.record_limit(self.store, ev)
        status = state["status"]
        if status == prev:
            return
        label = usage.WINDOWS.get(str(ev.get("kind")), ("usage limit", 0))[0].lower()
        pct = state["utilization"]
        pct_text = f" ({pct * 100:.0f}% used)" if pct is not None else ""
        when = f" It resets at {_clock(state['resets_at'])}." if state["resets_at"] else ""
        if status == "allowed_warning":
            text = f"Claude Code reports you're close to your {label}{pct_text}.{when}"
        elif status == "rejected" and state.get("overage_status") in ("allowed", "allowed_warning"):
            text = f"Your {label} is reached; Claude carries on with extra usage (billed).{when}"
        elif status == "rejected":
            text = f"Your {label} is reached.{when}"
        else:
            return
        self.emit(sid, {"type": "notice", "kind": "usage", "text": text})

    def limit_hit(self, sid: int, text: str = "") -> None:
        """A turn stopped at a usage limit: wait for the reset, then continue (usage_tick)."""
        row = self.store.session(sid)
        if row is None:
            return
        now = time.time()
        tries = int(row.get("limit_tries") or 0) + 1
        until = usage.reached(self.store, now) or usage.reset_from_text(text)
        if until and until > now:
            until += LIMIT_SLACK_SECONDS
        else:  # no reset time known: try again later, waiting longer each time
            until = now + LIMIT_RETRY_SECONDS[min(tries, len(LIMIT_RETRY_SECONDS)) - 1]
        self.store.update_session(sid, status="limited", limit_until=until, limit_tries=tries)
        self.emit(sid, {"type": "status", "status": "limited"}, persist=False)
        auto = usage.settings()["usage_auto_resume"]
        what = "An API rate limit" if usage.plan(self.store) == "api" else "Claude's usage limit"
        self.emit(sid, {"type": "notice", "kind": "limit", "until": until,
                        "text": f"{what} stopped this task. "
                                + (f"Carrier will continue it at {_clock(until)}." if auto else
                                   f"It can go on after {_clock(until)}: press Continue now then.")})

    def usage_tick(self, every: float = 15.0) -> list[int]:
        """Continue sessions whose usage limit has reset. Called as the page polls."""
        now = time.time()
        if now - self._last_usage_tick < every or not self._usage_lock.acquire(blocking=False):
            return []
        try:
            self._last_usage_tick = now
            continued = []
            auto = usage.settings()["usage_auto_resume"]
            for row in self.store.sessions(active_only=True):
                until = row.get("limit_until")
                if row["status"] != "limited" or not until or until > now:
                    continue
                sid = row["id"]
                still = usage.reached(self.store, now)
                if still and still > now:  # another window (e.g. weekly) is still reached
                    self.store.update_session(sid, limit_until=still + LIMIT_SLACK_SECONDS)
                    continue
                if not auto:  # stays "limited" (Continue now is the way on), no longer timed
                    self.store.update_session(sid, limit_until=None)
                    self.emit(sid, {"type": "status", "status": "limited"}, persist=False)
                    self.emit(sid, {"type": "notice", "kind": "limit",
                                    "text": "The limit has reset. Press Continue now to carry on."})
                    continue
                try:
                    if self.continue_session(sid, scheduled=True) is not None:
                        continued.append(sid)
                    self._continue_failed.pop(sid, None)
                except Exception as exc:  # e.g. the folder is gone: try again later, say why; give up after a few
                    fails = self._continue_failed.get(sid, 0) + 1
                    self._continue_failed[sid] = fails
                    if fails >= CONTINUE_FAILURES:
                        self._continue_failed.pop(sid, None)
                        self.store.update_session(sid, status="paused", limit_until=None)
                        self.emit(sid, {"type": "status", "status": "paused"}, persist=False)
                        self.emit(sid, {"type": "notice", "kind": "limit",
                                        "text": f"Couldn't continue after the limit: {exc}. Stopped trying; "
                                                "fix the problem, then press Resume."})
                    else:
                        self.store.update_session(sid, limit_until=now + LIMIT_RETRY_SECONDS[0])
                        self.emit(sid, {"type": "notice", "kind": "limit",
                                        "text": f"Couldn't continue after the limit: {exc}. Trying again later."})
            return continued
        finally:
            self._usage_lock.release()

    def continue_session(self, sid: int, scheduled: bool = False) -> dict[str, Any] | None:
        """Carry on after a usage limit (the Continue now button, or ``scheduled`` by
        :meth:`usage_tick`): tell Claude to go on. Scheduled, it returns None if the
        session was already continued (e.g. the user pressed the button meanwhile)."""
        with self._continue_lock:  # the button and the scheduled continue can't both go
            row = self.store.session(sid)
            if row is None:
                raise DeckError("Unknown session.")
            if row["status"] == "ended":
                raise DeckError("This session has ended.")
            if scheduled and row["status"] != "limited":
                return None
            if sid not in self.runners:
                self.resume_session(sid)
            runner = self._runner(sid)
            if not runner.claim():
                if scheduled:
                    return None
                raise DeckError("Claude is still working.")
            self.store.update_session(sid, limit_until=None)
            self._spawn(runner.send(CONTINUE_PROMPT))
            return self._session_summary(self.store.session(sid))

    def set_policy(self, policy: str) -> str:
        if policy not in POLICIES:
            raise DeckError(f"Unknown policy: {policy}")
        self.policy = policy
        return policy

    HANDOFF_PROMPT = ("[Carrier] The user is ending this session; the task isn't finished. Write a short handoff "
                      "note for the next session that picks up this task: what's done, what's left, decisions "
                      "made, and anything to watch out for (files, commands, gotchas). Markdown bullets, under "
                      "200 words. Reply with the note only; it is given to the next session on this task.")

    def end_with_note(self, sid: int) -> dict[str, Any]:
        """Ask Claude for a handoff note, save it to the task, then end the session."""
        row = self.store.session(sid)
        if row is None or not row.get("task_id"):
            raise DeckError("This session has no task to leave a note on.")
        runner = self._runner(sid)
        if not runner.claim():
            raise DeckError("Claude is still working. Wait for it, or press Stop.")

        def finished(text: str) -> None:  # on the event loop: ending waits for it, so end from a thread
            threading.Thread(target=self._end_quietly, args=(sid, text), daemon=True).start()

        def failed() -> None:
            current = self.store.session(sid)
            if current and current["status"] == "ended":
                return  # ended some other way meanwhile
            self.emit(sid, {"type": "notice", "kind": "handoff",
                            "text": "No note was written, so the session is still open. Try again, or end it "
                                    "with a note of your own."})

        self.emit(sid, {"type": "notice", "kind": "handoff", "text": "Asking Claude for a handoff note, then ending…"})
        self._spawn(runner.send(self.HANDOFF_PROMPT, on_done=finished, on_fail=failed))
        return {"ending": True}

    def _end_quietly(self, sid: int, handoff: str) -> None:
        try:
            self.end_session(sid, handoff=handoff, by="Claude")
        except Exception:
            traceback.print_exc()

    def end_session(self, sid: int, handoff: str | None = None, by: str = "you") -> dict[str, Any]:
        """End for good. ``handoff``: a note for the next session on the task (None = keep the old one)."""
        row = self.store.session(sid)
        if handoff is not None and row and row.get("task_id"):
            self.room.set_handoff(row["task_id"], handoff, by=by, session_id=sid)
        runner = self.runners.pop(sid, None)
        if runner is not None:
            self._run(runner.stop(), timeout=30)
        self.store.update_session(sid, status="ended", ended=time.time(), paused_at=None)
        self.store.drop_manifest(sid)
        self._release_task(self.store.session(sid).get("task_id"))
        self.emit(sid, {"type": "status", "status": "ended"}, persist=False)
        self.emit(sid, {"type": "notice", "kind": "end",
                        "text": "Session ended." + (" Its note is saved for the next session on this task."
                                                    if handoff and handoff.strip() else "")})
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
        # seq before anything else is read: the page skips polled events up to this seq, so state
        # read here must be at least as new (a status change in between is then polled again).
        with self._ev_lock:
            seq = self._seq
        s = self.store.session(sid)
        if s is None:
            raise DeckError("Unknown session.")
        runner = self.runners.get(sid)
        pending: list[dict[str, Any]] = []
        if runner:
            async def _snapshot() -> list[dict[str, Any]]:  # read runner state on its own loop
                return runner.pending_list()

            pending = self._run(_snapshot(), timeout=10)
        task = self.store.task(s["task_id"]) if s.get("task_id") else None
        project = self.store.project(s["project_id"]) if s["project_id"] is not None else None
        return {**self._session_summary(s), "project": project, "task_row": task, "seq": seq,
                "images": self.room.images_allowed(s["project_id"]),
                "transcript": self.store.messages(sid), "pending": pending, "busy": bool(runner and runner.busy),
                "last_activity": runner.last_activity if runner else None,
                "background": bool(runner and runner._bg_active),
                "agents_working": bool(runner and runner.agents_since is not None),
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
        for tick in (self.usage_tick, self.takeoff_tick):
            try:
                tick()
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
