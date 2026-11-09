# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Which files a project owns.

* A project's **main folder** is its own alone. Another project may only use it (or anything
  inside it) as a *reference* folder: read, never edited. :func:`normalize` turns such a work
  folder into a reference folder.
* A **work folder** is either *this project's* (``share: "own"``, everything in it) or a
  **shared space** (``share: "shared"``) that other projects use too. In a shared space the
  project owns *its part*: files matching its patterns (``part``, like ``ships/**``), files
  it created there (``claimed``), and — for reviews — files its sessions changed.
* Other projects' main folders inside a work folder stay theirs (read only).

What that means:

* Flight Deck: edits in another project's main folder are blocked; edits to existing files
  outside the project's part of a shared space always ask (even when editing is
  pre-approved); new files there are allowed and become the project's.
* Debrief: reviews and scans leave out other projects' main folders and, in shared spaces,
  files outside the project's part that its sessions didn't change.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

SHARES = ("own", "shared")


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _inside(path: str, root: str) -> bool:
    t, r = _norm(path), _norm(root)
    return t == r or t.startswith(r.rstrip(os.sep) + os.sep)


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """A folder pattern (``ships/**``, ``data/ship_*.csv``, ``*.md``) as a regex on posix paths.

    ``**`` matches any number of folders, ``*`` and ``?`` stay within one name. A pattern
    without a slash matches the name anywhere (like .gitignore); a trailing slash or ``/**``
    means everything inside that folder, and a plain name (``ships``, ``data/ships``) means
    that file or folder with everything inside it."""
    p = pattern.strip().replace("\\", "/")
    if p.endswith("/"):
        p += "**"
    # A plain name: that file or folder and everything inside it.
    tail = "(?:/.*)?" if not any(c in p for c in "*?") else ""
    anchored = "/" in p.rstrip("/")
    p = p.lstrip("/")
    out = ""
    i = 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(("" if anchored else "(?:.*/)?") + out + tail + "$", re.IGNORECASE if os.name == "nt" else 0)


def clean_patterns(patterns: list[str] | str) -> list[str]:
    items = patterns.replace("\n", ",").split(",") if isinstance(patterns, str) else list(patterns or [])
    out: list[str] = []
    for x in (str(i).strip() for i in items):
        if not x:
            continue
        if ".." in x.replace("\\", "/").split("/") or re.match(r"^([A-Za-z]:|[/\\~])", x):
            raise ValueError(f"“{x}”: patterns are paths inside the folder, like ships/** or data/*.csv")
        if x not in out:
            out.append(x)
    return out


def matches(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/").lstrip("/")
    return any(pattern_regex(p).match(rel) for p in patterns)


def foreign_mains(store: Any, project: dict[str, Any]) -> list[dict[str, Any]]:
    """Other projects whose main folder is inside one of this project's folders (main or work)."""
    roots = [r for _, r in store.project_roots(project)]
    out = []
    for q in store.projects():
        if q["id"] != project["id"] and any(_inside(q["path"], r) for r in roots):
            out.append({"id": q["id"], "name": q["name"], "path": q["path"]})
    return out


def claimed(store: Any, pid: int, folder: str) -> list[str]:
    return list((store.get_setting(f"project:{pid}:claimed", {}) or {}).get(_norm(folder), []))


def is_claimed(store: Any, pid: int, folder: str, rel: str) -> bool:
    key = os.path.normcase(rel.replace("\\", "/"))
    return any(os.path.normcase(c) == key for c in claimed(store, pid, folder))


def claim(store: Any, pid: int, folder: str, rel: str) -> None:
    """Remember a file the project created in a shared space: it's the project's now."""
    data = store.get_setting(f"project:{pid}:claimed", {}) or {}
    files = data.setdefault(_norm(folder), [])
    rel = rel.replace("\\", "/")
    if rel not in files:
        files.append(rel)
        store.set_setting(f"project:{pid}:claimed", data)


def classify(store: Any, project: dict[str, Any], path: str) -> dict[str, Any]:
    """Who a file belongs to, for this project.

    ``{"kind": ..., "folder": folder dict or None, "rel": path in the folder, "owner": name}``
    with kind one of: "own" (this project's), "part" (its part of a shared space), "new" (a
    file that doesn't exist yet in a shared space: it becomes the project's), "other" (an
    existing file in a shared space outside the project's part), "foreign" (another project's
    main folder), "outside" (none of the project's work folders).
    """
    for q in foreign_mains(store, project):
        if _inside(path, q["path"]):
            return {"kind": "foreign", "folder": None, "rel": "", "owner": q["name"], "owner_path": q["path"]}
    if _inside(path, project["path"]):
        return {"kind": "own", "folder": None, "rel": "", "owner": project["name"]}
    from carrier.core.db import folder_kind

    for f in sorted(project.get("folders") or [], key=lambda f: -len(f["path"])):
        if folder_kind(f) != "work" or not _inside(path, f["path"]):
            continue
        # Keep the file name's case (Windows paths are case-insensitive, but git and people aren't).
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(f["path"])).replace("\\", "/")
        if f.get("share") != "shared":
            return {"kind": "own", "folder": f, "rel": rel, "owner": project["name"]}
        if matches(rel, f.get("part") or []) or is_claimed(store, project["id"], f["path"], rel):
            return {"kind": "part", "folder": f, "rel": rel, "owner": project["name"]}
        if not os.path.exists(path):
            return {"kind": "new", "folder": f, "rel": rel, "owner": project["name"]}
        return {"kind": "other", "folder": f, "rel": rel, "owner": ""}
    return {"kind": "outside", "folder": None, "rel": "", "owner": ""}


def normalize(store: Any, log: Any = None) -> list[str]:
    """Apply the ownership rules to every project's folders; returns what changed.

    * a work folder that is (or is inside) another project's main folder becomes a reference
      folder — a main folder belongs to its project alone;
    * a work folder another project also works in becomes a shared space;
    * the earlier "review only this project's changes" setting becomes a shared space.
    """
    from carrier.core.db import folder_kind

    changes: list[str] = []

    def note(p: dict[str, Any], msg: str) -> None:
        changes.append(f"{p['name']}: {msg}")
        if log:
            log(p["id"], msg)

    # 1. Work folders in another project's main folder become reference folders.
    projects = store.projects()
    for p in projects:
        folders, changed = [], False
        for f in p.get("folders") or []:
            f = dict(f)
            owner = next((q for q in projects if q["id"] != p["id"] and _inside(f["path"], q["path"])), None)
            if folder_kind(f) == "work" and owner is not None:
                f["kind"] = "reference"
                for k in ("share", "part"):
                    f.pop(k, None)
                changed = True
                note(p, f"{f['path']} is now a reference folder: it's in {owner['name']}'s main folder, "
                        "which belongs to that project alone")
            folders.append(f)
        if changed:
            store.update_project(p["id"], folders=folders)
    # 2. With those settled: work folders other projects work in too are shared spaces.
    projects = store.projects()
    for p in projects:
        folders, changed = [], False
        for f in p.get("folders") or []:
            f = dict(f)
            if folder_kind(f) == "work":
                if f.get("review") == "own":  # the earlier "only this project's changes" setting
                    f["review"] = "all"
                    f["share"] = "shared"
                    changed = True
                shared = any(folder_kind(g) == "work" and (_inside(f["path"], g["path"]) or _inside(g["path"], f["path"]))
                             for q in projects if q["id"] != p["id"] for g in q.get("folders") or [])
                if shared and f.get("share") != "shared":
                    f["share"] = "shared"
                    changed = True
                    note(p, f"{f['path']} is now a shared space: another project works in it too")
            folders.append(f)
        if changed:
            store.update_project(p["id"], folders=folders)
    return changes
