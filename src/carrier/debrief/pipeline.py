# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""The *prepare* stage: turn "what changed" into a ready-to-walk review.

This is the orchestrator. Read :func:`prepare` top to bottom to see the whole
pipeline; each step is a call into a focused module::

    1. decide the base       (gitutil: --from, checkpoint, or first-review default)
    2. snapshot the worktree (gitutil.snapshot_worktree)
    3. diff base..snapshot   (gitutil.diff_text / parse_diff)
    4. reuse if unchanged    (store.find_by_fingerprint)
    5. collect notes         (notes.collect_notes)
    6. collect edit events   (transcripts.collect_events)
    7. group into items      (segment.segment)
    8. link events to items  (transcripts.link_events)
    9. run criteria agents   (flagging.run_criteria)
   10. rank and save         (ranking.rank, store.save_review)

Cancelling (the desktop Cancel button, Ctrl+C) is safe at any point: the
review is written only in step 10, and the snapshot pin is removed if the
run stops earlier. The agents' Claude Code processes are cancelled with it.

Passing ``backend=None`` runs the whole pipeline without any model calls
(per-file grouping, no flags). That is what ``--no-ai`` does, and it makes
the deterministic part easy to test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from carrier.core import gitutil
from carrier.core.config import Config, load_config
from carrier.debrief.criteria import Criterion, load_criteria
from carrier.debrief.flagging import run_criteria
from carrier.core.llm import Backend
from carrier.debrief.models import Review
from carrier.core.notes import Note, collect_notes
from carrier.debrief.ranking import rank
from carrier.debrief.segment import segment
from carrier.debrief.store import find_by_fingerprint, is_stale, save_review, supersede
from carrier.debrief.transcripts import (collect_events, find_transcripts, link_events, now_iso, parse_transcript,
                                         user_requests)

Progress = Callable[[str], None]

# Lookback before the base commit when matching transcripts, so an edit
# made moments before a commit is not missed.
TRANSCRIPT_SLACK_SECONDS = 15 * 60


class NothingToReview(Exception):
    """Raised when there are no changes since the base."""


def resolve_base(repo: Path, cfg: Config, from_ref: str | None, ns: str | None = None) -> tuple[str, str]:
    """Return ``(sha, how)`` where ``how`` explains the choice to the user."""
    if from_ref:
        sha = gitutil.resolve(repo, from_ref)
        if not sha:
            raise gitutil.GitError(f"Unknown starting point: {from_ref!r}")
        return sha, f"from {from_ref}"
    if cp := gitutil.get_checkpoint(repo, ns):
        return cp, "since the last review"
    first = cfg.get("review", "first_review_from")
    if sha := gitutil.resolve(repo, first):
        return sha, f"first review — starting from {first} (use --from to choose)"
    if gitutil.has_head(repo):
        # A single-commit repo has no HEAD~1: review everything.
        return gitutil.EMPTY_TREE, "first review — whole repository"
    return gitutil.EMPTY_TREE, "first review — no commits yet"


def load_notes(cfg: Config) -> tuple[list[Note], list[str]]:
    return collect_notes(cfg)


TaskLookup = Callable[[str, "float | None"], list[dict]]


def _label_of(path: str, labels: set[str]) -> str:
    head = path.split("/", 1)[0]
    return head if "/" in path and head in labels else ""


async def prepare(
    repo: Path,
    backend: Backend | None,
    from_ref: str | None = None,
    reuse: bool = True,
    progress: Progress = lambda _msg: None,
    *,
    extras: list[tuple[str, Path]] | None = None,
    project: dict | None = None,
    context: str = "",
    criteria: list[Criterion] | None = None,
    task_lookup: TaskLookup | None = None,
) -> Review:
    """Prepare a review of ``repo`` (plus a Carrier project's extra folders).

    ``extras`` are ``(label, folder)`` pairs: each is snapshotted and diffed
    like the main folder, always since its own last review, and its files
    are named ``<label>/<path>``. ``context`` (the project's goal, rules and
    tasks) is given to every agent as a note. ``criteria`` replaces the
    file-based criteria (Ready Room manages them for projects).
    ``task_lookup(path, since)`` names the Flight Deck tasks that edited a
    file, so each item can say which task it came from.
    """
    cfg = load_config(repo)
    # A project keeps its own checkpoints (see gitutil.checkpoint_ref).
    ns = str(project["id"]) if project and project.get("id") is not None else None

    # 1–3. base, snapshot and diff, for every folder
    parts: list[dict] = []
    for label, root, ref in [("", repo, from_ref)] + [(lbl, Path(r), None) for lbl, r in extras or []]:
        part_cfg = cfg if not label else load_config(root)
        base, how = resolve_base(root, part_cfg, ref, ns)
        progress(f"Reviewing changes {how}." if not label else f"Folder {label}: changes {how}.")
        target = gitutil.snapshot_worktree(root)
        parts.append({"label": label, "repo": root, "base": base, "target": target,
                      "raw": gitutil.diff_text(root, base, target),
                      "checkpoint_at_prepare": gitutil.get_checkpoint(root, ns)})
    if not any(pt["raw"].strip() for pt in parts):
        main_how = resolve_base(repo, cfg, from_ref, ns)[1]
        raise NothingToReview(f"No changes {main_how}.")

    # 4. reuse an identical prepared review
    fingerprint = hashlib.sha256("\n".join(f"{pt['label']}\n{pt['base']}\n{pt['raw']}" for pt in parts)
                                 .encode()).hexdigest()[:16]
    existing = find_by_fingerprint(cfg, fingerprint) if reuse else None
    if existing and not is_stale(cfg, existing):
        progress("Already prepared — reusing it.")
        return existing

    # Parse every folder's diff; extra folders' paths get their label, and
    # hunk ids are numbered across all folders so they stay unique.
    files = []
    counter = 0
    for pt in parts:
        for f in gitutil.parse_diff(pt["raw"]):
            if pt["label"]:
                f.path = f"{pt['label']}/{f.path}"
                f.old_path = f"{pt['label']}/{f.old_path}" if f.old_path else None
            for h in f.hunks:
                counter += 1
                h.id = f"h{counter}"
                if pt["label"]:
                    h.path = f"{pt['label']}/{h.path}"
            files.append(f)
    progress(f"{len(files)} file(s), {sum(len(f.hunks) for f in files)} hunk(s) changed.")

    # 5–6. notes and Claude Code edit events
    notes, warnings = collect_notes(cfg)
    if context.strip():
        notes.insert(0, Note(path="carrier:project", text=context.strip()))
    since_by_label: dict[str, float | None] = {}
    events = []
    for pt in parts:
        base_time = gitutil.commit_time(pt["repo"], pt["base"])
        since = base_time - TRANSCRIPT_SLACK_SECONDS if base_time else None
        since_by_label[pt["label"]] = since
        if not pt["label"]:
            events += collect_events(repo, since)
            continue
        # Sessions usually run in the main folder and reach into the extra
        # one, so read both folders' transcripts, relative to the extra folder.
        for t in sorted(set(find_transcripts(repo)) | set(find_transcripts(pt["repo"]))):
            for e in parse_transcript(t, pt["repo"], since):
                e.path = f"{pt['label']}/{e.path}"
                events.append(e)
    events.sort(key=lambda e: e.timestamp)
    requests = user_requests(events)
    progress(f"{len(notes)} note file(s); {len(events)} Claude Code edit(s) on record.")
    if not events:
        warnings.append("No Claude Code session record found for this range — "
                        "the guide will say when a reason is unknown.")

    # 7. group into items
    progress("Grouping changes…")
    items, w = await segment(files, requests, cfg, backend)
    warnings += w

    review_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 2
    while (cfg.reviews_dir / review_id).exists():  # two prepares in one second
        review_id = f"{review_id.split('_')[0]}_{n}"
        n += 1

    main = parts[0]
    review = Review(
        id=review_id,
        created_at=now_iso(),
        repo=str(repo),
        base=main["base"],
        target=main["target"],
        fingerprint=fingerprint,
        files=files,
        items=items,
        notes_used=[n.path for n in notes],
        warnings=warnings,
        checkpoint_at_prepare=main["checkpoint_at_prepare"],
        checkpoint_ns=ns,
        project_id=(project or {}).get("id"),
        project_name=(project or {}).get("name", ""),
        parts=[{"label": pt["label"], "repo": str(pt["repo"]), "base": pt["base"], "target": pt["target"],
                "checkpoint_at_prepare": pt["checkpoint_at_prepare"]} for pt in parts[1:]],
        context=context.strip(),
    )
    for pt in parts:
        gitutil.keep_snapshot(pt["repo"], review.id, pt["target"])
    try:
        # 8. link events (and Flight Deck tasks) to items
        link_events(review, events)
        if task_lookup is not None:
            labels = {pt["label"] for pt in parts if pt["label"]}
            for item in review.items:
                seen: dict[int, dict] = {}
                for path in item.paths:
                    for t in task_lookup(path, since_by_label.get(_label_of(path, labels))):
                        seen.setdefault(t["id"], t)
                item.tasks = list(seen.values())
        # 9. criteria agents
        if backend is not None:
            chosen = criteria if criteria is not None else load_criteria(cfg)
            review.criteria_used = [c.name for c in chosen]
            progress(f"Running {len(chosen)} criteria over {len(items)} item(s)…")
            review.warnings += await run_criteria(review, chosen, requests, notes, cfg, backend)
        review.items = rank(review.items)
    except BaseException:
        # Cancelled (Cancel button / Ctrl+C) or failed before anything was
        # saved: unpin the snapshots so nothing is left behind.
        for pt in parts:
            gitutil.drop_snapshot(pt["repo"], review.id)
        raise
    # 10. save
    save_review(cfg, review)  # the only write — a cancelled prepare saves nothing
    supersede(cfg, keep_id=review.id)
    flagged = sum(1 for i in review.items if i.flags)
    progress(f"Ready: {len(review.items)} item(s), {flagged} flagged.")
    return review
