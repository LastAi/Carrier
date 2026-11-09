# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""ReviewSession — the one API every front end talks to.

The terminal UI, the desktop app and (later) the optional network server
all drive reviews through this class. Every public method takes and returns
**plain JSON-compatible values** (dicts, lists, strings, numbers). That is
deliberate:

* the desktop app passes them straight across the pywebview JS bridge,
* a future network server can expose the same methods over HTTP without
  any new logic — the server stays a thin, removable layer,
* tests can call it without any UI.

Threading model
---------------
The pipeline and agents are ``async``. Front ends such as pywebview call
methods from worker threads, so the session owns a private event loop on a
background thread and runs coroutines there (:meth:`_run`). Methods are
therefore safe to call from any thread and simply block until done.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import shutil
import threading
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from carrier.core import gitutil, themes, usersettings

from carrier.debrief import guide
from carrier.core.config import Config, load_config
from carrier.core.llm import AgentSDKBackend, Backend, BackendError
from carrier.debrief.models import ChangeItem, ChatTurn, Decision, Review, Selection
from carrier.core.notes import Note, append_review_note, collect_notes
from carrier.debrief.pipeline import NothingToReview, prepare
from carrier.debrief.projects import ProjectSpec, review_matches
from carrier.debrief.record import finish, render_fixes
from carrier.debrief.sidebyside import item_rows, selection_from_rows
from carrier.debrief.store import is_stale, open_review, save_review, supersede

T = TypeVar("T")
Progress = Callable[[str], None]


class SessionError(Exception):
    """A user-facing problem (bad folder, nothing to review, …).

    The message is written for the reviewer, not for a developer.
    """


# --------------------------------------------------------------------------
# Folder / setup helpers (no review needed)
# --------------------------------------------------------------------------


def setup_checks() -> dict[str, Any]:
    """What is installed? Shown on the desktop start screen and by `doctor`."""
    git_ok = shutil.which("git") is not None
    return {
        "git": git_ok,
        "claude_cli": shutil.which("claude") is not None,
        "agent_sdk": importlib.util.find_spec("claude_agent_sdk") is not None,
        "hints": {
            "git": "Install Git from https://git-scm.com/downloads and restart Debrief.",
            "claude": "Install Claude Code (https://claude.com/claude-code), then run `claude` "
                      "once in a terminal to log in.",
        },
    }


def test_claude(backend: Backend | None = None) -> dict[str, Any]:
    """Make one tiny model call to confirm Claude Code works and is logged in."""
    backend = backend or AgentSDKBackend()
    try:
        reply = asyncio.run(backend.ask(system="Reply with one word.", prompt="Say OK.", model="haiku"))
        return {"ok": True, "reply": reply[:40]}
    except BackendError as exc:
        return {"ok": False, "error": str(exc)}


def inspect_folder(path: str, ns: str | None = None) -> dict[str, Any]:
    """Describe a folder for the start screen: is it a repo, its history, any open review."""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise SessionError(f"Folder not found: {path}")
    try:
        root = gitutil.repo_root(p)
    except gitutil.GitError:
        return {"path": str(p), "is_repo": False}
    cfg = load_config(root)
    review = open_review(cfg, ns=ns)
    cp = gitutil.get_checkpoint(root, ns)
    return {
        "path": str(root),
        "is_repo": True,
        "has_commits": gitutil.has_head(root),
        "checkpoint": cp,
        "commits": recent_commits(root),
        "open_review": _review_summary(review) if review and not is_stale(cfg, review) else None,
    }


def recent_commits(repo: Path, limit: int = 25) -> list[dict[str, str]]:
    if not gitutil.has_head(repo):
        return []
    out = gitutil.git(repo, "log", f"-{limit}", "--format=%H%x1f%h%x1f%s%x1f%cr", check=False)
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "short": parts[1], "subject": parts[2], "when": parts[3]})
    return commits


def start_tracking(path: str) -> dict[str, Any]:
    """Turn a plain folder into a git repo with a baseline commit.

    This is the "Start tracking this folder" button: Debrief needs git to
    see what changed. Uses a fallback identity only if the user has none, so
    it works on a fresh machine.
    """
    p = Path(path).expanduser()
    if gitutil.in_repo(p) and not (p / ".git").exists():
        # Already inside a repository (e.g. one folder of a workspace): it's
        # tracked; a second repository nested in the first would hide its files.
        return inspect_folder(str(p))
    if not (p / ".git").exists():
        gitutil.git(p, "init", "-q")
    gitutil.git(p, "add", "-A")
    env = {}
    if not gitutil.git(p, "config", "user.email", check=False).strip():
        env = {"GIT_AUTHOR_NAME": "Carrier", "GIT_AUTHOR_EMAIL": "carrier@localhost",
               "GIT_COMMITTER_NAME": "Carrier", "GIT_COMMITTER_EMAIL": "carrier@localhost"}
    gitutil.git(p, "commit", "-q", "--allow-empty", "-m", "Baseline (created by Carrier)", env=env)
    return inspect_folder(str(p))


def _review_summary(r: Review) -> dict[str, Any]:
    return {
        "id": r.id,
        "status": r.status,
        "items": len(r.items),
        "flagged": sum(1 for i in r.items if i.flags),
        "decided": r.decided_count,
        "base": r.base[:10],
        "target": r.target[:10],
    }


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


class ReviewSession:
    """A review of one folder, or of a Carrier project (``project``: its main
    folder plus extra folders, task-aware, with Ready Room context and agents)."""

    def __init__(self, repo: str | Path, backend: Backend | None | str = "auto",
                 project: ProjectSpec | None = None) -> None:
        self.project = project
        if project is not None:
            repo = project.path
            untracked = [lbl for lbl, path in project.extras if not gitutil.in_repo(path)]
            if untracked:
                raise SessionError("These folders aren't tracked by git yet: " + ", ".join(untracked)
                                   + ". Start tracking them first.")
        try:
            self.repo = gitutil.repo_root(Path(repo).expanduser())
        except gitutil.GitError as exc:
            raise SessionError(f"{repo} is not tracked by git yet.") from exc
        # A project's main folder may be a subfolder of a bigger repository
        # (several projects split out of one workspace): review only it.
        self.scope: Path | None = None
        if project is not None:
            main = Path(project.path).expanduser().resolve()
            if main != self.repo.resolve():
                self.scope = main
        self.cfg: Config = load_config(self.repo)
        if backend == "auto":
            backend = AgentSDKBackend(concurrency=int(self.cfg.get("agents", "concurrency")))
        self.backend: Backend | None = backend  # None = no-AI mode
        self.review: Review | None = None
        self.notes: list[Note] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="debrief-loop")
        self._thread.start()
        self._lock = threading.RLock()  # serialises writes to the review
        self._closed = False
        self._preparing: concurrent.futures.Future | None = None  # running start(), for Cancel
        self._cancel_requested = False
        self._finished_paths: set[str] = set()  # files finish() produced (record, fixes)
        usersettings.remember_repo(str(self.repo))

    # -- plumbing ----------------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._closed:
            coro.close()
            raise SessionError("This review was closed.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError) as exc:  # by close()
            raise SessionError("This review was closed.") from exc

    def close(self) -> None:
        """Cancel running work (e.g. a prefetching guide call), then stop the loop.

        Cancelling matters: callers blocked in _run() get an error instead
        of waiting forever, and the Claude Code subprocesses are shut down.
        """
        if self._closed:
            return
        self._closed = True

        async def _cancel_all() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_cancel_all(), self._loop).result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _need_review(self) -> Review:
        if self.review is None:
            raise SessionError("No review is open.")
        return self.review

    def _need_open_review(self) -> Review:
        """Like _need_review, but refuses changes to a finished review."""
        r = self._need_review()
        if r.status == "done":
            raise SessionError("This review is finished. Start a new one to make changes.")
        return r

    def _item(self, item_id: str) -> ChangeItem:
        item = self._need_review().item(item_id)
        if item is None:
            raise SessionError(f"Unknown item {item_id}")
        return item

    def _save(self) -> None:
        with self._lock:
            save_review(self.cfg, self._need_review())

    # -- starting ----------------------------------------------------------

    def start(self, from_ref: str | None = None, fresh: bool = False,
              progress: Progress = lambda _m: None, scan: bool = False,
              agents: list[str] | None = None) -> dict[str, Any]:
        """Resume the open review, or prepare a new one. Returns the overview.

        ``scan`` reviews the whole project as it is now (no history) with only
        the ``agents`` named (e.g. a security check); it moves no checkpoint.
        """
        if not fresh and not from_ref:
            existing = open_review(self.cfg, status="in_progress",
                                   ns=None if self.project is None else str(self.project.id))
            if existing and existing.scan != scan:
                existing = None  # a scan and a change review don't resume each other
            if existing and self.project is not None and not review_matches(
                    existing, self.project.id, self.project.extras):
                existing = None  # prepared for other folders: prepare a full one
            if existing and is_stale(self.cfg, existing):
                supersede(self.cfg, keep_id="", statuses=("in_progress",), ns=existing.checkpoint_ns)
                existing = None
            if existing:
                progress(f"Resuming review {existing.id}.")
                return self._open(existing)
        if self._closed:
            raise SessionError("This review was closed.")
        self._cancel_requested = False
        p = self.project
        extra_args: dict[str, Any] = {} if p is None else {
            "extras": p.extras, "project": {"id": p.id, "name": p.name}, "context": p.context,
            "criteria": p.criteria, "task_lookup": p.task_lookup, "scope": self.scope,
            "folder_notes": p.folder_notes, "keep_file": p.keep_file}
        if scan:
            from carrier.debrief.criteria import load_criteria

            pool = p.criteria if p is not None and p.criteria is not None else load_criteria(self.cfg)
            chosen = [c for c in pool if c.name in set(agents or [])]
            if not chosen:
                raise SessionError("Pick at least one review agent for the scan.")
            extra_args.update(scan=True, criteria=chosen)
        future = asyncio.run_coroutine_threadsafe(
            prepare(self.repo, self.backend, from_ref, reuse=not fresh, progress=progress, **extra_args),
            self._loop)
        self._preparing = future
        try:
            review = future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError) as exc:
            raise SessionError("Cancelled — nothing was saved. You can start again any time.") from exc
        except NothingToReview as exc:
            raise SessionError(f"{exc} Nothing to review.") from exc
        except gitutil.GitError as exc:
            raise SessionError(str(exc)) from exc
        finally:
            self._preparing = None
        if self._cancel_requested:  # Cancel arrived just as preparing finished
            raise SessionError("Cancelled — nothing was saved. You can start again any time.")
        return self._open(review)

    def cancel_start(self) -> bool:
        """Stop a running :meth:`start` (the Cancel button). Safe from any thread.

        Cancels the prepare task, which also cancels the agents' Claude Code
        calls. Returns True if something was running.
        """
        self._cancel_requested = True
        future = self._preparing
        if future is None or future.done():
            return False
        future.cancel()
        return True

    def _open(self, review: Review) -> dict[str, Any]:
        self.review = review
        self.notes, _ = collect_notes(self.cfg)
        if review.context:
            self.notes.insert(0, Note(path="carrier:project", text=review.context))
        review.status = "in_progress"
        self._save()
        return self.overview()

    # -- reading -----------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        r = self._need_review()
        return {
            **_review_summary(r),
            "repo": str(self.repo),
            "project": r.project_name or None,
            "folders": [p["label"] for p in r.parts],
            "ai": self.backend is not None,
            "warnings": r.warnings,
            "criteria": r.criteria_used,
            "items": [
                {
                    "id": i.id,
                    "title": i.title,
                    "paths": i.paths,
                    "severity": i.top_severity,
                    "flags": len(i.flags),
                    "decision": i.decision.action if i.decision else None,
                    "tasks": [t["title"] + (f" ({t['other_project']})" if t.get("other_project") else "")
                              for t in i.tasks],
                }
                for i in r.items
            ],
        }

    def item(self, item_id: str, split: bool = False) -> dict[str, Any]:
        r = self._need_review()
        it = self._item(item_id)
        return {
            "id": it.id,
            "title": it.title,
            "summary": it.summary,
            "paths": it.paths,
            "tasks": it.tasks,
            "flags": [vars(f) for f in it.flags],
            "explanation": it.explanation,
            "chat": [
                {"question": t.question, "answer": t.answer,
                 "selection": vars(t.selection) if t.selection else None,
                 "selection_label": t.selection.label() if t.selection else None}
                for t in it.chat
            ],
            "notes": it.reviewer_notes,
            "decision": vars(it.decision) if it.decision else None,
            "has_session_record": bool(it.events),
            "rows": [row.to_dict() for row in item_rows(r, it, split)],
            "split": split,
        }

    # -- guide ---------------------------------------------------------------

    def explain(self, item_id: str) -> dict[str, Any] | None:
        """Generate (or return the cached) explanation. None in no-AI mode."""
        it = self._item(item_id)
        if it.explanation is not None or self.backend is None:
            return it.explanation
        exp = self._run(guide.explain(self._need_review(), it, self.notes, self.cfg, self.backend))
        with self._lock:
            it.explanation = exp
        self._save()
        return exp

    def selection(self, item_id: str, split: bool, first_row: int, last_row: int,
                  side: str | None = None) -> dict[str, Any] | None:
        """Turn a range of displayed rows into a Selection (for preview)."""
        rows = item_rows(self._need_review(), self._item(item_id), split)
        sel = selection_from_rows(rows, first_row, last_row, side)
        return {**vars(sel), "label": sel.label()} if sel else None

    def ask(self, item_id: str, question: str, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.backend is None:
            raise SessionError("The guide is off in no-AI mode.")
        question = question.strip()
        if not question:
            raise SessionError("Type a question first.")
        it = self._item(item_id)
        sel = None
        if selection:
            keys = ("path", "side", "start", "end", "text", "old_start", "old_end")
            sel = Selection.from_dict({k: selection.get(k) for k in keys})
        try:
            answer = self._run(guide.ask(self._need_review(), it, question, self.cfg, self.backend, sel))
        except BackendError as exc:
            raise SessionError(f"The guide could not answer: {exc}") from exc
        with self._lock:
            it.chat.append(ChatTurn(question=question, answer=answer, selection=sel))
        self._save()
        return {"question": question, "answer": answer,
                "selection": vars(sel) if sel else None,
                "selection_label": sel.label() if sel else None}

    # -- decisions -----------------------------------------------------------

    def decide(self, item_id: str, action: str, instruction: str = "") -> dict[str, Any]:
        if action not in ("approve", "fix", "revert", "skip", "clear"):
            raise SessionError(f"Unknown action: {action}")
        if action == "fix" and not instruction.strip():
            raise SessionError("Say what should change, so Claude Code knows what to fix.")
        it = self._item(item_id)
        with self._lock:
            self._need_open_review()  # checked under the lock: finish() can't slip in between
            it.decision = None if action == "clear" else Decision(
                action=action, instruction=instruction.strip(),
                decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._save()
        return self.overview()

    def add_note(self, item_id: str, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise SessionError("The note is empty.")
        it = self._item(item_id)
        with self._lock:
            self._need_open_review()
            append_review_note(self.cfg, it.title, it.paths, text)
            it.reviewer_notes.append(text)
        self._save()
        return {"notes": it.reviewer_notes}

    # -- finishing -------------------------------------------------------------

    def finish(self) -> dict[str, Any]:
        with self._lock:
            # Status is checked inside the lock so two concurrent finishes
            # (a double click) cannot both run.
            r = self._need_open_review()
            try:
                result = finish(self.cfg, r)
            except gitutil.GitError as exc:
                raise SessionError(f"Couldn't finish: {exc}") from exc
            self._finished_paths = {str(result.record)} | ({str(result.fixes)} if result.fixes else set())
            self._last_fixes = result.fixes
        fix_task = None
        if result.fixes and self.project is not None and self.project.add_fix_task is not None:
            # Project reviews hand the fixes back as the next task in the queue.
            n = sum(1 for i in r.items if i.decision and i.decision.action in ("fix", "revert"))
            fix_task = self.project.add_fix_task(f"Fix review findings ({n}) — review {r.id}",
                                                 render_fixes(r) or "", r.id)
        return {
            "fix_task": fix_task,
            "project": self.project.name if self.project else None,
            "record": str(result.record),
            "fixes": str(result.fixes) if result.fixes else None,
            "fixes_text": render_fixes(r),
            "checkpoint_moved": result.checkpoint_moved,
            "message": result.message,
            "counts": {a: sum(1 for i in r.items if i.decision and i.decision.action == a)
                       for a in ("approve", "fix", "revert", "skip")},
            "undecided": len(r.items) - r.decided_count,
        }

    def launch_fixes(self) -> dict[str, Any]:
        """Open a terminal running Claude Code on the fix list from finish().

        Takes no path on purpose: the page cannot point this at any other file.
        """
        from carrier.core.terminal import launch_claude

        fixes = getattr(self, "_last_fixes", None)
        if not fixes:
            raise SessionError("There is no fix list for this review.")
        return launch_claude(self.repo, Path(fixes))

    def is_finished_file(self, path: str) -> bool:
        """True only for the record/fixes files this session's finish() wrote."""
        try:
            return str(Path(path).resolve()) in {str(Path(p).resolve()) for p in self._finished_paths}
        except OSError:
            return False


# --------------------------------------------------------------------------
# Settings (shared by all front ends)
# --------------------------------------------------------------------------


def get_settings() -> dict[str, Any]:
    s = usersettings.load()
    return {**s, "theme_resolved": themes.resolve(s["theme"], s["custom_colors"]),
            "presets": themes.list_presets(), "color_keys": themes.KEYS}


def set_settings(changes: dict[str, Any]) -> dict[str, Any]:
    usersettings.update(**changes)
    return get_settings()

