# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Saving and loading reviews (``.carrier/reviews/<id>/review.json``).

Writes are atomic (write to a temp file, then rename) so a crash or Ctrl-C
during a save can never leave a half-written review behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from carrier.core.config import Config
from carrier.debrief.models import Review

REVIEW_FILE = "review.json"


def review_dir(cfg: Config, review_id: str) -> Path:
    return cfg.reviews_dir / review_id


def save_review(cfg: Config, review: Review) -> Path:
    folder = review_dir(cfg, review.id)
    folder.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(cfg)
    path = folder / REVIEW_FILE
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".review-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(review.to_dict(), fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_review(path: Path) -> Review:
    with path.open(encoding="utf-8") as fh:
        return Review.from_dict(json.load(fh))


def all_reviews(cfg: Config) -> list[Review]:
    """Every saved review, oldest first. Unreadable files are skipped."""
    out = []
    if cfg.reviews_dir.is_dir():
        for p in sorted(cfg.reviews_dir.glob(f"*/{REVIEW_FILE}")):
            try:
                out.append(load_review(p))
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return out


# Review lifecycle:
#   prepared    – built (often by the Stop hook), not opened yet
#   in_progress – opened in the UI; decisions may exist
#   done        – finished; record written, checkpoint moved
#   superseded  – a newer prepare replaced it before anyone opened it
OPEN_STATUSES = ("prepared", "in_progress")


def open_review(cfg: Config, status: str | None = None) -> Review | None:
    """The most recent open review (optionally with a specific status)."""
    wanted = (status,) if status else OPEN_STATUSES
    pending = [r for r in all_reviews(cfg) if r.status in wanted]
    return pending[-1] if pending else None


def find_by_fingerprint(cfg: Config, fingerprint: str) -> Review | None:
    for r in reversed(all_reviews(cfg)):
        if r.fingerprint == fingerprint and r.status in OPEN_STATUSES:
            return r
    return None


def supersede(cfg: Config, keep_id: str, statuses: tuple[str, ...] = ("prepared",)) -> None:
    """Mark other reviews in ``statuses`` as superseded and unpin their snapshots.

    After a prepare only never-opened reviews are superseded; after a finish
    *all* other open reviews are, because their base is now out of date.
    """
    from carrier.core.gitutil import GitError, drop_snapshot  # local import: store stays git-free otherwise

    for r in all_reviews(cfg):
        if r.id != keep_id and r.status in statuses:
            r.status = "superseded"
            save_review(cfg, r)
            for part in r.all_parts():
                try:
                    drop_snapshot(Path(part["repo"]), r.id)
                except GitError:
                    pass  # that folder is gone: nothing left to unpin


def is_stale(cfg: Config, review: Review) -> bool:
    """True if a checkpoint (of any reviewed folder) moved since ``review`` was prepared."""
    from carrier.core.gitutil import GitError, get_checkpoint

    ns = review.checkpoint_ns
    if get_checkpoint(cfg.repo, ns) != review.checkpoint_at_prepare:
        return True
    for part in review.parts:
        try:
            if get_checkpoint(Path(part["repo"]), ns) != part["checkpoint_at_prepare"]:
                return True
        except GitError:
            return True  # the folder is gone or no longer a repository
    return False


def _ensure_gitignore(cfg: Config) -> None:
    """Keep machine-local files out of git; reviews themselves may be committed."""
    gi = cfg.data_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("# Machine-local Debrief files\n*.log\n*.lock\n", encoding="utf-8")
