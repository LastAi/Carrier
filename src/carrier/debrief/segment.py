# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Group diff hunks into logical change items.

Reviewing file-by-file hides intent (one feature often spans several files)
and buries small important edits inside large files. So we ask a fast model
to group hunks by *purpose*. The model's answer is validated:

* unknown hunk IDs are ignored,
* a hunk claimed by two groups stays in the first,
* hunks the model forgot become their own per-file items.

If the model call fails or the diff is huge, :func:`group_by_file` gives a
deterministic grouping instead, so a review can always be produced.
"""

from __future__ import annotations

from carrier.core.config import Config
from carrier.core.jsonutil import extract_json
from carrier.core.llm import Backend, BackendError
from carrier.debrief.models import ChangeItem, FileChange, Hunk
from carrier.debrief.prompts import SEGMENT_PROMPT, SEGMENT_SYSTEM

PREVIEW_LINES = 8  # changed lines shown per hunk in the grouping prompt


def _describe_file(f: FileChange) -> str:
    verb = {"added": "Add", "deleted": "Delete", "renamed": "Rename"}.get(f.status, "Change")
    target = f"{f.old_path} → {f.path}" if f.status == "renamed" and f.old_path else f.path
    return f"{verb} {target}"


def group_by_file(files: list[FileChange]) -> list[ChangeItem]:
    """Deterministic fallback: one item per changed file."""
    items = []
    for n, f in enumerate(files, start=1):
        hunks = [h.id for h in f.hunks]
        detail = "binary file" if f.binary else f"{len(hunks)} hunk(s)"
        items.append(
            ChangeItem(
                id=f"c{n}",
                title=_describe_file(f),
                summary=f"{f.status.capitalize()} {f.path} ({detail}).",
                hunk_ids=hunks,
                paths=[f.path],
            )
        )
    return items


def _hunk_preview(h: Hunk) -> str:
    changed = [ln for ln in h.lines if ln[:1] in "+-"][:PREVIEW_LINES]
    body = "\n    ".join(ln[:160] for ln in changed)
    return f"{h.id} {h.path} (+{h.added}/-{h.removed})\n    {body}"


def _validate(raw: object, files: list[FileChange]) -> list[ChangeItem]:
    """Turn the model's JSON into items, repairing omissions."""
    path_of = {h.id: h.path for f in files for h in f.hunks}
    used: set[str] = set()
    items: list[ChangeItem] = []
    groups = raw.get("items", []) if isinstance(raw, dict) else []
    for g in groups:
        if not isinstance(g, dict):
            continue
        ids = [h for h in g.get("hunks", []) if h in path_of and h not in used]
        if not ids:
            continue
        used.update(ids)
        paths = list(dict.fromkeys(path_of[h] for h in ids))  # ordered unique
        items.append(
            ChangeItem(
                id="",  # numbered below
                title=str(g.get("title") or "Change")[:80],
                summary=str(g.get("summary") or ""),
                hunk_ids=ids,
                paths=paths,
            )
        )
    # Anything the model left out (and binary/no-hunk files) gets its own item.
    for f in files:
        missing = [h.id for h in f.hunks if h.id not in used]
        if missing or not f.hunks:
            items.append(
                ChangeItem(id="", title=_describe_file(f), summary=f"{f.status.capitalize()} {f.path}.",
                           hunk_ids=missing, paths=[f.path])
            )
    for n, item in enumerate(items, start=1):
        item.id = f"c{n}"
    return items


async def segment(
    files: list[FileChange], requests: list[str], cfg: Config, backend: Backend | None
) -> tuple[list[ChangeItem], list[str]]:
    """Return ``(items, warnings)``."""
    hunks = [h for f in files for h in f.hunks]
    if backend is None or not hunks:
        return group_by_file(files), []
    if len(hunks) > int(cfg.get("review", "max_hunks_for_ai_grouping")):
        return group_by_file(files), [f"{len(hunks)} hunks — grouped by file instead of by purpose."]

    prompt = SEGMENT_PROMPT.format(
        requests="\n".join(f"- {r[:300]}" for r in requests) or "(none recorded)",
        hunks="\n".join(_hunk_preview(h) for h in hunks),
    )
    try:
        reply = await backend.ask(system=SEGMENT_SYSTEM, prompt=prompt, model=cfg.get("models", "segment"))
        return _validate(extract_json(reply), files), []
    except (BackendError, ValueError) as exc:
        return group_by_file(files), [f"Could not group changes by purpose ({exc}); grouped by file."]
