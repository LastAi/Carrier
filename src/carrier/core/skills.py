# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Claude Code skills: list, read and write them.

A skill is a folder holding ``SKILL.md``: YAML-style front matter with a
``name`` and a ``description`` (Claude Code reads the description to decide
when to use it), then the instructions. Claude Code loads skills from

* the project: ``<main folder>/.claude/skills/<name>/SKILL.md``, and
* the user: ``~/.claude/skills/<name>/SKILL.md`` (``CLAUDE_CONFIG_DIR`` if set).

Flight Deck sessions load both, as the terminal does. Carrier only reads and
writes these files; nothing else about skills lives in Carrier.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 60_000


def user_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "skills"


def project_dir(main_folder: str | Path) -> Path:
    return Path(main_folder) / ".claude" / "skills"


def parse(text: str) -> dict[str, str]:
    """``{"name", "description", "body"}`` from a SKILL.md."""
    meta: dict[str, str] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() in ("name", "description"):
                meta[key.strip()] = value.strip().strip("'\"")
    return {"name": meta.get("name", ""), "description": meta.get("description", ""), "body": body.strip()}


def render(name: str, description: str, body: str) -> str:
    description = " ".join(description.split())  # one line in the front matter
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


def _read_dir(folder: Path, scope: str) -> list[dict[str, Any]]:
    out = []
    if not folder.is_dir():
        return out
    for d in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        try:
            info = parse(f.read_text(encoding="utf-8", errors="replace")[:MAX_BODY])
        except OSError:
            continue
        out.append({"name": info["name"] or d.name, "folder": d.name, "description": info["description"],
                    "body": info["body"], "scope": scope, "path": str(f)})
    return out


def list_skills(main_folder: str | Path | None) -> dict[str, list[dict[str, Any]]]:
    """The project's skills and the user's personal ones."""
    return {"project": _read_dir(project_dir(main_folder), "project") if main_folder else [],
            "personal": _read_dir(user_dir(), "personal")}


def clean_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")[:64]
    if not NAME_RE.match(n):
        raise ValueError("A skill needs a name: lowercase letters, digits and dashes, e.g. release-mod.")
    return n


def save(main_folder: str | Path | None, name: str, description: str, body: str, scope: str = "project",
         old_name: str = "") -> dict[str, Any]:
    """Write (or rewrite) a skill; ``old_name`` renames one. Returns the saved skill."""
    name = clean_name(name)
    if not description.strip():
        raise ValueError("Describe when Claude should use this skill.")
    if not body.strip():
        raise ValueError("The skill has no instructions.")
    if len(body) > MAX_BODY:
        raise ValueError("That skill is too long.")
    if scope == "project":
        if not main_folder:
            raise ValueError("No project to save the skill in.")
        root = project_dir(main_folder)
    elif scope == "personal":
        root = user_dir()
    else:
        raise ValueError(f"Unknown place for a skill: {scope}")
    target = root / name
    if old_name and clean_name(old_name) != name:
        old = root / clean_name(old_name)
        if target.exists():
            raise ValueError(f"A skill called {name} already exists there.")
        if old.is_dir():
            old.rename(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(render(name, description, body), encoding="utf-8")
    return next(s for s in _read_dir(root, scope) if s["folder"] == name)


def exists(main_folder: str | Path | None, name: str, scope: str = "project") -> bool:
    root = project_dir(main_folder) if scope == "project" and main_folder else user_dir()
    try:
        return (root / clean_name(name) / "SKILL.md").exists()
    except ValueError:
        return False


def delete(main_folder: str | Path | None, name: str, scope: str = "project") -> None:
    """Remove a skill's SKILL.md (and its folder if nothing else is in it)."""
    root = project_dir(main_folder) if scope == "project" and main_folder else user_dir()
    folder = root / clean_name(name)
    f = folder / "SKILL.md"
    if f.is_file():
        f.unlink()
    try:
        folder.rmdir()
    except OSError:
        pass  # it holds other files (scripts, templates): leave them
