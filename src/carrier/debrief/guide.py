# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""The guide agent: explains each item and answers the reviewer's questions.

The guide is the only agent the reviewer talks to directly. For each item it
produces a structured explanation with five parts — what it does, what it
was supposed to do, what was there before, why it changed, and what to look
at — each tagged with its *source* (see ``prompts.GUIDE_SYSTEM``).

The guide is stateless: every question re-sends the item, the earlier
explanation and the conversation so far. That keeps resuming a review
trivial (everything is in review.json) at the cost of a slightly longer
prompt, which is fine for single-item conversations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from carrier.core.config import Config
from carrier.core.jsonutil import extract_json
from carrier.core.llm import READ_ONLY_TOOLS, Backend
from carrier.debrief.models import ChangeItem, Review, Selection
from carrier.core.notes import Note, render_notes
from carrier.debrief.prompts import GUIDE_CHAT_PROMPT, GUIDE_EXPLAIN_PROMPT, GUIDE_SELECTION_BLOCK, GUIDE_SYSTEM
from carrier.debrief.transcripts import describe_events

MAX_DIFF_CHARS = 30_000

EXPLANATION_FIELDS = ("what", "intent", "intent_source", "before", "why", "why_source", "attention")


def _diff(review: Review, item: ChangeItem) -> str:
    d = review.item_diff(item)
    return d if len(d) <= MAX_DIFF_CHARS else d[:MAX_DIFF_CHARS] + "\n... (truncated — use Read for more)"


def _flags(item: ChangeItem) -> str:
    if not item.flags:
        return "(none)"
    return "\n".join(f"- [{f.severity}] {f.criterion}: {f.reason} (evidence: {f.evidence})" for f in item.flags)


def normalise_explanation(raw: Any, fallback_text: str) -> dict[str, Any]:
    """Make sure every expected key exists, whatever the model returned."""
    if not isinstance(raw, dict):
        return {"what": fallback_text, "intent": "", "intent_source": "unknown", "before": "",
                "why": "", "why_source": "unknown", "attention": "", "questions": []}
    out: dict[str, Any] = {k: str(raw.get(k, "") or "") for k in EXPLANATION_FIELDS}
    out["intent_source"] = out["intent_source"] or "unknown"
    out["why_source"] = out["why_source"] or "unknown"
    qs = raw.get("questions", [])
    out["questions"] = [str(q) for q in qs][:3] if isinstance(qs, list) else []
    return out


async def explain(review: Review, item: ChangeItem, notes: list[Note], cfg: Config,
                  backend: Backend) -> dict[str, Any]:
    prompt = GUIDE_EXPLAIN_PROMPT.format(
        item_id=item.id,
        title=item.title,
        summary=item.summary,
        diff=_diff(review, item),
        events=describe_events(item, include_rationale=True),
        notes=render_notes(notes),
        flags=_flags(item),
    )
    reply = await backend.ask(
        system=GUIDE_SYSTEM,
        prompt=prompt,
        model=cfg.get("models", "guide"),
        tools=READ_ONLY_TOOLS,
        cwd=Path(review.folder),
        add_dirs=[review.part_folder(pt) for pt in review.parts],
        max_turns=int(cfg.get("agents", "guide_max_turns")),
    )
    try:
        return normalise_explanation(extract_json(reply), reply)
    except ValueError:
        return normalise_explanation(None, reply)


def render_explanation(exp: dict[str, Any]) -> str:
    """Explanation as markdown — used by the TUI and the review record."""
    def src(key: str) -> str:
        return f" _[{exp.get(key) or 'unknown'}]_"

    # One compact paragraph per part: "**Label:** text [source]".
    parts = [
        f"**What it does:** {exp.get('what', '')}",
        f"**Supposed to do:** {exp.get('intent', '')}{src('intent_source')}",
        f"**Before:** {exp.get('before', '')}",
        f"**Why it changed:** {exp.get('why', '')}{src('why_source')}",
    ]
    if exp.get("attention"):
        parts.append(f"**⚑ Look at:** {exp['attention']}")
    if exp.get("questions"):
        parts.append("**You might ask:** " + " · ".join(f"_{q}_" for q in exp["questions"]))
    return "\n\n".join(parts)


MAX_SELECTION_CHARS = 8_000


def _selection_block(sel: Selection | None) -> str:
    if sel is None:
        return ""
    text = sel.text if len(sel.text) <= MAX_SELECTION_CHARS else sel.text[:MAX_SELECTION_CHARS] + "\n..."
    label = sel.label()
    if sel.side == "both":
        label += " (diff lines: - removed, + added)"
    return GUIDE_SELECTION_BLOCK.format(label=label, text=text)


async def ask(review: Review, item: ChangeItem, question: str, cfg: Config, backend: Backend,
              selection: Selection | None = None) -> str:
    """Answer a reviewer question, optionally about highlighted lines."""
    def turn(t) -> str:
        about = f" (about {t.selection.label()})" if t.selection else ""
        return f"Reviewer{about}: {t.question}\nGuide: {t.answer}"

    history = "\n\n".join(turn(t) for t in item.chat) or "(none)"
    prompt = GUIDE_CHAT_PROMPT.format(
        item_id=item.id,
        title=item.title,
        diff=_diff(review, item),
        events=describe_events(item, include_rationale=True),
        explanation=render_explanation(item.explanation) if item.explanation else "(not generated)",
        history=history,
        selection=_selection_block(selection),
        question=question,
    )
    return await backend.ask(
        system=GUIDE_SYSTEM,
        prompt=prompt,
        model=cfg.get("models", "guide"),
        tools=READ_ONLY_TOOLS,
        cwd=Path(review.folder),
        add_dirs=[review.part_folder(pt) for pt in review.parts],
        max_turns=int(cfg.get("agents", "guide_max_turns")),
    )
