# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Sharing a project's pre-approved actions with Claude Code itself.

Claude Code keeps personal per-project permission rules in
``<project>/.claude/settings.local.json`` (``permissions.allow``). With a project's
"Also let Claude Code use these" switch on, Carrier writes its pre-approvals there as
Claude Code rules, so plain Claude Code in that folder (the terminal, the Claude app)
doesn't ask for them either. Inside Carrier, Carrier's own list decides.

Carrier only ever adds and removes the rules it wrote itself (remembered per project);
rules you added, and every other setting in the file, are left as they are. If git would
otherwise publish the file, it is added to the repository's ``.git/info/exclude`` (ignored
on this computer, without changing any file in the project).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from carrier.core import gitutil, secrets
from carrier.flightdeck.sdk import group_entries
from carrier.flightdeck.shellcheck import PROTECTED, RISKY_ARGS, SCOPED_READERS, program_name

FILE = Path(".claude") / "settings.local.json"


class SettingsFileError(Exception):
    """The settings file can't be updated (it isn't valid JSON, or can't be written)."""


def rules_for(allow: dict[str, Any]) -> list[str]:
    """Claude Code permission rules for a project's pre-approvals.

    Reading and editing cover the project's main folder (``/**`` is relative to the
    project in project settings); extra folders are Carrier-only.
    """
    rules: list[str] = []
    if allow.get("read"):
        rules.append("Read(/**)")
    if allow.get("edit"):
        rules.append("Edit(/**)")
    if allow.get("web_search"):
        rules.append("WebSearch")
    if allow.get("web_fetch"):
        sites = allow.get("sites") or []
        rules += [f"WebFetch(domain:{s})" for s in sites] if sites else ["WebFetch"]
    for entry in group_entries(allow):
        # Readers are limited to the project's folders only by Carrier's own check: plain
        # Claude Code would let them read anywhere, so they stay Carrier-only.
        # So are programs with options Carrier holds back (git -c, find -exec …): Claude
        # Code's own rules would allow those options.
        prog = program_name(entry.split()[0]).lower()
        if prog in SCOPED_READERS or prog in RISKY_ARGS:
            continue
        rules.append(f"Bash({entry}:*)")  # Claude Code checks each part of a compound command itself
    rules += list(allow.get("tools") or [])
    return list(dict.fromkeys(rules))


def ask_rules_for(allow: dict[str, Any]) -> list[str]:
    """With editing shared: still ask before edits that could make git or Claude Code run
    commands (``ask`` beats ``allow`` in Claude Code)."""
    if not allow.get("edit"):
        return []
    files = (".mcp.json", ".gitmodules", ".envrc")
    return [f"Edit(**/{p})" if p in files else f"Edit(**/{p}/**)" for p in PROTECTED]


def sync(main_folder: str | Path, allow: dict[str, Any], owned: list[str]) -> list[str]:
    """Bring the file's Carrier rules up to date. ``owned``: the rules Carrier wrote last
    time. Returns the rules Carrier owns now (store them for next time)."""
    folder = Path(main_folder)
    path = folder / FILE
    if path.is_symlink():
        path = path.resolve()  # keep the link: write where it points
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, ValueError) as exc:
            raise SettingsFileError(f"{path} isn't valid JSON, so Carrier left it alone ({exc}).") from exc
        if not isinstance(data, dict):
            raise SettingsFileError(f"{path} isn't a settings object, so Carrier left it alone.")
    perms = data.get("permissions")
    if perms is None:
        perms = {}
    if not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list):
        raise SettingsFileError(f"{path} has an unexpected \"permissions\" section, so Carrier left it alone.")
    if not isinstance(perms.get("ask", []), list):
        raise SettingsFileError(f"{path} has an unexpected \"permissions\" section, so Carrier left it alone.")
    on = allow.get("export", True)
    now_owned: list[str] = []
    changed = False
    for key, rules in (("allow", rules_for(allow) if on else []), ("ask", ask_rules_for(allow) if on else [])):
        current = list(perms.get(key, []))
        mine = {r.split(":", 1)[1] for r in owned if r.startswith(key + ":")}  # "allow:Bash(javap:*)"
        theirs = [r for r in current if not (isinstance(r, str) and r in mine)]  # the user's (kept as they are)
        wanted = [r for r in rules if r not in theirs]
        merged = theirs + wanted
        now_owned += [f"{key}:{r}" for r in wanted]
        if merged != current:
            changed = True
            if merged:
                perms[key] = merged
            else:
                perms.pop(key, None)
    if not changed:
        return now_owned
    if not path.exists() and not perms:
        return []
    if perms:
        data["permissions"] = perms
    else:
        data.pop("permissions", None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise SettingsFileError(f"Couldn't write {path}: {exc}") from exc
    _keep_private(folder)
    return now_owned


def _keep_private(folder: Path) -> None:
    """Make sure git won't publish the file (personal settings), as Claude Code does."""
    try:
        repo, sub = gitutil.locate(folder)
    except gitutil.GitError:
        return
    rel = f"{sub}/{FILE.as_posix()}" if sub else FILE.as_posix()
    tracked = gitutil.git(repo, "ls-files", "--", rel, check=False).strip()
    if tracked:
        return  # already in the repository: the user chose to keep it there
    if gitutil.git(repo, "check-ignore", "--", rel, check=False).strip():
        return  # already ignored
    # .git/info/exclude: ignored on this computer only, without changing a file git tracks.
    exclude = Path(gitutil.git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    secrets.append_gitignore(exclude, ["/" + rel], "Claude Code personal settings (added by Carrier)")
