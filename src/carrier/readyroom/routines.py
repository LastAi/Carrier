# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Routines: repeatable tasks a project runs again and again.

A routine has a name and instructions (what Claude should do), a **trigger**
and a **gate**:

* trigger ``manual`` — added to the queue when the user presses Run;
  ``after_task`` — added as the next task whenever another task of the
  project is finished (e.g. "update the documentation").
* gate ``none`` — can start any time; ``reviewed`` — can only start when the
  project's review is clear (e.g. "push to GitHub"): no review in progress,
  no unreviewed changes in its work folders, and no open fix tasks.

Running a routine just adds a task (linked to the routine) to the project's
queue; Flight Deck checks the gate when that task is started.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from carrier.core import gitutil

TRIGGERS = ("manual", "after_task", "group_done")
GATES = ("none", "reviewed")
# Permission modes a routine may use ("" = the project's default preset). Never
# "bypassPermissions": a routine can start on its own, so it must not skip asking.
MODES = ("", "default", "acceptEdits", "plan")


class RoutineError(ValueError):
    """Shown to the user as is."""


def save(room: Any, pid: int, name: str, instructions: str, trigger: str = "manual", gate: str = "none",
         enabled: bool = True, rid: int | None = None, group_id: int | None = None,
         mode: str = "") -> list[dict[str, Any]]:
    room._project(pid)
    if not name.strip():
        raise RoutineError("A routine needs a name.")
    if not instructions.strip():
        raise RoutineError("Say what the routine should do.")
    if trigger not in TRIGGERS:
        raise RoutineError(f"Unknown trigger: {trigger}")
    if gate not in GATES:
        raise RoutineError(f"Unknown gate: {gate}")
    if rid is not None and (r := room.store.routine(rid)) is not None and r["project_id"] != pid:
        raise RoutineError("That routine belongs to another project.")
    if group_id is not None and ((g := room.store.task_group(group_id)) is None or g["project_id"] != pid):
        raise RoutineError("Unknown task group.")
    if mode not in MODES:
        raise RoutineError("A routine can ask before acting, auto-accept edits or only plan — not “Never ask”.")
    if trigger == "group_done" and group_id is None:
        raise RoutineError("Pick the task group whose finish starts this routine.")
    room.store.save_routine(pid, rid, name=name.strip(), instructions=instructions.strip(), trigger=trigger,
                            gate=gate, enabled=enabled, group_id=group_id, mode=mode)
    room.store.log_project_event(pid, "routine", ("changed" if rid else "created") + f" the routine {name.strip()}",
                                 detail=f"{trigger}, gate {gate}" + ("" if enabled else ", paused"))
    return room.store.routines(pid)


def _queued(room: Any, pid: int, rid: int) -> dict[str, Any] | None:
    return next((t for t in room.store.tasks(pid, statuses=("planned", "active")) if t.get("routine_id") == rid),
                None)


def run(room: Any, rid: int) -> dict[str, Any]:
    """Queue a routine as the project's next task (once: not if it's already queued)."""
    r = room.store.routine(rid)
    if r is None:
        raise RoutineError("Unknown routine.")
    if _queued(room, r["project_id"], rid) is None:
        _queue(room, r)
    return room.project(r["project_id"])


def _queue(room: Any, r: dict[str, Any]) -> None:
    """Add a routine's run as the next task — in its task group's queue if it has one."""
    room.add_task(r["project_id"], r["name"], r["instructions"], first=True, routine_id=r["id"],
                  group_id=r.get("group_id"))


def after_task(room: Any, pid: int, group_id: int | None = None) -> None:
    """A task of the project finished: queue its enabled "after each task" routines
    (one tied to a task group only reacts to that group's tasks)."""
    for r in room.store.routines(pid):
        if not (r["enabled"] and r["trigger"] == "after_task") or _queued(room, pid, r["id"]) is not None:
            continue
        if r.get("group_id") is None or r["group_id"] == group_id:
            _queue(room, r)


def group_done(room: Any, pid: int, group_id: int) -> None:
    """A task group's last open task finished: queue the routines waiting for that."""
    for r in room.store.routines(pid):
        if r["enabled"] and r["trigger"] == "group_done" and r.get("group_id") == group_id \
                and _queued(room, pid, r["id"]) is None:
            _queue(room, r)


def takeoff_queue(store: Any) -> list[dict[str, Any]]:
    """Routine runs waiting to start, in every project, with whether their gate is clear."""
    out = []
    for p in store.projects():
        groups = {g["id"]: g for g in store.task_groups(p["id"])}
        for t in store.tasks(p["id"], statuses=("planned",)):
            if not t.get("routine_id") or (r := store.routine(t["routine_id"])) is None:
                continue
            blockers = review_blockers(store, p["id"]) if r["gate"] == "reviewed" else []
            group = groups.get(t.get("group_id"))
            out.append({"task_id": t["id"], "title": t["title"], "project_id": p["id"], "project": p["name"],
                        "group": group["name"] if group else "", "gate": r["gate"], "blockers": blockers,
                        "backlog": bool(group and group["priority"] == "backlog")})
    return out


# The review gate runs several git commands per project, and Flight Deck asks for it on
# every refresh: answers are cached per project and dropped when something that could
# change them happens (an edit, a finished review or task) or after CACHE_SECONDS.
CACHE_SECONDS = 45.0
_cache: dict[int, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()
_generation: dict[int, int] = {}  # bumped on invalidate, so a slow check can't store a stale answer


def invalidate(pid: int | None = None) -> None:
    """Forget the cached gate answer for one project (or all)."""
    with _cache_lock:
        if pid is None:
            _cache.clear()
            _generation[-1] = _generation.get(-1, 0) + 1  # every project
        else:
            _cache.pop(pid, None)
            _generation[pid] = _generation.get(pid, 0) + 1


def review_blockers(store: Any, pid: int, fresh: bool = False) -> list[str]:
    """Why the project's review isn't clear (empty list: it is). Cached unless ``fresh``."""
    now = time.monotonic()
    if not fresh:
        with _cache_lock:
            hit = _cache.get(pid)
        if hit and now - hit[0] < CACHE_SECONDS:
            return list(hit[1])
    with _cache_lock:
        gen = (_generation.get(pid, 0), _generation.get(-1, 0))
    out = _review_blockers(store, pid)
    with _cache_lock:
        if (_generation.get(pid, 0), _generation.get(-1, 0)) == gen:  # not invalidated meanwhile
            _cache[pid] = (now, out)
    return list(out)


def _review_blockers(store: Any, pid: int) -> list[str]:
    from carrier.core.config import load_config
    from carrier.debrief.store import open_review

    p = store.project(pid)
    out: list[str] = []
    ns = str(pid)
    seen: set[str] = set()
    for label, folder in store.project_roots(p):
        name = label or "the main folder"
        try:
            repo, sub = gitutil.locate(Path(folder))
        except gitutil.GitError:
            out.append(f"{name} isn't tracked by git yet, so it has never been reviewed.")
            continue
        key = os.path.normcase(str(repo))
        if key not in seen:
            seen.add(key)
            r = open_review(load_config(repo), ns=ns)
            if r is not None and not r.scan:
                out.append(f"A review is still open (started {r.created_at[:16].replace('T', ' ')}); finish it in "
                           "Debrief.")
        cp = gitutil.get_checkpoint(repo, ns)
        if cp is None:
            out.append(f"{name} has never been reviewed.")
            continue
        spec = sub or "."
        changed = gitutil.git(repo, "diff", "--name-only", cp, "--", spec, f":(exclude){gitutil.CARRIER_PATH}",
                              check=False).strip()
        untracked = gitutil.git(repo, "ls-files", "--others", "--exclude-standard", "--", spec,
                                f":(exclude){gitutil.CARRIER_PATH}", check=False).strip()
        n = len(set(changed.splitlines()) | set(untracked.splitlines()))
        if n:
            out.append(f"{name} has {n} changed file{'s' if n != 1 else ''} since the last review.")
    for h in secret_hits(store, pid)[:5]:
        out.append(f"Possible secret ({h['kind']}) in {h['path']} line {h['line']}: {h['preview']} — remove it, "
                   "or mark it safe in the Ready Room.")
    fixes = [t for t in store.tasks(pid, statuses=("planned", "active")) if t.get("fix_review")]
    if fixes:
        out.append(f"{len(fixes)} fix task{'s' if len(fixes) != 1 else ''} from reviews still open "
                   f"(“{fixes[0]['title']}”).")
    return out


def secret_hits(store: Any, pid: int) -> list[dict[str, Any]]:
    """Likely secrets in the files the project would publish (its work folders), minus
    the ones the user marked safe."""
    from carrier.core import secrets

    p = store.project(pid)
    safe = set(store.get_setting(f"project:{pid}:secrets_safe", []))
    hits: list[dict[str, Any]] = []
    for label, folder in store.project_roots(p):
        hits += secrets.scan_folder(Path(folder), safe, label)
    return hits


def mark_secret_safe(store: Any, pid: int, fingerprint: str) -> None:
    safe = list(store.get_setting(f"project:{pid}:secrets_safe", []))
    if fingerprint not in safe:
        store.set_setting(f"project:{pid}:secrets_safe", [*safe, fingerprint])
    store.log_project_event(pid, "secrets", "marked a possible secret as safe", detail=fingerprint)
    invalidate(pid)


def check_gate(store: Any, task: dict[str, Any]) -> None:
    """Raise if the task is a gated routine whose project isn't ready for it."""
    rid = task.get("routine_id")
    if not rid or task.get("project_id") is None:
        return
    r = store.routine(rid)
    if r is None or r["gate"] != "reviewed":
        return
    blockers = review_blockers(store, task["project_id"], fresh=True)  # starting: always check now
    if blockers:
        raise RoutineError(f"“{r['name']}” waits until the project's review is clear: " + " ".join(blockers))
