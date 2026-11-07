# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Debrief for Carrier projects: what a project review needs from the store.

A *project review* covers the project's main folder plus its extra folders
(see ``Review.parts``). This module turns a Ready Room project into a
:class:`ProjectSpec` for :class:`~carrier.debrief.session.ReviewSession`:

* ``extras``      — ``(label, folder)`` for each extra folder,
* ``context``     — goal, rules and recent work (``readyroom.context``) plus the
  Flight Deck tasks worked on since the last review, given to every agent,
* ``criteria``    — the review agents managed in the Ready Room (below),
* ``task_lookup`` — which Flight Deck task edited a file, from the shared
  edit log, so each item can say which task it came from,
* ``add_fix_task`` — "Needs fix" decisions become a task in the project queue.

Review agents
-------------
Effective agents for a project, later wins on a name clash:

1. Debrief's built-ins (each can be switched off globally or per project),
2. the folder's ``.carrier/criteria/*.md`` files (still supported),
3. global agents from the Ready Room,
4. the project's own agents.

Like the rest of Debrief this uses the Ready Room only through its public
API (``ReadyRoom``, ``context.build``) and the shared store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from carrier.core import gitutil
from carrier.core.config import load_config
from carrier.core.db import Store
from carrier.core import models as models_catalog
from carrier.core.llm import EFFORTS
from carrier.debrief.criteria import Criterion, builtin_criteria, project_criteria
from carrier.debrief.models import SEVERITIES


@dataclass
class ProjectSpec:
    id: int
    name: str
    path: Path
    extras: list[tuple[str, Path]] = field(default_factory=list)
    context: str = ""
    criteria: list[Criterion] | None = None
    task_lookup: Callable[[str, float | None], list[dict[str, Any]]] | None = None
    add_fix_task: Callable[[str, str], dict[str, Any]] | None = None


# -- review agents -------------------------------------------------------------------

def _disabled_key(pid: int | None) -> str:
    return "review_builtins_disabled" if pid is None else f"project:{pid}:review_builtins_disabled"


def disabled_builtins(store: Store, pid: int | None) -> list[str]:
    return list(store.get_setting(_disabled_key(pid), []))


def set_builtin_enabled(store: Store, name: str, enabled: bool, pid: int | None) -> None:
    if name not in {c.name for c in builtin_criteria()}:
        raise ValueError(f"Unknown built-in agent: {name}")
    off = [n for n in disabled_builtins(store, pid) if n != name]
    if not enabled:
        off.append(name)
    store.set_setting(_disabled_key(pid), off)


# Per-scope settings for built-in agents: {name: {"severity", "model", "effort"}}.
# A project's value wins over the global one, which wins over the built-in's.
OPTION_KEYS = ("severity", "model", "effort")


def _options_key(pid: int | None) -> str:
    return "review_builtin_options" if pid is None else f"project:{pid}:review_builtin_options"


def builtin_options(store: Store, pid: int | None) -> dict[str, dict[str, str]]:
    return dict(store.get_setting(_options_key(pid), {}))


def _clean_option(key: str, value: Any) -> str | None:
    value = (str(value).strip() if value else "") or None
    if value is None:
        return None
    if key == "model":
        if not models_catalog.known(value):
            raise ValueError(f"Unknown model: {value}")
        return value
    value = value.lower()
    if value not in {"severity": SEVERITIES, "effort": EFFORTS}[key]:
        raise ValueError(f"Unknown {key}: {value}")
    return value


def set_builtin_option(store: Store, name: str, key: str, value: Any, pid: int | None) -> None:
    """Set (or with an empty value, clear) one setting of a built-in agent in a scope."""
    if name not in {c.name for c in builtin_criteria()}:
        raise ValueError(f"Unknown built-in agent: {name}")
    if key not in OPTION_KEYS:
        raise ValueError(f"Unknown setting: {key}")
    opts = builtin_options(store, pid)
    entry = {k: v for k, v in opts.get(name, {}).items() if k in OPTION_KEYS}
    cleaned = _clean_option(key, value)
    if cleaned is None:
        entry.pop(key, None)
    else:
        entry[key] = cleaned
    if entry:
        opts[name] = entry
    else:
        opts.pop(name, None)
    store.set_setting(_options_key(pid), opts)


def _apply_options(c: Criterion, *layers: dict[str, str]) -> Criterion:
    for layer in layers:
        if layer.get("severity"):
            c.severity = layer["severity"]
        if layer.get("model"):
            c.model = layer["model"]
        if layer.get("effort"):
            c.effort = layer["effort"]
    return c


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "agent"


def save_agent(store: Store, fields: dict[str, Any], aid: int | None = None,
               pid: int | None = None) -> int:
    title = str(fields.get("title", "")).strip()
    prompt = str(fields.get("prompt", "")).strip()
    if not title or not prompt:
        raise ValueError("A review agent needs a title and what to look for.")
    severity = str(fields.get("severity", "medium")).lower()
    clean = {"title": title, "prompt": prompt, "severity": severity if severity in SEVERITIES else "medium",
             "model": _clean_option("model", fields.get("model")),
             "effort": _clean_option("effort", fields.get("effort")),
             "name": slug(str(fields.get("name") or title)),
             **{k: bool(fields.get(k, d)) for k, d in
                (("sees_rationale", False), ("uses_notes", True), ("explore", False), ("enabled", True))}}
    if aid is None:
        return store.add_review_agent(project_id=pid, **clean)
    store.update_review_agent(aid, **clean)
    return aid


def _as_criterion(a: dict[str, Any]) -> Criterion:
    return Criterion(name=a["name"], title=a["title"], prompt=a["prompt"], severity=a["severity"],
                     model=a.get("model") or None, effort=a.get("effort") or None,
                     sees_rationale=a["sees_rationale"], uses_notes=a["uses_notes"], explore=a["explore"],
                     enabled=a["enabled"], source="ready-room" if a["project_id"] is None else "project")


def agents_view(store: Store, pid: int | None) -> dict[str, Any]:
    """Everything the Ready Room's review-agent editor shows for global (None) or a project."""
    off_global = set(disabled_builtins(store, None))
    off_here = set(disabled_builtins(store, pid))
    opts_global = builtin_options(store, None)
    opts_here = builtin_options(store, pid) if pid is not None else {}
    builtins = []
    for c in builtin_criteria():
        # "inherited": what applies if this scope sets nothing (built-in, then global).
        inherited = {"severity": c.severity, "model": None, "effort": None}
        if pid is not None:
            inherited.update({k: v for k, v in opts_global.get(c.name, {}).items() if v})
        own = (opts_here if pid is not None else opts_global).get(c.name, {})
        builtins.append({"name": c.name, "title": c.title, "prompt": c.prompt, "default_severity": c.severity,
                         "inherited": inherited, "own": {k: own.get(k) for k in OPTION_KEYS},
                         "enabled": c.name not in off_here and (pid is None or c.name not in off_global),
                         "off_globally": pid is not None and c.name in off_global})
    return {"builtins": builtins, "agents": store.review_agents(pid),
            "models": [m["value"] for m in models_catalog.catalog()["models"]],
            "efforts": list(EFFORTS), "default_model": "sonnet",
            "global_agents": store.review_agents(None) if pid is not None else []}


def effective_criteria(store: Store, pid: int, repo: Path) -> list[Criterion]:
    off = set(disabled_builtins(store, None)) | set(disabled_builtins(store, pid))
    merged: dict[str, Criterion] = {}
    opts_global, opts_project = builtin_options(store, None), builtin_options(store, pid)
    for c in builtin_criteria():
        if c.name not in off:
            merged[c.name] = _apply_options(c, opts_global.get(c.name, {}), opts_project.get(c.name, {}))
    for c in project_criteria(load_config(repo)):
        merged[c.name] = c
    for a in store.review_agents(pid, include_global=True):  # global first, then the project's
        merged[a["name"]] = _as_criterion(a)
    return [c for c in merged.values() if c.enabled]


# -- project specs -----------------------------------------------------------------------

def project_spec(store: Store, pid: int) -> ProjectSpec:
    from carrier.readyroom import context as rr_context
    from carrier.readyroom.manager import ReadyRoom

    p = store.project(pid)
    if p is None:
        raise ValueError("Unknown project.")
    extras = [(lbl, Path(path)) for lbl, path in store.project_roots(p)[1:]]  # work folders only
    text = rr_context.build(store, pid, None, for_review=True)
    if extras:
        text += "\n\n## Folders in this review\nFiles named `<label>/…` are in the project's extra folders:\n" + \
            "\n".join(f"- {lbl}/ = {path}" for lbl, path in extras)

    def task_lookup(path: str, since: float | None) -> list[dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for e in store.edits(pid, path=path, since=since or 0):
            s = store.session(e["session_id"])
            t = store.task(s["task_id"]) if s and s.get("task_id") else None
            if t and t["id"] not in out:
                out[t["id"]] = {"id": t["id"], "title": t["title"], "session": s["id"], "status": t["status"]}
        return list(out.values())

    room = ReadyRoom(store)

    def add_fix_task(title: str, details: str) -> dict[str, Any]:
        project = room.add_task(pid, title, details, first=True)
        return next(t for t in project["tasks"] if t["title"] == title)

    return ProjectSpec(id=pid, name=p["name"], path=Path(p["path"]), extras=extras, context=text,
                       criteria=effective_criteria(store, pid, Path(p["path"])),
                       task_lookup=task_lookup, add_fix_task=add_fix_task)


def project_kwargs(repo: Path, store: Store | None = None) -> dict[str, Any]:
    """``prepare()`` arguments when ``repo`` is a Carrier project's main folder, else ``{}``.

    Used by the terminal commands and the Stop hook, so a review prepared
    there covers the same folders, context and agents as one started in
    the app (and can be resumed there).
    """
    from carrier.core.db import default_path

    try:
        if store is None:
            path = default_path()
            if not path.exists():  # Carrier's app was never used here: nothing to look up
                return {}
            store = Store(path)
        p = store.project_by_path(str(Path(repo).resolve()))
        if p is None:
            return {}
        spec = project_spec(store, p["id"])
    except Exception:  # no store yet, or it can't be read: plain folder review
        return {}
    # Only folders that are git repositories of their own can be reviewed; the
    # app asks to start tracking the others, here they are left out.
    return {"extras": [(lbl, path) for lbl, path in spec.extras if gitutil.is_repo(path)],
            "project": {"id": spec.id, "name": spec.name}, "context": spec.context,
            "criteria": spec.criteria, "task_lookup": spec.task_lookup}


def _key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def review_matches(review: Any, project_id: int | None, extras: list[tuple[str, Path]]) -> bool:
    """Was ``review`` prepared for exactly this project and these folders?

    Compares labels *and* folders, so a review made before an extra folder
    was swapped for another one with the same label is not resumed.
    """
    have = {(p["label"], _key(p["repo"])) for p in review.parts}
    want = {(lbl, _key(path)) for lbl, path in extras}
    return review.project_id == project_id and have == want
