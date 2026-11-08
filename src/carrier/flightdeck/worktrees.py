# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Isolated copies (git worktrees) for parallel sessions in one project.

When a second session starts in a project that already has an active
session in its folder, Flight Deck offers an *own copy*: a git worktree —
a second checkout of the same repository, on its own branch, in its own
folder. Two sessions then cannot overwrite each other's files at all; their
work meets again when the branch is merged (and each can be reviewed with
Debrief first).

Worktrees are created in the per-user Carrier folder
(``worktrees/<project>/<session-id>``) so they don't clutter the project.
The branch is ``carrier/<session-id>-<slug>``, starting from the project's
current commit. Uncommitted changes in the main folder are *not* copied —
the user is told so when they choose this option.
"""

from __future__ import annotations

import re
from pathlib import Path

from carrier.core import gitutil, usersettings


def worktrees_root() -> Path:
    return usersettings.config_dir() / "worktrees"


def slug(text: str, limit: int = 30) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-")) or "session"


def can_isolate(project_path: Path) -> bool:
    """Worktrees need a git repository with at least one commit."""
    try:
        root = gitutil.repo_root(project_path)
    except gitutil.GitError:
        return False
    return gitutil.has_head(root)


def create(project_path: Path, project_name: str, session_id: int, task: str) -> tuple[Path, str]:
    """Create the worktree; returns ``(folder, branch)``.

    When the project's folder is a subfolder of its repository, the whole
    repository is copied and ``folder`` is that subfolder inside the copy.
    """
    repo, sub = gitutil.locate(project_path)
    branch = f"carrier/{session_id}-{slug(task)}"
    folder = worktrees_root() / slug(project_name) / str(session_id)
    folder.parent.mkdir(parents=True, exist_ok=True)
    gitutil.git(repo, "worktree", "add", "-b", branch, str(folder), "HEAD")
    return (folder / sub if sub else folder), branch


def remove(project_path: Path, folder: Path, force: bool = False) -> None:
    """Remove a worktree folder (the branch is kept, so no work is lost)."""
    repo = gitutil.repo_root(project_path)
    try:  # the session may run in a subfolder of the copy
        folder = gitutil.repo_root(folder)
    except gitutil.GitError:
        pass
    args = ["worktree", "remove", str(folder)]
    if force:
        args.insert(2, "--force")
    gitutil.git(repo, *args)


def has_uncommitted(folder: Path) -> bool:
    return bool(gitutil.git(folder, "status", "--porcelain", check=False).strip())
