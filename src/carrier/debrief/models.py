# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Core data models shared by every pipeline stage.

Everything a review needs is captured in a single :class:`Review` object that
is saved as JSON (``.carrier/reviews/<id>/review.json``). Keeping the whole
state in one plain file means:

* a review can be closed and resumed later,
* contributors can inspect exactly what each stage produced,
* a different front end (e.g. a web UI) can read the same file.

Each dataclass has an explicit ``from_dict`` rather than relying on a generic
deserialiser — it is a little more code, but it is obvious and easy to
change when fields are added.

Glossary
--------
Hunk
    One ``@@`` block of a unified diff.
FileChange
    All hunks for one file, plus whether it was added/deleted/renamed.
EditEvent
    One moment in a Claude Code session where Claude edited a file, together
    with what the user had asked for at that point and what Claude said it
    was doing. This is the evidence for "why was this changed?".
ChangeItem
    A *logical* change the human reviews — one or more hunks that belong
    together (e.g. "add retry to the loader" spanning two files).
Flag
    A reason, raised by a criteria agent, for the human to look closely.
Decision
    What the human decided about an item.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Diff types are shared with other Carrier tools, so they live in core.
from carrier.core.diffmodel import FileChange, Hunk

# Severity levels, highest first. Used for ranking and display.
SEVERITIES: tuple[str, ...] = ("high", "medium", "low")

# The actions a reviewer can take on an item.
#   approve – fine as is
#   fix     – needs changes; ``Decision.instruction`` says what
#   revert  – should be undone
#   skip    – looked at, no opinion (not carried into the fix list)
ACTIONS: tuple[str, ...] = ("approve", "fix", "revert", "skip")


@dataclass
class EditEvent:
    session_id: str
    timestamp: str  # ISO-8601, as written by Claude Code
    path: str  # repo-relative path that was edited
    tool: str  # "Edit", "Write", "MultiEdit", ...
    user_request: str  # the most recent thing the human asked for
    assistant_note: str  # what Claude said right before making the edit
    snippet: str = ""  # start of the new text Claude wrote (used for matching)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EditEvent":
        return cls(**d)


@dataclass
class Flag:
    criterion: str  # criterion name, e.g. "scope-creep"
    severity: str  # one of SEVERITIES
    reason: str  # one or two sentences for the human
    evidence: str = ""  # quote / line reference backing the reason
    confidence: float = 0.5  # 0..1, as reported by the agent

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Flag":
        return cls(**d)


@dataclass
class Decision:
    action: str  # one of ACTIONS
    instruction: str = ""  # for "fix"/"revert": what should change, or why
    decided_at: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Decision":
        return cls(**d)


@dataclass
class Selection:
    """Lines of code the reviewer highlighted to ask about.

    ``side`` says which version the line numbers refer to: "new" (the code
    after the change), "old" (before), or "both" — a unified-view highlight
    that mixes removed and added lines. For "both", ``start``/``end`` are the
    new-file lines, ``old_start``/``old_end`` the old-file lines, and
    ``text`` keeps the diff markers (``-``/``+``/space) so nothing the
    reviewer highlighted is lost. ``text`` is always exactly what the
    reviewer saw.
    """

    path: str
    side: str  # "old" | "new" | "both"
    start: int  # first line number, 1-based (new file for "both")
    end: int  # last line number (inclusive)
    text: str
    old_start: int | None = None  # only for side == "both"
    old_end: int | None = None

    @staticmethod
    def _span(a: int | None, b: int | None) -> str:
        if a is None:
            return "—"
        return f"{a}" if a == b else f"{a}–{b}"

    def label(self) -> str:
        if self.side == "both":
            return (f"{self.path} lines {self._span(self.old_start, self.old_end)} before → "
                    f"{self._span(self.start, self.end)} after the change")
        version = "before" if self.side == "old" else "after"
        return f"{self.path} lines {self._span(self.start, self.end)} ({version} the change)"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Selection":
        return cls(**d)


@dataclass
class ChatTurn:
    """One question/answer exchange between reviewer and guide."""

    question: str
    answer: str
    selection: Selection | None = None  # code the question was about, if any

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatTurn":
        d = dict(d)
        d["selection"] = Selection.from_dict(d["selection"]) if d.get("selection") else None
        return cls(**d)


@dataclass
class ChangeItem:
    id: str  # e.g. "c1"
    title: str
    summary: str
    hunk_ids: list[str]
    paths: list[str]
    events: list[EditEvent] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    # Filled lazily by the guide agent the first time the item is opened.
    # Keys: what, intent, intent_source, before, why, why_source, attention
    explanation: dict[str, Any] | None = None
    chat: list[ChatTurn] = field(default_factory=list)
    reviewer_notes: list[str] = field(default_factory=list)  # also appended to .carrier/notes.md
    decision: Decision | None = None
    # Flight Deck tasks whose sessions edited this item's files in the
    # reviewed range: [{"id", "title", "session"}]. Empty outside Carrier projects.
    tasks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def top_severity(self) -> str | None:
        for sev in SEVERITIES:
            if any(f.severity == sev for f in self.flags):
                return sev
        return None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChangeItem":
        d = dict(d)
        d["events"] = [EditEvent.from_dict(e) for e in d.get("events", [])]
        d["flags"] = [Flag.from_dict(f) for f in d.get("flags", [])]
        d["chat"] = [ChatTurn.from_dict(c) for c in d.get("chat", [])]
        d["decision"] = Decision.from_dict(d["decision"]) if d.get("decision") else None
        return cls(**d)


@dataclass
class Review:
    id: str  # timestamp-based, e.g. "20261106-142233"
    created_at: str
    repo: str  # absolute path of the repository
    base: str  # commit SHA the review starts from
    target: str  # snapshot commit SHA of exactly what is reviewed
    fingerprint: str  # hash of base+diff, used to reuse a prepared review
    files: list[FileChange] = field(default_factory=list)
    items: list[ChangeItem] = field(default_factory=list)
    notes_used: list[str] = field(default_factory=list)  # note file paths
    criteria_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # shown to the user
    # The checkpoint when this review was prepared. If it differs at finish
    # time, another review finished in between and this one is stale.
    checkpoint_at_prepare: str | None = None
    status: str = "prepared"  # "prepared" | "in_progress" | "done"
    # Carrier project reviews (all empty/None for a plain folder review):
    project_id: int | None = None
    project_name: str = ""
    # The project's extra folders, reviewed with the main one (``repo``):
    # [{"label", "repo", "base", "target", "checkpoint_at_prepare"}]. Their
    # files appear as "<label>/<path>" in ``files`` and items.
    parts: list[dict[str, Any]] = field(default_factory=list)
    # Ready Room context (goal, rules, tasks in this range) given to the agents.
    context: str = ""
    # Whose checkpoints this review moves: the project id, or None for a
    # plain folder review (see gitutil.checkpoint_ref).
    checkpoint_ns: str | None = None

    # -- helpers -----------------------------------------------------------

    def hunk(self, hunk_id: str) -> Hunk | None:
        for f in self.files:
            for h in f.hunks:
                if h.id == hunk_id:
                    return h
        return None

    def item_hunks(self, item: ChangeItem) -> list[Hunk]:
        return [h for hid in item.hunk_ids if (h := self.hunk(hid)) is not None]

    def item_diff(self, item: ChangeItem) -> str:
        """Unified-diff text for one item, grouped by file."""
        out: list[str] = []
        current = None
        for h in self.item_hunks(item):
            if h.path != current:
                # Standard git header: highlighted as a heading, familiar to models.
                out.append(f"diff --git a/{h.path} b/{h.path}")
                current = h.path
            out.append(h.text())
        return "\n".join(out)

    def all_parts(self) -> list[dict[str, Any]]:
        """Every reviewed folder, the main one (label "") first."""
        main = {"label": "", "repo": self.repo, "base": self.base, "target": self.target,
                "checkpoint_at_prepare": self.checkpoint_at_prepare}
        return [main, *self.parts]

    def item(self, item_id: str) -> ChangeItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    @property
    def decided_count(self) -> int:
        return sum(1 for i in self.items if i.decision is not None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Review":
        d = dict(d)
        d["files"] = [FileChange.from_dict(f) for f in d.get("files", [])]
        d["items"] = [ChangeItem.from_dict(i) for i in d.get("items", [])]
        return cls(**d)
