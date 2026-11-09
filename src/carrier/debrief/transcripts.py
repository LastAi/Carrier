# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Read Claude Code session transcripts to answer "why was this changed?".

Where transcripts live
----------------------
Claude Code writes one JSONL file per session to::

    <config dir>/projects/<project key>/<session id>.jsonl

``<config dir>`` is ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``. ``<project key>``
is the project path with every non-alphanumeric character replaced by
``-`` (e.g. ``/home/alex/app`` -> ``-home-alex-app``; ``C:\\Code\\app`` ->
``C--Code-app``). We ask the Claude Agent SDK for the key when available,
because it knows the exact rule (including hashing of very long paths), and
fall back to scanning every project folder for sessions whose recorded
``cwd`` is inside this repo.

What we extract
---------------
Each line of a transcript is a JSON object. We only need three kinds:

* ``type == "user"`` with text content -> what the human asked for,
* ``type == "assistant"`` text blocks -> what Claude said it was doing,
* ``type == "assistant"`` ``tool_use`` blocks named Edit/Write/MultiEdit/
  NotebookEdit -> an actual file edit.

Every edit becomes an :class:`~debrief.models.EditEvent` carrying the latest
user request and Claude's latest remark. The format is undocumented and may
change, so parsing is deliberately forgiving: unknown lines are skipped,
never fatal.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from carrier.debrief.models import ChangeItem, EditEvent, Review

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Long text is trimmed so prompts stay small; the guide can always read more.
MAX_REQUEST_CHARS = 1200
MAX_NOTE_CHARS = 800
MAX_SNIPPET_CHARS = 400


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def project_key(repo: Path) -> str:
    """Folder name Claude Code uses for ``repo`` under ``projects/``."""
    try:
        from claude_agent_sdk import project_key_for_directory

        return project_key_for_directory(str(repo))
    except Exception:  # SDK missing or API changed — use the documented rule
        return re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(repo))


def _norm(p: str | Path) -> str:
    """Normalise a path for *comparison only* (lower-cased on Windows).

    Never use the result as a displayed or stored path: on Windows it loses
    the file name's real case and would no longer match git's paths.
    """
    return os.path.normcase(os.path.realpath(str(p)))


def _cwd_inside(cwd: str | None, repo: Path) -> bool:
    if not cwd:
        return False
    c, r = _norm(cwd), _norm(repo)
    return c == r or c.startswith(r.rstrip(os.sep) + os.sep)


def find_transcripts(repo: Path) -> list[Path]:
    """All transcript files that belong to ``repo``.

    Includes sessions started in a *subfolder* of the repo: Claude Code keys
    those by the subfolder path, e.g. ``-home-alex-app-src``, which starts
    with the repo's key. Prefix matches are confirmed by the recorded ``cwd``
    so a sibling such as ``-home-alex-app-old`` is not picked up.
    """
    root = projects_dir()
    if not root.is_dir():
        return []
    key = project_key(repo)
    found: list[Path] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if d.name == key:
            found.extend(d.glob("*.jsonl"))
        elif d.name.startswith(key + "-"):
            found.extend(f for f in d.glob("*.jsonl") if _cwd_inside(_first_cwd(f), repo))
    if not found:
        # Key rule may differ (e.g. hashed long paths): fall back to cwd matching.
        found = [f for f in root.glob("*/*.jsonl") if _cwd_inside(_first_cwd(f), repo)]
    return sorted(set(found))


def _first_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(20), fh):
                try:
                    cwd = json.loads(line).get("cwd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def _parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _user_text(content: Any) -> str | None:
    """Text the *human* typed, or None for tool results / system noise."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    # Slash-command plumbing and hook output are not requests.
    if not text or text.startswith(("<command-", "<local-command", "Caveat:")):
        return None
    return text


def _rel(repo: Path, file_path: str) -> str | None:
    """Repo-relative POSIX path, or None if the edit was outside the repo."""
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        # realpath without normcase keeps the file name's case; relpath itself
        # compares case-insensitively on Windows.
        return Path(os.path.relpath(os.path.realpath(p), os.path.realpath(repo))).as_posix()
    except ValueError:  # different drive on Windows
        return None


def _snippet(tool: str, inp: dict[str, Any]) -> str:
    if tool == "MultiEdit":
        text = "\n".join(e.get("new_string", "") for e in inp.get("edits", []) if isinstance(e, dict))
    else:
        text = inp.get("new_string") or inp.get("content") or inp.get("new_source") or ""
    return str(text)[:MAX_SNIPPET_CHARS]


def parse_transcript(path: Path, repo: Path, since: float | None) -> list[EditEvent]:
    """Extract edit events from one transcript, keeping those after ``since``."""
    events: list[EditEvent] = []
    last_request = ""
    last_note = ""
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return events
    with fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                # Sidechains are sub-agent conversations; their edits are
                # still reflected in the main thread's tool calls.
                continue
            msg = entry.get("message") or {}
            kind = entry.get("type")
            if kind == "user" and not entry.get("isMeta"):
                text = _user_text(msg.get("content"))
                if text:
                    last_request = text[:MAX_REQUEST_CHARS]
                    last_note = ""
            elif kind == "assistant":
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text", "").strip():
                        last_note = block["text"].strip()[:MAX_NOTE_CHARS]
                    elif block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                        inp = block.get("input") or {}
                        rel = _rel(repo, inp.get("file_path") or inp.get("notebook_path") or "")
                        ts = entry.get("timestamp", "")
                        t = _parse_ts(ts)
                        if rel is None or rel.startswith("..") or (since and t and t < since):
                            continue
                        events.append(
                            EditEvent(
                                session_id=entry.get("sessionId", path.stem),
                                timestamp=ts,
                                path=rel,
                                tool=block["name"],
                                user_request=last_request,
                                assistant_note=last_note,
                                snippet=_snippet(block["name"], inp),
                            )
                        )
    return events


def collect_events(repo: Path, since: float | None) -> list[EditEvent]:
    """All edit events for ``repo`` after unix time ``since`` (None = all)."""
    events: list[EditEvent] = []
    for f in find_transcripts(repo):
        # Skip files untouched since the cutoff — cheap and big win on old repos.
        if since and f.stat().st_mtime < since:
            continue
        events.extend(parse_transcript(f, repo, since))
    events.sort(key=lambda e: e.timestamp)
    return events


def user_requests(events: Iterable[EditEvent]) -> list[str]:
    """Distinct user requests in chronological order."""
    seen: list[str] = []
    for e in events:
        if e.user_request and e.user_request not in seen:
            seen.append(e.user_request)
    return seen


# --------------------------------------------------------------------------
# Linking events to change items
# --------------------------------------------------------------------------

MAX_EVENTS_PER_ITEM = 4


def _overlap(snippet: str, added_lines: set[str]) -> int:
    lines = {ln.strip() for ln in snippet.splitlines() if len(ln.strip()) > 3}
    return len(lines & added_lines)


def link_events(review: Review, events: list[EditEvent]) -> None:
    """Attach the most relevant edit events to each item (in place).

    An event is a candidate for an item when it edited one of the item's
    files. Candidates are ranked by how many of the lines Claude wrote appear
    in the item's added lines, then by recency.
    """
    # Keys use normcase so that on Windows "src/MyMod.java" (git) matches an
    # edit recorded as "src/mymod.java"; elsewhere normcase is a no-op.
    by_path: dict[str, list[EditEvent]] = {}
    for e in events:
        by_path.setdefault(os.path.normcase(e.path), []).append(e)

    for item in review.items:
        added = {
            ln[1:].strip()
            for h in review.item_hunks(item)
            for ln in h.lines
            if ln.startswith("+") and len(ln[1:].strip()) > 3
        }
        candidates = [e for p in item.paths for e in by_path.get(os.path.normcase(p), [])]
        candidates.sort(key=lambda e: (_overlap(e.snippet, added), e.timestamp), reverse=True)
        item.events = candidates[:MAX_EVENTS_PER_ITEM]


def describe_events(item: ChangeItem, include_rationale: bool = True) -> str:
    """Format an item's events for a prompt.

    ``include_rationale=False`` hides Claude's own explanations and keeps only
    the human's requests. Some criteria use this so the reviewer agent judges
    the change against what was *asked*, not against the author's
    justification (which tends to be persuasive).
    """
    own = [t for t in item.tasks if not t.get("other_project")]
    others = [t for t in item.tasks if t.get("other_project")]
    lines = []
    if getattr(item, "folder_note", ""):
        lines.append("About the folder: " + item.folder_note)
    if own:
        lines.append("Carrier task(s) of this project whose sessions edited these files: "
                     + "; ".join(repr(t["title"]) for t in own))
    if others:
        lines.append("Edited by another project's session: "
                     + "; ".join(f"{t['other_project']} — {t['title']!r}" for t in others)
                     + ". That is the other project's work in a shared folder, not a change this project made or "
                       "asked for: don't judge it against this project's requests.")
    tasks = "\n".join(lines)
    if not item.events:
        none = "(no Claude Code session record for these files — possibly edited by hand)"
        return f"{tasks}\n{none}" if tasks else none
    parts = [tasks] if tasks else []
    for e in item.events:
        part = f"- [{e.timestamp} {e.tool} {e.path}] user asked: {e.user_request!r}"
        if include_rationale and e.assistant_note:
            part += f"\n  Claude said: {e.assistant_note!r}"
        parts.append(part)
    return "\n".join(parts)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
