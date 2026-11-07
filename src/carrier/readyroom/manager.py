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

import re
import time
from pathlib import Path
from typing import Any

from carrier.core import gitutil
from carrier.core.db import ACTIVE, FOLDER_KINDS, Store, default_path, folder_kind
from carrier.core.llm import EFFORTS
from carrier.readyroom import context

OPEN = ("planned", "active")
MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
POLICIES = ("warn", "ask", "block", "inform")
MAX_TAG_LEN = 30


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


class ReadyRoom:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store(default_path())

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

    def current_task(self, pid: int) -> dict[str, Any] | None:
        open_tasks = self.store.tasks(pid, statuses=OPEN)
        return open_tasks[0] if open_tasks else None

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
        return {**p, "folder_exists": Path(p["path"]).is_dir(),
                "folders": [{**f, "kind": folder_kind(f), "exists": Path(f["path"]).is_dir()}
                            for f in p["folders"]],
                "rule_set_ids": self.store.project_rule_set_ids(pid),
                "tasks": self.store.tasks(pid, statuses=OPEN),
                "history": self.store.tasks(pid, statuses=("done",)),
                "links": links,
                "active_sessions": len(self._active_sessions(pid))}

    # -- projects ------------------------------------------------------------------

    def create_project(self, name: str, path: str, goal: str = "", first_task: str = "",
                       tags: list[str] | str | None = None, rules: str = "",
                       rule_set_ids: list[int] | None = None, preset_id: int | None = None,
                       template_id: int | None = None) -> dict[str, Any]:
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise RoomError(f"Folder not found: {path}")
        folder = folder.resolve()
        if self.store.project_by_path(str(folder)):
            raise RoomError("That folder is already a project's main folder.")
        if template_id is not None:  # the template fills anything left empty
            t = self.store.template(template_id)
            if t is None:
                raise RoomError("Unknown template.")
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
        try:
            self.store.update_project(pid, goal=goal.strip(), rules=rules.strip(), tags=clean_tags(tags),
                                      preset_id=preset_id)
            if rule_set_ids:
                self.store.set_project_rule_sets(pid, [int(i) for i in rule_set_ids])
            if first_task.strip():
                self.store.add_task(first_task.strip(), pid)
        except Exception:
            self.store.remove_project(pid)  # don't leave a half-made project behind
            raise
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
        self.store.update_project(pid, **fields)
        if rule_set_ids is not None:
            self.store.set_project_rule_sets(pid, [int(i) for i in rule_set_ids])
        return self.project(pid)

    # -- extra folders -----------------------------------------------------------------

    def add_folder(self, pid: int, path: str, label: str = "", kind: str = "work") -> dict[str, Any]:
        """Add an extra folder to a project.

        Claude Code still runs in the main folder and is given access to the
        extra folders too:

        * **work** — Claude may read and edit it; Debrief reviews it with the
          main folder. Must be a git repository's top folder (or not in git yet).
        * **reference** — read only: Claude can look things up there; edits are
          blocked and it isn't reviewed.

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
        own = [Path(r).resolve() for _, r in self.store.project_roots(p, include_reference=True)]
        if folder in own:
            raise RoomError("That folder is already part of this project.")
        if any(r in folder.parents or folder in r.parents for r in own):
            raise RoomError("That folder is inside (or contains) one of this project's folders.")
        if kind == "work":
            self._check_work_root(folder)
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
        return self.project(pid)

    @staticmethod
    def _check_work_root(folder: Path) -> None:
        # Debrief and Flight Deck track a work folder through its git repository,
        # so a folder inside a repository must be that repository's top folder.
        try:
            root = gitutil.repo_root(folder).resolve()
        except gitutil.GitError:
            root = folder  # not in git yet: Debrief offers to start tracking it
        if root != folder:
            raise RoomError(f"That folder is inside the git repository {root}. Add the repository's "
                            "top folder instead, or add it as a reference folder.")

    def set_folder_kind(self, pid: int, path: str, kind: str) -> dict[str, Any]:
        """Switch an extra folder between work and reference."""
        if kind not in FOLDER_KINDS:
            raise RoomError(f"Unknown folder type: {kind}")
        p = self._project(pid)
        if self._active_sessions(pid):
            raise RoomError("End this project's sessions before changing its folders.")
        if not any(f["path"] == path for f in p["folders"]):
            raise RoomError("That folder isn't part of this project.")
        if kind == "work":
            self._check_work_root(Path(path))
        self.store.update_project(pid, folders=[{**f, "kind": kind} if f["path"] == path else f
                                                for f in p["folders"]])
        return self.project(pid)

    def remove_folder(self, pid: int, path: str) -> dict[str, Any]:
        p = self._project(pid)
        if self._active_sessions(pid):
            raise RoomError("End this project's sessions before changing its folders.")
        self.store.update_project(pid, folders=[f for f in p["folders"] if f["path"] != path])
        return self.project(pid)

    def remove_project(self, pid: int) -> dict[str, Any]:
        self._project(pid)
        if self._active_sessions(pid):
            raise RoomError("End this project's sessions first.")
        self.store.remove_project(pid)
        return self.overview()

    # -- tasks ------------------------------------------------------------------------

    def add_task(self, pid: int, title: str, details: str = "", first: bool = False) -> dict[str, Any]:
        self._project(pid)
        title = title.strip()
        if not title:
            raise RoomError("A task needs a title.")
        new_id = self.store.add_task(title, pid, details=details.strip())
        if first:
            # "First" means next up: after any task a session is already working
            # on, so the current task doesn't change under a running session.
            queue = [x["id"] for x in self.store.tasks(pid, statuses=OPEN) if x["id"] != new_id]
            active = {x["id"] for x in self.store.tasks(pid, statuses=("active",))}
            k = 0
            while k < len(queue) and queue[k] in active:
                k += 1
            queue.insert(k, new_id)
            for n, task_id in enumerate(queue, start=1):
                self.store.update_task(task_id, position=n)
        return self.project(pid)

    def update_task(self, tid: int, title: str | None = None, details: str | None = None) -> dict[str, Any]:
        t = self._task(tid)
        fields: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise RoomError("A task needs a title.")
            fields["title"] = title.strip()
        if details is not None:
            fields["details"] = details.strip()
        self.store.update_task(tid, **fields)
        return self.project(t["project_id"]) if t["project_id"] is not None else self._task(tid)

    def delete_task(self, tid: int) -> dict[str, Any]:
        t = self._task(tid)
        if self._active_sessions(task_id=tid):
            raise RoomError("A session is working on this task. End it first.")
        self.store.delete_task(tid)
        return self.project(t["project_id"]) if t["project_id"] is not None else {"deleted": tid}

    def move_task(self, tid: int, direction: str) -> dict[str, Any]:
        """Move a task up/down in its project's queue, or to the top ("current")."""
        t = self._task(tid)
        pid = t["project_id"]
        if pid is None:
            raise RoomError("Standalone tasks have no queue.")
        queue = self.store.tasks(pid, statuses=OPEN)
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
        return self._task(tid) if t["project_id"] is None else self.project(t["project_id"])

    def reopen_task(self, tid: int) -> dict[str, Any]:
        t = self._task(tid)
        self.store.update_task(tid, status="planned", finished=None)
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
                      tid: int | None = None) -> list[dict[str, Any]]:
        if not name.strip():
            raise RoomError("A template needs a name.")
        fields = {"name": name.strip(), "goal": goal.strip(), "rules": rules.strip(),
                  "rule_set_ids": [int(i) for i in rule_set_ids or []], "tags": clean_tags(tags),
                  "first_task": first_task.strip(), "preset_id": preset_id or None}
        if tid is None:
            self.store.add_template(**fields)
        else:
            self.store.update_template(tid, **fields)
        return self.store.templates()

    def template_from_project(self, pid: int, name: str) -> list[dict[str, Any]]:
        p = self._project(pid)
        return self.save_template(name or f"{p['name']} template", goal=p["goal"], rules=p["rules"],
                                  rule_set_ids=self.store.project_rule_set_ids(pid), tags=p["tags"],
                                  preset_id=p["preset_id"])

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
