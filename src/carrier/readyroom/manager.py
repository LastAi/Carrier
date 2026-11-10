# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""ReadyRoom — the API for setting projects up.

Everything a project *is* lives here: its folder, long-term goal, rules,
tags, task queue and history, links to other projects, plus the reusable
presets (rule sets, session presets, project templates) and the global
rules. Flight Deck reads this to run sessions; it never edits it except to
move tasks through their lifecycle.

Like the other front-end APIs, every method takes and returns plain JSON
values and raises :class:`RoomError` with a message meant for the user.
No background threads are needed: the store is thread-safe.

Task lifecycle
--------------
``planned`` → ``active`` (a Flight Deck session is working on it) →
``done`` (summary written) — or ``dropped``. A project's *current task* is
the first planned/active task in queue order.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from carrier.core.db import ACTIVE, FOLDER_KINDS, Store, default_path, folder_kind
from carrier.core.llm import EFFORTS
from carrier.flightdeck.sdk import GROUPS
from carrier.readyroom import context, ownership

OPEN = ("planned", "active")
MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
POLICIES = ("warn", "ask", "block", "inform")
MAX_TAG_LEN = 30


GROUP_PRIORITIES = ("high", "normal", "low", "backlog")


def _diff_lines(old: str, new: str) -> str:
    """A short +/- list of the lines that changed between two texts (for the activity log)."""
    a = [ln for ln in old.splitlines() if ln.strip()]
    b = [ln for ln in new.splitlines() if ln.strip()]
    gone = [f"- {ln}" for ln in a if ln not in b]
    came = [f"+ {ln}" for ln in b if ln not in a]
    return "\n".join(gone + came)


class RoomError(Exception):
    """A user-facing problem; the message is written for the user."""


def clean_tags(tags: list[str] | str | None) -> list[str]:
    """Trim, de-duplicate (case-insensitive) and keep tags short."""
    if isinstance(tags, str):
        tags = tags.split(",")
    out: list[str] = []
    for t in tags or []:
        t = re.sub(r"\s+", " ", str(t)).strip()[:MAX_TAG_LEN]
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def clean_globs(globs: list[str] | str | None) -> list[str]:
    if isinstance(globs, str):
        globs = globs.split(",")
    return [g.strip() for g in globs or [] if g and g.strip()]


_NORMALIZED: set[str] = set()  # stores whose folders have been checked against the ownership rules


class ReadyRoom:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store(default_path())
        key = str(getattr(self.store, "path", id(self.store)))
        if key not in _NORMALIZED:  # once per database: folders saved before the ownership rules
            _NORMALIZED.add(key)
            try:
                self.normalize_folders()
            except Exception:  # never stop the app over this
                import traceback

                traceback.print_exc()

    def normalize_folders(self) -> list[str]:
        """Main folders are their project's alone; folders projects work in together are shared
        spaces (readyroom/ownership.py)."""
        return ownership.normalize(self.store, log=lambda pid, msg: self.store.log_project_event(pid, "folder", msg))

    def _main_of_other(self, pid: int, path: str) -> dict[str, Any] | None:
        """Another project whose main folder is ``path`` or contains it."""
        return next((q for q in self.store.projects() if q["id"] != pid and ownership._inside(path, q["path"])), None)

    # -- helpers ---------------------------------------------------------------

    def _project(self, pid: int) -> dict[str, Any]:
        p = self.store.project(pid)
        if p is None:
            raise RoomError("Unknown project.")
        return p

    def _task(self, tid: int) -> dict[str, Any]:
        t = self.store.task(tid)
        if t is None:
            raise RoomError("Unknown task.")
        return t

    def queue(self, pid: int, group_id: int | None = None) -> list[dict[str, Any]]:
        """Open tasks of one queue: the main one (``group_id=None``) or a task group's."""
        return [t for t in self.store.tasks(pid, statuses=OPEN) if t.get("group_id") == group_id]

    def current_task(self, pid: int, group_id: int | None = None) -> dict[str, Any] | None:
        q = self.queue(pid, group_id)
        return q[0] if q else None

    def _queue_name(self, pid: int, gid: int | None) -> str:
        g = self.store.task_group(gid) if gid is not None else None
        return f"task group {g['name']}" if g else "main queue"

    # -- reshaping work (proposed by Claude in a session, approved by the user) -----------

    def place_task(self, pid: int, title: str, details: str, *, group_id: int | None = None,
                   new_group: dict[str, Any] | None = None, position: str = "end", by: str = "you",
                   note: str = "", session_id: int | None = None) -> dict[str, Any]:
        """Add a task where the user chose: a queue (main, a group, or a new group) and a
        position ("next", "end", or "after:<task id>"). Returns the new task."""
        if new_group:
            name = str(new_group.get("name", "")).strip()
            self.save_group(pid, name, str(new_group.get("priority", "normal")), new_group.get("tags"))
            group_id = max(g["id"] for g in self.store.task_groups(pid) if g["name"] == name)
        self.add_task(pid, title, details, first=(position == "next"), group_id=group_id, by=by, note=note,
                      session_id=session_id)
        new = max((t for t in self.queue(pid, group_id) if t["title"] == title.strip()), key=lambda t: t["id"])
        if position.startswith("after:"):
            after = int(position.split(":", 1)[1])
            ids = [t["id"] for t in self.queue(pid, group_id) if t["id"] != new["id"]]
            if after in ids:
                ids.insert(ids.index(after) + 1, new["id"])
                for n, task_id in enumerate(ids, start=1):
                    self.store.update_task(task_id, position=n)
        return self.store.task(new["id"])

    def add_to_task(self, tid: int, title: str, details: str, *, by: str = "claude", note: str = "",
                    session_id: int | None = None) -> dict[str, Any]:
        """Widen a task's scope with new work (logged, so reviews see why it grew)."""
        t = self._task(tid)
        part = f"### Added: {title.strip()}\n{details.strip()}".strip()
        new_details = (t["details"].rstrip() + "\n\n" + part).strip()
        self.store.update_task(tid, details=new_details)
        self._log(t, "scope_added", {"details": new_details}, by, note or f"Added “{title.strip()}”", session_id)
        return self.store.task(tid)

    def merge_tasks(self, into: int, source: int, *, part: str = "", remaining: str | None = None,
                    by: str = "claude", note: str = "", session_id: int | None = None) -> dict[str, Any]:
        """Pull another task — whole, or the ``part`` of it given — into task ``into``.

        Whole: the other task is closed as merged. Part: its details become
        ``remaining``. Both tasks' logs record it, so nothing silently disappears.
        """
        a, b = self._task(into), self._task(source)
        if a["id"] == b["id"]:
            raise RoomError("Pick a different task to pull in.")
        if a["project_id"] != b["project_id"]:
            raise RoomError("Tasks can only be combined within one project.")
        if a["status"] not in OPEN or b["status"] not in OPEN:
            raise RoomError("Only open tasks can be merged.")
        if b["status"] == "active" and self._active_sessions(task_id=b["id"]):
            raise RoomError(f"A session is working on “{b['title']}”. End it first.")
        whole = not part.strip()
        taken = (b["details"] if whole else part).strip()
        block = f"### From task “{b['title']}”" + ("" if whole else " (part)") + (f"\n{taken}" if taken else "")
        new_details = (a["details"].rstrip() + "\n\n" + block).strip()
        self.store.update_task(a["id"], details=new_details)
        self._log(a, "merged_in", {"details": new_details}, by, note or f"Pulled in “{b['title']}”"
                  + ("" if whole else " (part)"), session_id)
        if whole:
            self._unblock_waiters(b, f"was merged into “{a['title']}” (#{a['id']})")
            summary = f"Merged into task “{a['title']}” (#{a['id']})."
            self.store.update_task(b["id"], status="dropped", summary=summary, finished=time.time())
            self._log(b, "merged_away", {"status": "dropped"}, by, note or summary, session_id)
        else:
            rest = (remaining if remaining is not None else b["details"]).strip()
            self.store.update_task(b["id"], details=rest)
            self._log(b, "merged_away", {"details": rest}, by,
                      note or f"Part moved to “{a['title']}” (#{a['id']})", session_id)
        return self.store.task(a["id"])

    def split_task(self, tid: int, keep: str, title: str, details: str, *, group_id: int | None = None,
                   new_group: dict[str, Any] | None = None, position: str = "next", by: str = "claude",
                   note: str = "", session_id: int | None = None) -> dict[str, Any]:
        """Move part of a task out into a new task: ``keep`` is what stays."""
        t = self._task(tid)
        if t["project_id"] is None:
            raise RoomError("Standalone tasks can't be split.")
        if not keep.strip():
            raise RoomError("Say what stays in the task.")
        self.store.update_task(tid, details=keep.strip())
        self._log(t, "split_out", {"details": keep.strip()}, by, note or f"Split out “{title.strip()}”", session_id)
        return self.place_task(t["project_id"], title, details, group_id=group_id, new_group=new_group,
                               position=position, by=by, note=f"Split from “{t['title']}” (#{tid})",
                               session_id=session_id)

    # -- images in sessions ---------------------------------------------------------------

    def images_allowed(self, pid: int | None) -> bool:
        """Whether this project's sessions may use images (pasted, shown, or opened by Claude)."""
        return pid is None or bool(self.store.get_setting(f"project:{pid}:images", True))

    def set_images_allowed(self, pid: int, on: bool) -> dict[str, Any]:
        self._project(pid)
        self.store.set_setting(f"project:{pid}:images", bool(on))
        self.store.log_project_event(pid, "images", "turned images " + ("on" if on else "off"))
        return self.project(pid)

    # -- watching reference folders --------------------------------------------------------

    def watch_refs(self, pid: int | None) -> bool:
        """Tell this project's sessions what changed in its reference and workspace folders,
        with each message and after a pause (off by default)."""
        return bool(self.store.get_setting(f"project:{pid}:watch_refs", False)) if pid is not None else False

    def watched_refs(self, pid: int | None) -> list[str]:
        """The reference and workspace folders whose changes this project's sessions hear about:
        each folder's own setting, or the project's switch for folders without one."""
        p = self.store.project(pid) if pid is not None else None
        if p is None:
            return []
        default = self.watch_refs(pid)
        return [f["path"] for f in p.get("folders") or [] if folder_kind(f) in ("reference", "workspace")
                and (f["watch"] if isinstance(f.get("watch"), bool) else default)]

    def set_watch_refs(self, pid: int, on: bool) -> dict[str, Any]:
        self._project(pid)
        self.store.set_setting(f"project:{pid}:watch_refs", bool(on))
        if not on:  # forget the old look, so turning it back on doesn't report a backlog of changes
            for s in self.store.sessions(pid):
                self.store.drop_manifest(s["id"], "refs")
        self.store.log_project_event(pid, "watch_refs",
                                     "sessions " + ("now hear" if on else "no longer hear")
                                     + " about changes in reference and workspace folders")
        return self.project(pid)

    def set_handoff(self, tid: int, text: str, by: str = "you", session_id: int | None = None) -> dict[str, Any]:
        """Save (or clear) the note for the next session on a task."""
        t = self._task(tid)
        text = (text or "").strip()
        self.store.update_task(tid, handoff=text)
        self.store.log_task_edit(tid, "handoff", {"handoff": t.get("handoff", "")}, {"handoff": text},
                                 by=by, session_id=session_id, project_id=t["project_id"])
        return self.store.task(tid)

    # -- pre-approved actions -------------------------------------------------------------

    def allowances(self, pid: int | None) -> dict[str, Any]:
        """What this project's sessions may do without asking (see flightdeck/sdk.preapproved)."""
        from carrier.flightdeck.sdk import ALLOW_DEFAULT

        from carrier.flightdeck.sdk import GROUPS
        from carrier.flightdeck.shellcheck import valid_entries

        saved = self.store.get_setting(f"project:{pid}:allow", {}) if pid is not None else {}
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in ALLOW_DEFAULT.items()}
        for k, v in (saved or {}).items():
            if k in out and isinstance(v, type(out[k])):
                out[k] = v
        out["commands"] = valid_entries(out["commands"])  # e.g. "bash" saved by 0.8.0: never valid alone
        out["groups"] = [g for g in out["groups"] if g in GROUPS]
        return out

    def allow_suggestions(self, pid: int) -> list[str]:
        """Program groups that fit what's in the project's main folder (Java files -> Java, ...)."""
        from carrier.flightdeck.sdk import GROUPS

        folder = Path(self._project(pid)["path"])
        have = self.allowances(pid)["groups"]
        found: set[str] = set()
        markers = {"build.gradle": ("gradle", "java"), "build.gradle.kts": ("gradle", "java"),
                   "settings.gradle": ("gradle",), "gradlew": ("gradle",), "pom.xml": ("maven", "java"),
                   "package.json": ("node",), "pyproject.toml": ("python",), "requirements.txt": ("python",),
                   "setup.py": ("python",), "Cargo.toml": ("rust",), "go.mod": ("go",), ".git": ("git_read",)}
        for name, groups in markers.items():
            if (folder / name).exists():
                found.update(groups)
        seen = 0
        try:
            for root, dirs, files in os.walk(folder):  # a quick look for sources, a few folders deep
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "build", "out",
                                                                                  "target", "dist", ".venv")]
                if root[len(str(folder)):].count(os.sep) >= 3:
                    dirs[:] = []
                for f in files:
                    ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                    found.update({"java": ("java",), "kt": ("java",), "csproj": ("dotnet",), "sln": ("dotnet",),
                                  "py": ("python",), "rs": ("rust",), "go": ("go",)}.get(ext, ()))
                seen += 1
                if seen > 400:
                    break
        except OSError:
            pass
        return [g for g in GROUPS if g in found and g not in have]

    def set_allowances(self, pid: int, **changes: Any) -> dict[str, Any]:
        """Change pre-approvals: booleans for read / edit / web_search / web_fetch / export,
        lists (or comma-separated text) for sites / commands / tools / groups. Then shares
        them with Claude Code (readyroom/claude_settings.py) if the project says so."""
        from carrier.flightdeck.sdk import ALLOW_DEFAULT, GROUPS
        from carrier.flightdeck.shellcheck import parse_entry

        self._project(pid)
        cur = self.allowances(pid)
        notes = []
        for k, v in changes.items():
            if k not in ALLOW_DEFAULT:
                raise RoomError(f"Unknown setting: {k}")
            if isinstance(ALLOW_DEFAULT[k], bool):
                v = bool(v)
            else:
                items = v.split(",") if isinstance(v, str) else list(v or [])
                v = []
                for x in (str(i).strip() for i in items):
                    if x and x not in v:
                        v.append(x)
                if k == "commands":
                    for x in v:
                        try:
                            parse_entry(x)
                        except ValueError as exc:
                            raise RoomError(str(exc)) from None
                if k == "groups" and any(x not in GROUPS for x in v):
                    raise RoomError("Unknown program group.")
            if cur[k] != v:
                notes.append(f"{k.replace('_', ' ')}: {v if not isinstance(v, bool) else ('on' if v else 'off')}")
            cur[k] = v
        self.store.set_setting(f"project:{pid}:allow", cur)
        if notes:
            self.store.log_project_event(pid, "allow", "pre-approved actions — " + "; ".join(notes))
            # Running sessions read this at their start: tell them with the next message.
            text = context.permissions_section(self.store, pid).replace(
                "## Permissions in this project", "The user changed what runs without asking in this project. Now:", 1)
            for s in self.store.sessions(pid, active_only=True):
                self.store.add_event(s["id"], None, "allow_changed", text)
        self.sync_claude_settings(pid)
        return self.project(pid)

    def sync_claude_settings(self, pid: int) -> str:
        """Write the project's pre-approvals to Claude Code's settings.local.json (see
        claude_settings.py). Returns a problem to show, or ""; never raises."""
        from carrier.readyroom import claude_settings

        p = self._project(pid)
        owned = self.store.get_setting(f"project:{pid}:claude_rules", []) or []
        try:
            now = claude_settings.sync(p["path"], self.allowances(pid), owned)
        except claude_settings.SettingsFileError as exc:
            self.store.set_setting(f"project:{pid}:claude_rules_error", str(exc))
            return str(exc)
        except Exception as exc:  # e.g. git missing: the Carrier list still works
            self.store.set_setting(f"project:{pid}:claude_rules_error", f"Couldn't update Claude Code's settings: {exc}")
            return str(exc)
        self.store.set_setting(f"project:{pid}:claude_rules", now)
        self.store.set_setting(f"project:{pid}:claude_rules_error", "")
        return ""

    def add_allowance(self, pid: int, change: dict[str, Any]) -> dict[str, Any]:
        """Add to the pre-approvals (lists are extended, not replaced): "always allow in this project"."""
        cur = self.allowances(pid)
        merged = {k: (cur[k] + [x for x in v if x not in cur[k]]) if isinstance(v, list) else v
                  for k, v in change.items()}
        return self.set_allowances(pid, **merged)

    # -- task groups ---------------------------------------------------------------------

    def save_group(self, pid: int, name: str, priority: str = "normal", tags: list[str] | str | None = None,
                   gid: int | None = None) -> dict[str, Any]:
        """A task group: its own queue of tasks (work in parallel with the main queue), a
        priority ("backlog" = parked ideas, never started automatically) and tags."""
        self._project(pid)
        if not name.strip():
            raise RoomError("A task group needs a name.")
        if priority not in GROUP_PRIORITIES:
            raise RoomError(f"Unknown priority: {priority}")
        if gid is not None and (g := self.store.task_group(gid)) is not None and g["project_id"] != pid:
            raise RoomError("That group belongs to another project.")
        self.store.save_task_group(pid, gid, name=name.strip(), priority=priority, tags=clean_tags(tags))
        self.store.log_project_event(pid, "group", ("changed" if gid else "created") + f" the task group {name.strip()}",
                                     detail=f"priority {priority}")
        return self.project(pid)

    def delete_group(self, pid: int, gid: int) -> dict[str, Any]:
        g = self.store.task_group(gid)
        if g is None or g["project_id"] != pid:
            raise RoomError("Unknown task group.")
        self.store.delete_task_group(gid)  # its tasks join the main queue
        self.store.log_project_event(pid, "group", f"deleted the task group {g['name']}",
                                     detail="Its tasks moved to the main queue.")
        return self.project(pid)

    def drop_task(self, tid: int, why: str, *, by: str = "you", note: str = "") -> dict[str, Any]:
        """Close an open task without doing it (stale, a duplicate, no longer wanted). Kept in
        the history with the reason, unlike deleting."""
        t = self._task(tid)
        if t["status"] not in OPEN:
            raise RoomError("Only open tasks can be dropped.")
        if self._active_sessions(task_id=tid):
            raise RoomError(f"A session is working on “{t['title']}”. End it first.")
        why = why.strip() or "No longer needed."
        self.store.update_task(tid, status="dropped", summary=f"Dropped: {why}", finished=time.time())
        self._log(t, "dropped", {"status": "dropped"}, by, note or why)
        self._unblock_waiters(t, "was dropped")
        return self.store.task(tid)

    def _unblock_waiters(self, t: dict[str, Any], what: str) -> None:
        """Tasks waiting on ``t`` can go ahead: note it in their history."""
        for l in self.store.task_links(t["id"]):
            if l["kind"] == "waits_on" and l["direction"] == "in" and l["other_status"] in OPEN:
                self._log(self.store.task(l["other_id"]), "unblocked", {}, "carrier",
                          f"“{t['title']}” (#{t['id']}) in {self._project_name(t['project_id'])} {what}: "
                          "this task can go ahead.")

    def reorder_queue(self, pid: int, gid: int | None, ids: list[int]) -> dict[str, Any]:
        """Put a queue's open tasks in this order (tasks not named keep their order, after them)."""
        tasks = self.queue(pid, gid)
        queue = [t["id"] for t in tasks]
        wanted = [int(i) for i in ids if int(i) in queue]
        if len(set(wanted)) != len(wanted):
            raise RoomError("A task is named twice.")
        order = wanted + [i for i in queue if i not in wanted]
        # Tasks a session is working on stay in front: re-ordering never changes a running session's task.
        running = [t["id"] for t in tasks if self._active_sessions(task_id=t["id"])]
        order = [i for i in order if i in running] + [i for i in order if i not in running]
        for n, task_id in enumerate(order, start=1):
            self.store.update_task(task_id, position=n)
        return self.project(pid)

    def _project_name(self, pid: int | None) -> str:
        p = self.store.project(pid) if pid is not None else None
        return p["name"] if p else "standalone work"

    def _link_view(self, tid: int) -> list[dict[str, Any]]:
        return [{"kind": l["kind"], "direction": l["direction"], "task_id": l["other_id"], "title": l["other_title"],
                 "status": l["other_status"], "project_id": l["other_project"],
                 "project": l["other_project_name"] or "standalone"} for l in self.store.task_links(tid)]

    def linked_projects(self, pid: int) -> list[dict[str, Any]]:
        return context.linked_projects(self.store, pid)

    def linked_project(self, pid: int, which: Any) -> dict[str, Any]:
        """A project linked to ``pid``, by id or name; a clear error otherwise."""
        linked = self.linked_projects(pid)
        w = str(which or "").strip()
        for q in linked:
            if w == str(q["id"]) or w.lower() == q["name"].lower():
                return q
        names = ", ".join(q["name"] for q in linked) or "none"
        raise RoomError(f"“{w}” isn't linked to this project (linked projects: {names}). Ask the user to link "
                        "them in the Ready Room (Linked projects) if work there is needed.")

    def link_tasks(self, from_task: int, to_task: int, kind: str = "opened", session_id: int | None = None) -> None:
        if kind not in ("opened", "waits_on"):
            raise RoomError("Unknown link.")
        a, b = self._task(from_task), self._task(to_task)
        if a["id"] == b["id"]:
            raise RoomError("A task can't link to itself.")
        self.store.add_task_link(a["id"], b["id"], kind, session_id)

    def waiting_on(self, tid: int) -> list[dict[str, Any]]:
        """Open tasks this task waits for."""
        return [l for l in self.store.task_links(tid)
                if l["kind"] == "waits_on" and l["direction"] == "out" and l["other_status"] in OPEN]

    def set_task_group(self, tid: int, gid: int | None) -> dict[str, Any]:
        """Move a task to another queue (to the end of it)."""
        t = self._task(tid)
        pid = t["project_id"]
        if pid is None:
            raise RoomError("Standalone tasks have no queue.")
        if gid is not None and ((g := self.store.task_group(gid)) is None or g["project_id"] != pid):
            raise RoomError("Unknown task group.")
        last = max([x["position"] for x in self.queue(pid, gid)] or [0])
        self.store.update_task(tid, group_id=gid, position=last + 1)
        self._log(t, "moved", {"group_id": gid}, note=self._queue_name(pid, gid))
        return self.project(pid)

    def _active_sessions(self, pid: int | None = None, task_id: int | None = None) -> list[dict[str, Any]]:
        rows = self.store.sessions(pid, active_only=True) if pid is not None else self.store.sessions(active_only=True)
        return [s for s in rows if task_id is None or s.get("task_id") == task_id]

    # -- overview ----------------------------------------------------------------

    def overview(self, search: str = "", tag: str | None = None) -> dict[str, Any]:
        """Projects (filtered by text and/or tag) plus everything the editors need."""
        q = search.strip().lower()
        projects = []
        all_tags: dict[str, str] = {}
        for p in self.store.projects():
            for t in p["tags"]:
                all_tags.setdefault(t.lower(), t)
            hay = " ".join([p["name"], p["path"], p["goal"], " ".join(p["tags"])]).lower()
            if q and q not in hay:
                continue
            if tag and tag.lower() not in (t.lower() for t in p["tags"]):
                continue
            current = self.current_task(p["id"])
            projects.append({
                "id": p["id"], "name": p["name"], "path": p["path"], "tags": p["tags"],
                "goal": p["goal"], "folder_exists": Path(p["path"]).is_dir(),
                "current_task": current["title"] if current else None,
                "planned": len(self.store.tasks(p["id"], statuses=("planned",))),
                "active_sessions": len(self._active_sessions(p["id"])),
            })
        return {
            "projects": projects,
            "tags": sorted(all_tags.values(), key=str.lower),
            "rule_sets": self.store.rule_sets(),
            "presets": self.store.presets(),
            "templates": self.store.templates(),
            "global": {"rules": self.store.get_setting("global_rules", ""),
                       "rule_set_ids": self.store.get_setting("global_rule_sets", [])},
        }

    def project(self, pid: int) -> dict[str, Any]:
        p = self._project(pid)
        names = {x["id"]: x["name"] for x in self.store.projects()}
        links = []
        for l in self.store.links(pid):
            mine_a = l["project_a"] == pid
            links.append({"id": l["id"], "note": l["note"],
                          "other_id": l["project_b"] if mine_a else l["project_a"],
                          "other": names.get(l["project_b"] if mine_a else l["project_a"]),
                          "mine": l["contracts_a"] if mine_a else l["contracts_b"],
                          "theirs": l["contracts_b"] if mine_a else l["contracts_a"]})
        foreign = ownership.foreign_mains(self.store, p)
        watched = set(self.watched_refs(pid))
        return {**p, "folder_exists": Path(p["path"]).is_dir(), "main_shared_with": self.shared_with(pid, p["path"]),
                "folders": [{**f, "kind": folder_kind(f), "exists": Path(f["path"]).is_dir(),
                             "review": "skip" if f.get("review") == "skip" else "all", "note": f.get("note", ""),
                             "share": f.get("share", "own") if folder_kind(f) == "work" else None,
                             "part": f.get("part") or [],
                             "claimed": len(ownership.claimed(self.store, pid, f["path"])),
                             "shared_with": self.shared_with(pid, f["path"]),
                             "foreign": [q["name"] for q in foreign if ownership._inside(q["path"], f["path"])],
                             "watch_own": f["watch"] if isinstance(f.get("watch"), bool) else None,
                             "watched": f["path"] in watched}
                            for f in p["folders"]],
                "rule_set_ids": self.store.project_rule_set_ids(pid),
                "images": self.images_allowed(pid),
                "watch_refs": self.watch_refs(pid),
                "allow": self.allowances(pid),
                "allow_groups": [{"id": k, **v} for k, v in GROUPS.items()],
                "allow_suggest": self.allow_suggestions(pid),
                "allow_export_error": self.store.get_setting(f"project:{pid}:claude_rules_error", "") or "",
                "tasks": [{**t, "links": self._link_view(t["id"])} for t in self.store.tasks(pid, statuses=OPEN)],
                "groups": self.store.task_groups(pid),
                "history": self.store.tasks(pid, statuses=("done",)),
                "links": links,
                "active_sessions": len(self._active_sessions(pid))}

    # -- projects ------------------------------------------------------------------

    def create_project(self, name: str, path: str, goal: str = "", first_task: str = "",
                       tags: list[str] | str | None = None, rules: str = "",
                       rule_set_ids: list[int] | None = None, preset_id: int | None = None,
                       template_id: int | None = None, first_task_details: str = "") -> dict[str, Any]:
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise RoomError(f"Folder not found: {path}")
        folder = folder.resolve()
        if self.store.project_by_path(str(folder)):
            raise RoomError("That folder is already a project's main folder.")
        for other in self.store.projects():
            # Nested main folders would review (and edit) each other's files.
            o = Path(other["path"])
            if o in folder.parents or folder in o.parents:
                raise RoomError(f"That folder is inside (or contains) the main folder of {other['name']}. "
                                "Use it as a work or reference folder of that project instead.")
        extras: dict[str, Any] = {}
        if template_id is not None:  # the template fills anything left empty
            t = self.store.template(template_id)
            if t is None:
                raise RoomError("Unknown template.")
            extras = t.get("extras") or {}
            goal = goal or t["goal"]
            rules = rules or t["rules"]
            first_task = first_task or t["first_task"]
            tags = clean_tags(tags) or t["tags"]
            rule_set_ids = rule_set_ids or t["rule_set_ids"]
            preset_id = preset_id or t["preset_id"]
        name = (name or folder.name).strip()
        if preset_id is not None and self.store.preset(preset_id) is None:
            preset_id = None  # the template's preset was deleted
        pid = self.store.add_project(name, str(folder))
        self.store.log_project_event(pid, "project", "created the project", detail=str(folder))
        try:
            self.store.update_project(pid, goal=goal.strip(), rules=rules.strip(), tags=clean_tags(tags),
                                      preset_id=preset_id)
            if rule_set_ids:
                self.store.set_project_rule_sets(pid, [int(i) for i in rule_set_ids])
            if extras:
                self._apply_template_extras(pid, extras)
            if first_task.strip():
                details = (first_task_details or "").strip()
                tid = self.store.add_task(first_task.strip(), pid, **({"details": details} if details else {}))
                self.store.log_task_edit(tid, "created", {}, {"title": first_task.strip(), "details": details},
                                         project_id=pid)
        except Exception:
            self.store.remove_project(pid)  # don't leave a half-made project behind
            raise
        # Its main folder is its own now: another project working inside it only reads it from here on.
        self.normalize_folders()
        return self.project(pid)

    def update_project(self, pid: int, **fields: Any) -> dict[str, Any]:
        self._project(pid)
        if "name" in fields and not str(fields["name"]).strip():
            raise RoomError("A project needs a name.")
        if "path" in fields:
            folder = Path(str(fields["path"])).expanduser()
            if not folder.is_dir():
                raise RoomError(f"Folder not found: {fields['path']}")
            if self._active_sessions(pid):
                raise RoomError("End this project's sessions before moving its folder.")
            other = self.store.project_by_path(str(folder.resolve()))
            if other and other["id"] != pid:
                raise RoomError("That folder is already another project.")
            if any(folder.resolve() == Path(f["path"]) for f in self._project(pid)["folders"]):
                raise RoomError("That folder is already one of this project's extra folders.")
            fields["path"] = str(folder.resolve())
        if "tags" in fields:
            fields["tags"] = clean_tags(fields["tags"])
        for key in ("name", "goal", "rules"):
            if key in fields:
                fields[key] = str(fields[key]).strip()
        rule_set_ids = fields.pop("rule_set_ids", None)
        before = self._project(pid)
        self.store.update_project(pid, **fields)
        if "path" in fields and fields["path"] != before.get("path"):
            self.normalize_folders()  # other projects' work folders in this new main folder become reference
        for key, label in (("name", "name"), ("path", "main folder"), ("goal", "goal"), ("rules", "rules"),
                           ("tags", "tags"), ("preset_id", "default preset")):
            if key in fields and fields[key] != before.get(key):
                old, new = before.get(key), fields[key]
                detail = (f"{old} → {new}" if key in ("name", "path", "tags", "preset_id")
                          else _diff_lines(str(old or ""), str(new or "")))
                self.store.log_project_event(pid, "project", f"changed the {label}", detail=detail)
        if rule_set_ids is not None:
            old_ids = self.store.project_rule_set_ids(pid)
            self.store.set_project_rule_sets(pid, [int(i) for i in rule_set_ids])
            if sorted(old_ids) != sorted(int(i) for i in rule_set_ids):
                names = {r["id"]: r["name"] for r in self.store.rule_sets()}
                self.store.log_project_event(pid, "rule", "changed the rule sets", detail=", ".join(
                    names.get(int(i), "?") for i in rule_set_ids) or "(none)")
        return self.project(pid)

    # -- extra folders -----------------------------------------------------------------

    def add_folder(self, pid: int, path: str, label: str = "", kind: str = "work") -> dict[str, Any]:
        """Add an extra folder to a project.

        Claude Code still runs in the main folder and is given access to the
        extra folders too:

        * **work** — Claude may read and edit it; Debrief reviews it with the
          main folder. It may be a whole git repository or a folder inside
          one (then only that folder is reviewed).
        * **reference** — read only: Claude can look things up there; edits are
          blocked and it isn't reviewed.
        * **workspace** — a shared parent folder (e.g. a modding workspace
          holding several projects). Its contents are read only, but when the
          project creates a *new* folder there, that folder is added to the
          project as a work folder. The project's other folders may sit inside it.

        A folder can be another project's main folder (as work or reference);
        only main folders are exclusive. Files in an extra folder are named
        ``<label>/<path>`` (label defaults to the folder's name).
        """
        if kind not in FOLDER_KINDS:
            raise RoomError(f"Unknown folder type: {kind}")
        p = self._project(pid)
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise RoomError(f"Folder not found: {path}")
        folder = folder.resolve()
        if kind == "work" and (owner := self._main_of_other(pid, str(folder))):
            raise RoomError(f"That's {owner['name']}'s main folder (or inside it), which belongs to that project "
                            "alone. Add it as a reference folder to read it.")
        own = [Path(r).resolve() for _, r in self.store.project_roots(p, include_reference=True)]
        if folder in own:
            raise RoomError("That folder is already part of this project.")
        # A workspace folder is a shared parent: the project's other folders
        # (its main folder too) may sit inside it, and it may hold new ones.
        workspaces = [Path(f["path"]).resolve() for f in p["folders"] if folder_kind(f) == "workspace"]
        if kind == "workspace":
            if any(r in folder.parents and r not in workspaces for r in own):
                raise RoomError("That folder is inside one of this project's folders.")
        elif any((r in folder.parents and r not in workspaces) or folder in r.parents for r in own):
            raise RoomError("That folder is inside (or contains) one of this project's folders.")
        label = re.sub(r"[^\w.-]+", "-", (label or folder.name).strip()).strip("-") or "folder"
        # Files in the extra folder are named "<label>/…", so the label must not
        # also be a top-level name in the main folder (or two files would share a name).
        taken = {lbl.lower() for lbl, _ in self.store.project_roots(p, include_reference=True)}
        try:
            taken |= {child.name.lower() for child in Path(p["path"]).iterdir()}
        except OSError:
            pass
        base, n = label, 2
        while label.lower() in taken:
            label, n = f"{base}-{n}", n + 1
        self.store.update_project(pid, folders=[*p["folders"], {"path": str(folder), "label": label,
                                                                "kind": kind}])
        self.store.log_project_event(pid, "folder", f"added the {kind} folder {label}", detail=str(folder))
        self.normalize_folders()  # another project working there too: a shared space
        return self.project(pid)

    def set_folder_kind(self, pid: int, path: str, kind: str) -> dict[str, Any]:
        """Switch an extra folder between work and reference."""
        if kind not in FOLDER_KINDS:
            raise RoomError(f"Unknown folder type: {kind}")
        p = self._project(pid)
        if self._active_sessions(pid):
            raise RoomError("End this project's sessions before changing its folders.")
        if not any(f["path"] == path for f in p["folders"]):
            raise RoomError("That folder isn't part of this project.")
        if kind != "workspace":
            here = Path(path).resolve()
            inner = [lbl or "the main folder" for lbl, r in self.store.project_roots(p, include_reference=True)
                     if here in Path(r).resolve().parents]
            if inner:
                raise RoomError(f"{', '.join(inner)} is inside this folder, so it can only be a workspace folder.")
        if kind == "work" and (owner := self._main_of_other(pid, path)):
            raise RoomError(f"That's {owner['name']}'s main folder (or inside it), which belongs to that project "
                            "alone: it can only be a reference folder here.")
        self.store.update_project(pid, folders=[{**f, "kind": kind} if f["path"] == path else f
                                                for f in p["folders"]])
        self.store.log_project_event(pid, "folder", f"made {path} a {kind} folder")
        self.normalize_folders()
        return self.project(pid)

    def set_folder_options(self, pid: int, path: str, share: str | None = None,
                           part: list[str] | str | None = None, review: str | None = None,
                           note: str | None = None, watch: bool | str | None = None) -> dict[str, Any]:
        """A work folder's ownership and review settings (readyroom/ownership.py):

        * ``share`` — "own" (everything in it is this project's) or "shared" (a shared space:
          the project owns its ``part`` and the files it creates there);
        * ``part`` — patterns for this project's part of a shared space (``ships/**``);
        * ``review`` — "all" (the default) or "skip" (left out of reviews and scans);
        * ``note`` — what the folder is, for Debrief's review agents;
        * ``watch`` — for reference and workspace folders: whether sessions hear about its changes
          (True / False, or "default" to follow the project's switch).
        Sessions may be running; Flight Deck applies changes from their next action."""
        p = self._project(pid)
        f0 = next((f for f in p["folders"] if f["path"] == path), None)
        if f0 is None:
            raise RoomError("That folder isn't part of this project.")
        if share is not None and share not in ownership.SHARES:
            raise RoomError("Unknown sharing setting.")
        if review is not None and review not in ("all", "skip"):
            raise RoomError("Unknown review setting.")
        if share == "own" and (others := self.shared_with(pid, path, work_only=True)):
            raise RoomError(f"{', '.join(others)} also works in this folder, so it stays a shared space. Give this "
                            "project its part of it instead.")
        if part is not None:
            try:
                part = ownership.clean_patterns(part)
            except ValueError as exc:
                raise RoomError(str(exc)) from None
        notes = []
        f = dict(f0)
        if watch is not None:
            if folder_kind(f) not in ("reference", "workspace"):
                raise RoomError("Only reference and workspace folders are watched.")
            if watch == "default":
                if "watch" in f:
                    f.pop("watch")
                    notes.append("changes reported as the project's switch says")
            elif bool(watch) != f.get("watch"):
                f["watch"] = bool(watch)
                notes.append("sessions " + ("hear" if watch else "don't hear") + " about its changes")
        for key, value, say in (("share", share, lambda v: "this project's" if v == "own" else "a shared space"),
                                ("part", part, lambda v: "this project's part: " + (", ".join(v) or "(none)")),
                                ("review", review, lambda v: "reviewed" if v == "all" else "left out of reviews"),
                                ("note", note.strip()[:2000] if note is not None else None,
                                 lambda v: "note for reviews: " + (v or "(none)"))):
            if value is not None and value != f.get(key, {"share": "own", "review": "all", "part": [], "note": ""}[key]):
                f[key] = value
                notes.append(say(value))
        self.store.update_project(pid, folders=[f if g["path"] == path else g for g in p["folders"]])
        if notes:
            self.store.log_project_event(pid, "folder", f"{f['label']}: " + "; ".join(notes))
        return self.project(pid)

    def shared_with(self, pid: int, path: str, work_only: bool = False) -> list[str]:
        """Other projects that also use this folder (as their main folder or an extra folder;
        ``work_only``: as a work folder)."""
        here = os.path.normcase(os.path.realpath(path))
        out = []
        for q in self.store.projects():
            if q["id"] == pid:
                continue
            roots = [os.path.normcase(os.path.realpath(r)) for _, r in self.store.project_roots(q, include_reference=not work_only)]
            if work_only:
                roots = roots[1:]  # their extra work folders, not their main folder
            if any(here == r or here.startswith(r.rstrip(os.sep) + os.sep) or r.startswith(here.rstrip(os.sep) + os.sep)
                   for r in roots):
                out.append(q["name"])
        return out

    def remove_folder(self, pid: int, path: str) -> dict[str, Any]:
        p = self._project(pid)
        if self._active_sessions(pid):
            raise RoomError("End this project's sessions before changing its folders.")
        self.store.update_project(pid, folders=[f for f in p["folders"] if f["path"] != path])
        self.store.log_project_event(pid, "folder", "removed a folder", detail=path)
        return self.project(pid)

    def remove_project(self, pid: int) -> dict[str, Any]:
        self._project(pid)
        if self._active_sessions(pid):
            raise RoomError("End this project's sessions first.")
        for t in self.store.tasks(pid, statuses=OPEN):
            self._unblock_waiters(t, "went with its project, which was removed")
        self.store.remove_project(pid)
        return self.overview()

    # -- tasks ------------------------------------------------------------------------

    def add_task(self, pid: int, title: str, details: str = "", first: bool = False,
                 **links: Any) -> dict[str, Any]:
        """``links``: ``routine_id`` (a routine's run) or ``fix_review`` (Debrief's fix list)."""
        self._project(pid)
        title = title.strip()
        if not title:
            raise RoomError("A task needs a title.")
        extra = {k: v for k, v in links.items() if k in ("routine_id", "fix_review", "group_id") and v is not None}
        gid = extra.get("group_id")
        if gid is not None and ((g := self.store.task_group(gid)) is None or g["project_id"] != pid):
            raise RoomError("Unknown task group.")
        new_id = self.store.add_task(title, pid, details=details.strip(), **extra)
        self.store.log_task_edit(new_id, "created", {}, {"title": title, "details": details.strip()},
                                 by=links.get("by", "routine" if extra.get("routine_id") else
                                              "debrief" if extra.get("fix_review") else "you"),
                                 note=links.get("note", ""), session_id=links.get("session_id"), project_id=pid)
        if first:
            # "First" means next up in its queue: after any task a session is already
            # working on, so the current task doesn't change under a running session.
            queue = [x["id"] for x in self.queue(pid, gid) if x["id"] != new_id]
            active = {x["id"] for x in self.store.tasks(pid, statuses=("active",))}
            k = 0
            while k < len(queue) and queue[k] in active:
                k += 1
            queue.insert(k, new_id)
            for n, task_id in enumerate(queue, start=1):
                self.store.update_task(task_id, position=n)
        return self.project(pid)

    def _log(self, t: dict[str, Any], kind: str, after: dict[str, Any], by: str = "you", note: str = "",
             session_id: int | None = None) -> None:
        before = {k: t.get(k) for k in after}
        if kind == "edited" and before == after:
            return
        self.store.log_task_edit(t["id"], kind, before, after, by=by, note=note, session_id=session_id,
                                 project_id=t["project_id"])

    def update_task(self, tid: int, title: str | None = None, details: str | None = None, by: str = "you",
                    note: str = "", session_id: int | None = None, kind: str = "edited") -> dict[str, Any]:
        t = self._task(tid)
        fields: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise RoomError("A task needs a title.")
            fields["title"] = title.strip()
        if details is not None:
            fields["details"] = details.strip()
        self.store.update_task(tid, **fields)
        self._log(t, kind, fields, by, note, session_id)
        return self.project(t["project_id"]) if t["project_id"] is not None else self._task(tid)

    def project_log(self, pid: int) -> list[dict[str, Any]]:
        """Everything recorded for the project, newest first."""
        self._project(pid)
        return self.store.project_log(pid)

    def task_history(self, tid: int) -> list[dict[str, Any]]:
        """Every recorded change to a task (oldest first)."""
        return self.store.task_edits(tid)

    def delete_task(self, tid: int) -> dict[str, Any]:
        t = self._task(tid)
        if self._active_sessions(task_id=tid):
            raise RoomError("A session is working on this task. End it first.")
        self._log(t, "deleted", {"status": "deleted"})
        self.store.delete_task(tid)
        return self.project(t["project_id"]) if t["project_id"] is not None else {"deleted": tid}

    def move_task(self, tid: int, direction: str) -> dict[str, Any]:
        """Move a task up/down in its project's queue, or to the top ("current")."""
        t = self._task(tid)
        pid = t["project_id"]
        if pid is None:
            raise RoomError("Standalone tasks have no queue.")
        queue = self.queue(pid, t.get("group_id"))
        ids = [x["id"] for x in queue]
        if tid not in ids:
            raise RoomError("Only open tasks can be moved. Reopen it first.")
        i = ids.index(tid)
        if direction == "top":
            ids.insert(0, ids.pop(i))
        elif direction == "up" and i > 0:
            ids[i - 1], ids[i] = ids[i], ids[i - 1]
        elif direction == "down" and i < len(ids) - 1:
            ids[i + 1], ids[i] = ids[i], ids[i + 1]
        for n, task_id in enumerate(ids, start=1):
            self.store.update_task(task_id, position=n)
        return self.project(pid)

    def finish_task(self, tid: int, summary: str = "") -> dict[str, Any]:
        t = self._task(tid)
        self.store.update_task(tid, status="done", summary=summary.strip(), finished=time.time())
        self._log(t, "status", {"status": "done"}, note="Finished")
        self._unblock_waiters(t, "is done")
        if t["project_id"] is not None:
            from carrier.readyroom import routines as _r

            _r.invalidate(t["project_id"])
        if t["project_id"] is not None:
            from carrier.readyroom import routines

            if not t.get("routine_id"):
                routines.after_task(self, t["project_id"], t.get("group_id"))  # e.g. "update the docs"
            gid = t.get("group_id")
            if gid is not None and not self.queue(t["project_id"], gid):
                routines.group_done(self, t["project_id"], gid)  # the group's last task is done
        return self._task(tid) if t["project_id"] is None else self.project(t["project_id"])

    def reopen_task(self, tid: int) -> dict[str, Any]:
        t = self._task(tid)
        self.store.update_task(tid, status="planned", finished=None)
        self._log(t, "status", {"status": "planned"}, note="Reopened")
        return self.project(t["project_id"]) if t["project_id"] is not None else self._task(tid)

    # -- standalone tasks -------------------------------------------------------------------

    def standalone_tasks(self, include_done: bool = True) -> list[dict[str, Any]]:
        statuses = ("planned", "active", "done") if include_done else OPEN
        return self.store.tasks(None, statuses=statuses)

    def upgrade_standalone(self, tid: int, name: str, goal: str = "", tags: list[str] | str | None = None,
                           template_id: int | None = None) -> dict[str, Any]:
        """Turn a standalone task into a new project (its folder becomes the project's)."""
        t = self._task(tid)
        if t["project_id"] is not None:
            raise RoomError("This task already belongs to a project.")
        if not t["cwd"]:
            raise RoomError("This task has no folder.")
        if self._active_sessions(task_id=tid):
            raise RoomError("End the task's session first.")
        project = self.create_project(name or Path(t["cwd"]).name, t["cwd"], goal=goal, tags=tags,
                                      template_id=template_id)
        self.store.update_task(tid, project_id=project["id"])
        for s in self.store.sessions(include_ended_hours=24 * 365):
            if s.get("task_id") == tid:
                self.store.update_session(s["id"], project_id=project["id"], last_activity=s["last_activity"])
        return self.project(project["id"])

    def assign_standalone(self, tid: int, pid: int, add_merge_task: bool = True) -> dict[str, Any]:
        """Record a finished standalone task as part of a project's history.

        Files are not moved. With ``add_merge_task`` the project gets an
        "Integrate …" task at the front of its queue that says where the
        files are, so Claude can bring them in during a normal session.
        """
        t = self._task(tid)
        self._project(pid)
        if t["project_id"] is not None:
            raise RoomError("This task already belongs to a project.")
        if t["status"] != "done":
            raise RoomError("Finish the task first (Mark done), then assign it.")
        if self._active_sessions(task_id=tid):
            raise RoomError("End the task's session first.")
        self.store.update_task(tid, project_id=pid, origin="standalone", origin_path=t["cwd"])
        if add_merge_task:
            details = (f"Work done earlier as a standalone task “{t['title']}” lives in {t['cwd']}. "
                       "Bring the relevant parts into this project.")
            if t["summary"].strip():
                details += f"\nIts summary: {t['summary'].strip()}"
            self.add_task(pid, f"Integrate standalone work: {t['title']}", details, first=True)
        return self.project(pid)

    # -- links ---------------------------------------------------------------------------

    def add_link(self, a: int, b: int, note: str = "", contracts_a: list[str] | str | None = None,
                 contracts_b: list[str] | str | None = None) -> dict[str, Any]:
        if a == b:
            raise RoomError("Pick two different projects to link.")
        self._project(a), self._project(b)
        self.store.add_link(a, b, note.strip(), clean_globs(contracts_a), clean_globs(contracts_b))
        return self.project(a)

    def remove_link(self, link_id: int, pid: int) -> dict[str, Any]:
        self.store.remove_link(link_id)
        return self.project(pid)

    # -- rules, presets, templates ----------------------------------------------------------

    def set_global_rules(self, rules: str, rule_set_ids: list[int] | None = None) -> dict[str, Any]:
        self.store.set_setting("global_rules", rules.strip())
        if rule_set_ids is not None:
            self.store.set_setting("global_rule_sets", [int(i) for i in rule_set_ids])
        return self.overview()["global"]

    def save_rule_set(self, name: str, body: str, rid: int | None = None) -> list[dict[str, Any]]:
        if not name.strip():
            raise RoomError("A rule set needs a name.")
        if rid is None:
            self.store.add_rule_set(name.strip(), body.strip())
        else:
            self.store.update_rule_set(rid, name.strip(), body.strip())
        return self.store.rule_sets()

    def apply_rule(self, text: str, target: str, pid: int | None = None, rule_set_id: int | None = None,
                   new_rule_set: str = "") -> str:
        """Add an approved rule (proposed by Claude, edited by the user) where the user chose.

        ``target``: "project" (that project's rules), "global", or "rule_set"
        (an existing set, or a new one named ``new_rule_set`` that is attached
        to the project). Returns a short description of what changed.
        """
        text = text.strip()
        if not text:
            raise RoomError("The rule is empty.")

        def joined(old: str) -> str:
            return (old.rstrip() + "\n" + text).strip() if old.strip() else text

        if target == "project":
            p = self._project(int(pid)) if pid is not None else None
            if p is None:
                raise RoomError("This session has no project; add the rule to global rules or a rule set.")
            self.store.update_project(p["id"], rules=joined(p["rules"]))
            self.store.log_project_event(p["id"], "rule", "added a rule Claude suggested", by="claude", detail=text)
            return f"Added to {p['name']}'s rules."
        if target == "global":
            self.store.set_setting("global_rules", joined(self.store.get_setting("global_rules", "")))
            return "Added to your global rules."
        if target == "rule_set":
            if rule_set_id:
                rs = self.store.rule_set(int(rule_set_id))
                if rs is None:
                    raise RoomError("That rule set is gone.")
                self.store.update_rule_set(rs["id"], rs["name"], joined(rs["body"]))
                if pid is not None:
                    self.store.log_project_event(int(pid), "rule", f"added a rule to the rule set {rs['name']}",
                                                 by="claude", detail=text)
                return f"Added to the rule set {rs['name']}."
            name = new_rule_set.strip()
            if not name:
                raise RoomError("Name the new rule set.")
            rid = self.store.add_rule_set(name, text)
            if pid is not None:
                self._project(int(pid))
                self.store.set_project_rule_sets(int(pid), [*self.store.project_rule_set_ids(int(pid)), rid])
                self.store.log_project_event(int(pid), "rule", f"created the rule set {name}", by="claude",
                                             detail=text)
                return f"Created the rule set {name} and attached it to this project."
            return f"Created the rule set {name}."
        raise RoomError(f"Unknown place for a rule: {target}")

    def delete_rule_set(self, rid: int) -> list[dict[str, Any]]:
        self.store.delete_rule_set(rid)
        ids = [i for i in self.store.get_setting("global_rule_sets", []) if i != rid]
        self.store.set_setting("global_rule_sets", ids)
        return self.store.rule_sets()

    def save_preset(self, name: str, model: str | None = None, mode: str = "default", policy: str | None = None,
                    rule_set_id: int | None = None, pid: int | None = None,
                    effort: str | None = None) -> list[dict[str, Any]]:
        if not name.strip():
            raise RoomError("A preset needs a name.")
        if mode not in MODES:
            raise RoomError(f"Unknown permission mode: {mode}")
        if policy not in (None, "", *POLICIES):
            raise RoomError(f"Unknown overlap setting: {policy}")
        if effort not in (None, "", *EFFORTS):
            raise RoomError(f"Unknown effort: {effort}")
        fields = {"name": name.strip(), "model": model or None, "effort": effort or None, "mode": mode,
                  "policy": policy or None, "rule_set_id": rule_set_id or None}
        if pid is None:
            self.store.add_preset(**fields)
        else:
            self.store.update_preset(pid, **fields)
        return self.store.presets()

    def delete_preset(self, pid: int) -> list[dict[str, Any]]:
        self.store.delete_preset(pid)
        return self.store.presets()

    def save_template(self, name: str, goal: str = "", rules: str = "", rule_set_ids: list[int] | None = None,
                      tags: list[str] | str | None = None, first_task: str = "", preset_id: int | None = None,
                      tid: int | None = None, extras: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not name.strip():
            raise RoomError("A template needs a name.")
        fields = {"name": name.strip(), "goal": goal.strip(), "rules": rules.strip(),
                  "rule_set_ids": [int(i) for i in rule_set_ids or []], "tags": clean_tags(tags),
                  "first_task": first_task.strip(), "preset_id": preset_id or None}
        if extras is not None:
            fields["extras"] = extras
        if tid is None:
            self.store.add_template(**fields)
        else:
            self.store.update_template(tid, **fields)
        return self.store.templates()

    def template_from_project(self, pid: int, name: str) -> list[dict[str, Any]]:
        p = self._project(pid)
        return self.save_template(name or f"{p['name']} template", goal=p["goal"], rules=p["rules"],
                                  rule_set_ids=self.store.project_rule_set_ids(pid), tags=p["tags"],
                                  preset_id=p["preset_id"], extras=self._template_extras(pid))

    def _template_extras(self, pid: int) -> dict[str, Any]:
        """What else a template copies from a project: task groups, routines, skills, review agents."""
        from carrier.core import skills

        p = self._project(pid)
        groups = {g["id"]: g for g in self.store.task_groups(pid)}
        return {
            "groups": [{"name": g["name"], "priority": g["priority"], "tags": g["tags"]} for g in groups.values()],
            "routines": [{k: r.get(k) for k in ("name", "instructions", "trigger", "gate", "enabled", "mode")}
                         | {"group": groups[r["group_id"]]["name"] if r.get("group_id") in groups else ""}
                         for r in self.store.routines(pid)],
            "skills": [{k: sk[k] for k in ("name", "description", "body")}
                       for sk in skills.list_skills(p["path"])["project"]],
            "agents": [{k: a.get(k) for k in self.store.AGENT_FIELDS if k != "project_id"}
                       for a in self.store.review_agents(pid)],
            "allow": self.allowances(pid),
            "images": self.images_allowed(pid),
            "watch_refs": self.watch_refs(pid),
        }

    def _apply_template_extras(self, pid: int, extras: dict[str, Any]) -> list[str]:
        """Recreate a template's groups, routines, skills and review agents in a new project.
        Skills that already exist in the folder are left as they are. Returns notes."""
        from carrier.core import skills
        from carrier.readyroom import routines

        notes = []
        if isinstance(extras.get("allow"), dict):
            self.set_allowances(pid, **{k: v for k, v in extras["allow"].items() if k in self.allowances(pid)})
        if extras.get("images") is False:
            self.store.set_setting(f"project:{pid}:images", False)
        if extras.get("watch_refs"):
            self.store.set_setting(f"project:{pid}:watch_refs", True)
        ids: dict[str, int] = {}
        for g in extras.get("groups") or []:
            self.save_group(pid, g["name"], g.get("priority", "normal"), g.get("tags"))
            ids[g["name"]] = max(x["id"] for x in self.store.task_groups(pid) if x["name"] == g["name"])
        for r in extras.get("routines") or []:
            routines.save(self, pid, r["name"], r["instructions"], r.get("trigger") or "manual",
                          r.get("gate") or "none", bool(r.get("enabled", True)), None, ids.get(r.get("group") or ""),
                          r.get("mode") or "")
        main = self._project(pid)["path"]
        for sk in extras.get("skills") or []:
            if skills.exists(main, sk["name"]):
                notes.append(f"Kept the existing skill {sk['name']}.")
            else:
                skills.save(main, sk["name"], sk["description"], sk["body"])
        for a in extras.get("agents") or []:
            self.store.add_review_agent(project_id=pid, **a)
        return notes

    def delete_template(self, tid: int) -> list[dict[str, Any]]:
        self.store.delete_template(tid)
        return self.store.templates()

    # -- what Claude will be told ------------------------------------------------------------

    def preview(self, pid: int | None, task_id: int | None = None, preset_id: int | None = None) -> str:
        task = self.store.task(task_id) if task_id else (self.current_task(pid) if pid is not None else None)
        project = self.store.project(pid) if pid is not None else None
        preset = self.store.preset(preset_id if preset_id else (project["preset_id"] if project else None))
        return context.build(self.store, pid, task, preset)


__all__ = ["ReadyRoom", "RoomError", "ACTIVE", "clean_tags"]
