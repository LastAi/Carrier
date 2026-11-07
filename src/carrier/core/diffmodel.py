# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Diff data types shared by every Carrier tool.

``Hunk`` is one ``@@`` block of a unified diff; ``FileChange`` is all hunks
for one file. Produced by :func:`carrier.core.gitutil.parse_diff`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Hunk:
    id: str  # stable within a review, e.g. "h3"
    path: str  # path in the *new* tree (old path for deletions)
    header: str  # the "@@ -a,b +c,d @@ context" line
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # body lines incl. +/-/space prefix

    @property
    def added(self) -> int:
        return sum(1 for ln in self.lines if ln.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for ln in self.lines if ln.startswith("-"))

    def text(self) -> str:
        """The hunk as unified-diff text (header + body)."""
        return "\n".join([self.header, *self.lines])

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Hunk":
        return cls(**d)


@dataclass
class FileChange:
    path: str
    old_path: str | None  # set for renames
    status: str  # "added" | "modified" | "deleted" | "renamed"
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileChange":
        d = dict(d)
        d["hunks"] = [Hunk.from_dict(h) for h in d.get("hunks", [])]
        return cls(**d)
