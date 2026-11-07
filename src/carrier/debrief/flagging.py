# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Run the criteria agents and attach their flags to items.

Cost model
----------
Naively, N criteria × M items = N·M model calls. Instead each criterion sees
a *batch* of items at once (up to ``[agents].batch_kb`` of diff), so a
typical review costs roughly one call per criterion. All calls run
concurrently, capped by the backend's semaphore.

Independence
------------
Each criterion decides whether it may see Claude's own explanations
(``sees_rationale``). Most built-ins say no: they see only what the *user*
asked for, so they judge the change on its merits instead of being
persuaded by the author's justification.

Failure handling
----------------
A criterion that errors or returns unparseable output contributes no flags
and adds a warning — one broken criterion never blocks a review.
"""

from __future__ import annotations

import asyncio

from carrier.core.config import Config
from carrier.debrief.criteria import Criterion
from carrier.core.jsonutil import extract_json
from carrier.core.llm import READ_ONLY_TOOLS, Backend, BackendError
from carrier.debrief.models import SEVERITIES, ChangeItem, Flag, Review
from carrier.core.notes import Note, render_notes
from carrier.debrief.prompts import CRITERIA_PROMPT, CRITERIA_SYSTEM
from carrier.debrief.transcripts import describe_events

MAX_ITEM_DIFF_CHARS = 12_000  # a single giant item is truncated, not dropped


def _render_item(review: Review, item: ChangeItem, with_rationale: bool) -> str:
    diff = review.item_diff(item)
    if len(diff) > MAX_ITEM_DIFF_CHARS:
        diff = diff[:MAX_ITEM_DIFF_CHARS] + "\n... (diff truncated)"
    return (
        f"### {item.id}: {item.title}\n{item.summary}\n"
        f"Session record:\n{describe_events(item, include_rationale=with_rationale)}\n"
        f"```diff\n{diff}\n```"
    )


def batch_items(review: Review, items: list[ChangeItem], batch_chars: int) -> list[list[ChangeItem]]:
    """Split items into batches whose combined diff fits ``batch_chars``."""
    batches: list[list[ChangeItem]] = []
    current: list[ChangeItem] = []
    size = 0
    for item in items:
        n = min(len(review.item_diff(item)), MAX_ITEM_DIFF_CHARS)
        if current and size + n > batch_chars:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += n
    if current:
        batches.append(current)
    return batches


def _parse_flags(reply: str, criterion: Criterion, valid_ids: set[str]) -> list[tuple[str, Flag]]:
    data = extract_json(reply)
    out = []
    for f in data.get("flags", []) if isinstance(data, dict) else []:
        if not isinstance(f, dict) or f.get("item") not in valid_ids or not f.get("reason"):
            continue
        sev = str(f.get("severity", criterion.severity)).lower()
        try:
            conf = max(0.0, min(1.0, float(f.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        out.append(
            (
                f["item"],
                Flag(
                    criterion=criterion.name,
                    severity=sev if sev in SEVERITIES else criterion.severity,
                    reason=str(f["reason"]),
                    evidence=str(f.get("evidence", "")),
                    confidence=conf,
                ),
            )
        )
    return out


async def _run_one(
    criterion: Criterion,
    batch: list[ChangeItem],
    review: Review,
    requests: list[str],
    notes: list[Note],
    cfg: Config,
    backend: Backend,
) -> tuple[list[tuple[str, Flag]], str | None]:
    prompt = CRITERIA_PROMPT.format(
        title=criterion.title,
        criterion=criterion.prompt,
        requests="\n".join(f"- {r}" for r in requests) or "(no session record)",
        notes=render_notes(notes) if criterion.uses_notes else "(not needed for this criterion)",
        items="\n\n".join(_render_item(review, i, criterion.sees_rationale) for i in batch),
        severity=criterion.severity,
    )
    try:
        reply = await backend.ask(
            system=CRITERIA_SYSTEM,
            prompt=prompt,
            model=criterion.model or cfg.get("models", "criteria"),
            effort=criterion.effort,
            tools=READ_ONLY_TOOLS if criterion.explore else None,
            cwd=cfg.repo,
            add_dirs=[pt["repo"] for pt in review.parts],
            # More effort also lets an exploring agent look further.
            max_turns=EXPLORE_TURNS.get(criterion.effort or "", 6) if criterion.explore else 1,
        )
        return _parse_flags(reply, criterion, {i.id for i in batch}), None
    except (BackendError, ValueError) as exc:
        return [], f"Criterion '{criterion.name}' failed: {exc}"


# Tool-use turns an exploring criterion may take, by effort (default 6).
EXPLORE_TURNS = {"low": 3, "medium": 6, "high": 10, "xhigh": 14, "max": 20}


async def run_criteria(
    review: Review,
    criteria: list[Criterion],
    requests: list[str],
    notes: list[Note],
    cfg: Config,
    backend: Backend,
) -> list[str]:
    """Run every criterion over every item; attach flags. Returns warnings."""
    batches = batch_items(review, review.items, int(cfg.get("agents", "batch_kb")) * 1024)
    jobs = [
        _run_one(c, b, review, requests, notes, cfg, backend) for c in criteria for b in batches
    ]
    results = await asyncio.gather(*jobs)
    if results and all(w for _, w in results):
        # Everything failed — almost always one cause (not logged in, offline).
        # One clear message beats a wall of identical ones.
        first = results[0][1] or ""
        return [f"All criteria failed, so nothing was flagged. First error: "
                f"{first.split(': ', 1)[-1]} — run `debrief doctor`."]
    warnings: list[str] = []
    for flags, warning in results:
        if warning:
            warnings.append(warning)
        for item_id, flag in flags:
            item = review.item(item_id)
            if item is not None:
                item.flags.append(flag)
    return warnings
