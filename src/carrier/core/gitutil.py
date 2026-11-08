# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Git helpers: snapshotting the working tree and parsing diffs.

Key idea — the review *snapshot*
--------------------------------
Claude Code usually leaves work uncommitted. To review a stable, exact state
we capture the working tree (tracked **and** untracked, honouring
.gitignore) as a commit object *without touching the user's index, working
tree or branches*:

1. ``git add -A`` into a throwaway index file (``GIT_INDEX_FILE``),
2. ``git write-tree`` to store that tree,
3. ``git commit-tree`` to wrap it in a commit whose parent is HEAD.

The diff for the review is then ``base..snapshot``. Because the snapshot is
fixed, edits made while the review is open do not shift the hunks under the
reviewer's feet. When the review finishes, the checkpoint ref points at the
snapshot, so the next review shows only what changed *after* it.

Nothing here talks to a model; it is all deterministic and unit-tested.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from carrier.core.diffmodel import FileChange, Hunk

# Git's well-known empty tree. Used as the base when a repo has one commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Carrier's per-project data folder, excluded from snapshots and diffs.
CARRIER_PATH = ".carrier"

# Identity used for snapshot commits, so it works even if user.name is unset.
_SNAPSHOT_ENV = {
    "GIT_AUTHOR_NAME": "carrier",
    "GIT_AUTHOR_EMAIL": "carrier@localhost",
    "GIT_COMMITTER_NAME": "carrier",
    "GIT_COMMITTER_EMAIL": "carrier@localhost",
}


class GitError(RuntimeError):
    """A git command failed. The message includes git's stderr."""


def git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Run ``git <args>`` in ``repo`` and return stdout (text, UTF-8)."""
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=repo,
            env=full_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:  # the folder is gone (or git isn't installed)
        raise GitError(f"git {' '.join(args)} could not run in {repo}: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    """Return the top-level directory of the repository containing ``start``."""
    try:
        return Path(git(start, "rev-parse", "--show-toplevel").strip())
    except (GitError, FileNotFoundError) as exc:
        raise GitError(f"{start} is not inside a git repository") from exc


def is_repo(folder: Path) -> bool:
    """True if ``folder`` is the top level of a git repository."""
    try:
        return Path(folder).is_dir() and repo_root(Path(folder)).resolve() == Path(folder).resolve()
    except GitError:
        return False


def in_repo(folder: Path) -> bool:
    """True if ``folder`` is inside a git repository (its top folder or below)."""
    try:
        return Path(folder).is_dir() and bool(repo_root(Path(folder)))
    except GitError:
        return False


def locate(folder: Path) -> tuple[Path, str]:
    """``(repository top folder, folder's POSIX path inside it)``; "" for the top itself.

    A Carrier folder may be a subfolder of a bigger repository (several
    projects split out of one workspace); its reviews are scoped to it.
    """
    root = repo_root(Path(folder))
    rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(root))
    rel = "" if rel == "." else Path(rel).as_posix()
    if rel.startswith(".."):
        raise GitError(f"{folder} is not inside {root}")
    return root, rel


def resolve(repo: Path, ref: str) -> str | None:
    """Resolve ``ref`` to a full SHA, or ``None`` if it does not exist."""
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
    return out or None


def has_head(repo: Path) -> bool:
    return resolve(repo, "HEAD") is not None


def commit_time(repo: Path, sha: str) -> int | None:
    """Committer time (unix seconds) of ``sha``; ``None`` for the empty tree."""
    if sha == EMPTY_TREE:
        return None
    out = git(repo, "show", "-s", "--format=%ct", sha, check=False).strip()
    return int(out) if out.isdigit() else None


def snapshot_worktree(repo: Path, keep_ref: str = "refs/carrier/debrief/pending") -> str:
    """Capture the full working tree as a commit and return its SHA.

    Does not modify the real index, working tree, or any branch. See the
    module docstring for why this exists.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {**_SNAPSHOT_ENV, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if has_head(repo):
            # Start from HEAD's tree so unchanged files are cheap to add.
            git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "-A", env=env)
        # Carrier's own files (reviews, logs) are never part of what is reviewed.
        git(repo, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", CARRIER_PATH, env=env)
        tree = git(repo, "write-tree", env=env).strip()
    parents = ["-p", "HEAD"] if has_head(repo) else []
    sha = git(
        repo, "commit-tree", tree, *parents, "-m", "carrier debrief snapshot", env=_SNAPSHOT_ENV
    ).strip()
    # Keep the object reachable so `git gc` cannot remove it. The pipeline
    # later pins it under a per-review ref (see keep_snapshot).
    git(repo, "update-ref", keep_ref, sha)
    return sha


def review_ref(review_id: str) -> str:
    return f"refs/carrier/debrief/reviews/{review_id}"


def keep_snapshot(repo: Path, review_id: str, sha: str) -> None:
    """Pin a review's snapshot so it survives `git gc` while the review is open.

    Without this, the next prepare (e.g. from the Stop hook) would move
    refs/carrier/debrief/pending and leave an open review's snapshot unreferenced.
    """
    git(repo, "update-ref", review_ref(review_id), sha)


def drop_snapshot(repo: Path, review_id: str) -> None:
    """Unpin a finished/superseded review's snapshot (the checkpoint keeps the
    finished one reachable)."""
    git(repo, "update-ref", "-d", review_ref(review_id), check=False)


def diff_text(repo: Path, base: str, target: str, sub: str = "") -> str:
    """Raw unified diff between two commits (rename detection on).

    ``sub`` limits it to one folder of the repository, with paths relative to
    that folder (see :func:`locate`).

    Read as bytes and decoded without newline translation: text mode would
    turn a lone ``\r`` inside a line into a line break and corrupt the diff.
    """
    scope = [f"--relative={sub}"] if sub else []
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "-M",
         "-U3", *scope, base, target, "--", sub or ".", f":(exclude){CARRIER_PATH}"],
        cwd=repo, capture_output=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git diff failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Unified-diff parser
# --------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f",
            "r": "\r", '"': '"', "\\": "\\"}


def _unquote(p: str) -> str:
    """Undo git's C-style quoting (used for paths with tabs, quotes, \\ …).

    Octal escapes are UTF-8 bytes, so they are collected and decoded together.
    """
    if not (len(p) >= 2 and p.startswith('"') and p.endswith('"')):
        return p
    body, out = p[1:-1], bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1:i + 4]
            if len(octal) == 3 and all(ch in "01234567" for ch in octal):
                out.append(int(octal, 8))
                i += 4
                continue
            out += _ESCAPES.get(nxt, nxt).encode()
            i += 2
            continue
        out += c.encode()
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_prefix(p: str) -> str | None:
    """'a/foo.py' -> 'foo.py'; '/dev/null' -> None; handles quoted paths."""
    p = p.rstrip("\n")
    if p.endswith("\t"):  # git may append a tab after paths containing spaces
        p = p[:-1]
    if p == "/dev/null":
        return None
    p = _unquote(p)
    return p[2:] if p[:2] in ("a/", "b/") else p


def _header_path(line: str) -> str:
    """Path from a ``diff --git a/X b/Y`` header.

    Splitting on " b/" is ambiguous when a path itself contains " b/". For
    non-renames X == Y, so the header is ``a/P b/P`` and P is exactly the
    first half. Renames are corrected later from the ``rename to`` line.
    """
    rest = line[len("diff --git "):]
    if rest.startswith('"'):  # quoted: "a/x" "b/y"
        end = rest.find('" ', 1)
        while end != -1 and rest[end - 1] == "\\":
            end = rest.find('" ', end + 1)
        if end != -1:
            return _strip_prefix(rest[end + 2:]) or ""
    # "a/" + P + " b/" + P  ->  len(rest) == 2 * len(P) + 5
    if rest.startswith("a/") and (len(rest) - 5) % 2 == 0:
        n = (len(rest) - 5) // 2
        p = rest[2:2 + n]
        if rest[2 + n:] == f" b/{p}":
            return p
    m = re.match(r"a/(.*?) b/(.*)$", rest)
    return m.group(2) if m else rest


def parse_diff(text: str) -> list[FileChange]:
    """Parse ``git diff`` output into :class:`FileChange` objects.

    Hunk IDs are assigned sequentially across the whole diff ("h1", "h2", …)
    so that later stages (and the model) can refer to them compactly.
    """
    files: list[FileChange] = []
    cur: FileChange | None = None
    hunk: Hunk | None = None
    counter = 0

    # Split on "\n" only: str.splitlines() also breaks on form feeds and
    # other separators that can legitimately appear inside a source line.
    for line in text.split("\n"):
        line = line[:-1] if line.endswith("\r") else line  # CRLF files
        if line.startswith("diff --git "):
            # Path from the header; refined by rename/---/+++ lines below.
            cur = FileChange(path=_header_path(line), old_path=None, status="modified")
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            # File-level metadata between "diff --git" and the first "@@".
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.old_path = _unquote(line[len("rename from "):])
                cur.status = "renamed"
            elif line.startswith("rename to "):
                cur.path = _unquote(line[len("rename to "):])
            elif line.startswith("Binary files "):
                cur.binary = True
            elif line.startswith("--- "):
                old = _strip_prefix(line[4:])
                if old and cur.status == "renamed":
                    cur.old_path = old
            elif line.startswith("+++ "):
                new = _strip_prefix(line[4:])
                if new:
                    cur.path = new
        m = _HUNK_RE.match(line)
        if m:
            counter += 1
            hunk = Hunk(
                id=f"h{counter}",
                path=cur.path,
                header=line,
                old_start=int(m.group(1)),
                old_len=int(m.group(2) or 1),
                new_start=int(m.group(3)),
                new_len=int(m.group(4) or 1),
            )
            cur.hunks.append(hunk)
            continue
        if hunk is not None and (line[:1] in ("+", "-", " ") or line.startswith("\\")):
            hunk.lines.append(line)
    return files


def checkpoint_ref(ns: str | None = None) -> str:
    """Name of the ref that marks "reviewed up to here".

    ``ns`` gives a Carrier project its own checkpoint, so two projects that
    both review a folder (one's main folder is the other's extra folder)
    don't move each other's. ``None`` is the plain folder checkpoint.
    """
    return "refs/carrier/debrief/last" if not ns else f"refs/carrier/debrief/projects/{ns}/last"


def get_checkpoint(repo: Path, ns: str | None = None) -> str | None:
    """The checkpoint; a project without its own yet starts from the folder's."""
    if ns and (sha := resolve(repo, checkpoint_ref(ns))):
        return sha
    return resolve(repo, checkpoint_ref())


def set_checkpoint(repo: Path, sha: str, ns: str | None = None) -> None:
    git(repo, "update-ref", checkpoint_ref(ns), sha)
