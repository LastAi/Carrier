# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Project notes: the record of what the code is *supposed* to do.

Notes are plain files matched by the globs in ``[notes].paths`` (CLAUDE.md,
docs/, …). They are given to the criteria agents (e.g. "does this change
contradict the design notes?") and to the guide (to explain intent).

Sizes are capped so a large docs folder cannot blow up the prompt; anything
truncated or skipped is reported back as a warning instead of failing
silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from carrier.core.config import Config


@dataclass
class Note:
    path: str  # repo-relative, forward slashes
    text: str
    truncated: bool = False


def collect_notes(cfg: Config) -> tuple[list[Note], list[str]]:
    """Return ``(notes, warnings)`` for the repository in ``cfg``."""
    max_file = int(cfg.get("notes", "max_file_kb")) * 1024
    max_total = int(cfg.get("notes", "max_total_kb")) * 1024
    seen: set[Path] = set()
    notes: list[Note] = []
    warnings: list[str] = []
    total = 0

    for pattern in cfg.get("notes", "paths"):
        for path in sorted(cfg.repo.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(cfg.repo).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            truncated = len(text) > max_file
            if truncated:
                text = text[:max_file]
                warnings.append(f"Note {rel} truncated to {max_file // 1024} KB.")
            if total + len(text) > max_total:
                warnings.append(f"Note {rel} skipped: notes size limit reached.")
                continue
            total += len(text)
            notes.append(Note(rel, text, truncated))
    return notes, warnings


def render_notes(notes: list[Note]) -> str:
    """Format notes for a prompt. Each note is fenced and labelled by path,
    which lets the model cite its source as ``notes:<path>``."""
    if not notes:
        return "(no project notes found)"
    parts = [f'<note path="{n.path}">\n{n.text}\n</note>' for n in notes]
    return "\n\n".join(parts)


def append_review_note(cfg: Config, item_title: str, paths: list[str], text: str) -> Path:
    """Append a reviewer note to ``.carrier/notes.md``.

    That file is in the default notes globs, so what the reviewer writes
    becomes context for every future review.
    """
    cfg.notes_file.parent.mkdir(parents=True, exist_ok=True)
    new_file = not cfg.notes_file.exists()
    stamp = datetime.now().strftime("%Y-%m-%d")
    with cfg.notes_file.open("a", encoding="utf-8") as fh:
        if new_file:
            fh.write("# Review notes\n\nNotes written during Debrief reviews.\n")
        fh.write(f"\n## {stamp} — {item_title}\n")
        fh.write(f"Files: {', '.join(paths)}\n\n{text.strip()}\n")
    return cfg.notes_file
