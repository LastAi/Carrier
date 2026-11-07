# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Turn diff hunks into display rows, for unified and side-by-side views.

Both front ends (terminal and desktop) render the same rows, so line
numbering, pairing of changed lines and selection all behave identically.

Row kinds
---------
``file``     a file header (path in ``new``)
``hunk``     the ``@@`` line, shown as a separator
``context``  unchanged line — same text on both sides
``change``   a removed line paired with the added line that replaced it
``del``      a removed line with no replacement
``add``      an added line with nothing removed opposite it
``note``     git's "\\ No newline at end of file" and similar

Side-by-side pairing: inside a hunk, each run of ``-`` lines followed by a
run of ``+`` lines is zipped together line by line (the common heuristic in
review tools). Leftovers become ``del`` or ``add`` rows.

Unified view uses the same Row type, one row per diff line (``change`` is
never produced there), so selections map to line numbers the same way.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import zip_longest

from carrier.debrief.models import ChangeItem, Hunk, Review, Selection


@dataclass
class Row:
    kind: str
    old_no: int | None = None
    old: str | None = None
    new_no: int | None = None
    new: str | None = None
    path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _flush(minus: list[tuple[int, str]], plus: list[tuple[int, str]], path: str) -> list[Row]:
    rows = []
    for m, p in zip_longest(minus, plus):
        if m and p:
            rows.append(Row("change", m[0], m[1], p[0], p[1], path))
        elif m:
            rows.append(Row("del", m[0], m[1], None, None, path))
        elif p:
            rows.append(Row("add", None, None, p[0], p[1], path))
    minus.clear()
    plus.clear()
    return rows


def hunk_rows_split(h: Hunk) -> list[Row]:
    rows = [Row("hunk", new=h.header, path=h.path)]
    old_no, new_no = h.old_start, h.new_start
    minus: list[tuple[int, str]] = []
    plus: list[tuple[int, str]] = []
    for line in h.lines:
        tag, text = line[:1], line[1:]
        if tag == "-":
            if plus:  # a new "-" run after "+" lines starts a new block
                rows += _flush(minus, plus, h.path)
            minus.append((old_no, text))
            old_no += 1
        elif tag == "+":
            plus.append((new_no, text))
            new_no += 1
        else:
            rows += _flush(minus, plus, h.path)
            if tag == "\\":
                rows.append(Row("note", new=line, path=h.path))
            else:
                rows.append(Row("context", old_no, text, new_no, text, h.path))
                old_no += 1
                new_no += 1
    rows += _flush(minus, plus, h.path)
    return rows


def hunk_rows_unified(h: Hunk) -> list[Row]:
    rows = [Row("hunk", new=h.header, path=h.path)]
    old_no, new_no = h.old_start, h.new_start
    for line in h.lines:
        tag, text = line[:1], line[1:]
        if tag == "-":
            rows.append(Row("del", old_no, text, None, None, h.path))
            old_no += 1
        elif tag == "+":
            rows.append(Row("add", None, None, new_no, text, h.path))
            new_no += 1
        elif tag == "\\":
            rows.append(Row("note", new=line, path=h.path))
        else:
            rows.append(Row("context", old_no, text, new_no, text, h.path))
            old_no += 1
            new_no += 1
    return rows


def item_rows(review: Review, item: ChangeItem, split: bool) -> list[Row]:
    """All display rows for an item, with a file header before each file."""
    rows: list[Row] = []
    current = None
    make = hunk_rows_split if split else hunk_rows_unified
    for h in review.item_hunks(item):
        if h.path != current:
            rows.append(Row("file", new=h.path, path=h.path))
            current = h.path
        rows += make(h)
    if not rows:
        for p in item.paths:
            rows.append(Row("file", new=p, path=p))
            rows.append(Row("note", new="(binary or metadata-only change — no text diff)", path=p))
    return rows


def selection_from_rows(rows: list[Row], first: int, last: int, side: str | None = None) -> Selection | None:
    """Build a :class:`Selection` from a range of row indexes.

    ``side`` is "old"/"new" for side-by-side views. For the unified view pass
    ``None``; the side is then chosen so nothing highlighted is dropped:

    * only removed lines  -> "old"
    * no removed lines    -> "new"
    * removed AND other lines -> "both" (text keeps ``-``/``+`` markers)

    Only one file is used — a question is about one place in the code. It
    is the first file in the range that has lines on the requested side.
    """
    if first > last:
        first, last = last, first
    chosen = [r for r in rows[max(first, 0): last + 1] if r.kind in ("context", "change", "add", "del")]
    if not chosen:
        return None

    def has_side(r: Row, sd: str) -> bool:
        return (r.new_no if sd == "new" else r.old_no) is not None

    if side is not None:
        path = next((r.path for r in chosen if has_side(r, side)), None)
        if path is None:
            return None
        lines = [(r.new_no if side == "new" else r.old_no, r.new if side == "new" else r.old)
                 for r in chosen if r.path == path and has_side(r, side)]
        return Selection(path=path, side=side, start=lines[0][0], end=lines[-1][0],
                         text="\n".join(t or "" for _, t in lines))

    path = chosen[0].path
    chosen = [r for r in chosen if r.path == path]
    has_del = any(r.kind == "del" for r in chosen)
    has_new = any(r.new_no is not None for r in chosen)
    if not has_new:
        return selection_from_rows(rows, first, last, "old") if path else None
    if not has_del:
        news = [(r.new_no, r.new) for r in chosen]
        return Selection(path=path, side="new", start=news[0][0], end=news[-1][0],
                         text="\n".join(t or "" for _, t in news))
    marked = []
    for r in chosen:
        if r.kind == "del":
            marked.append(f"-{r.old or ''}")
        elif r.kind == "add":
            marked.append(f"+{r.new or ''}")
        else:
            marked.append(f" {r.new or ''}")
    olds = [r.old_no for r in chosen if r.old_no is not None]
    news = [r.new_no for r in chosen if r.new_no is not None]
    return Selection(path=path, side="both", start=news[0], end=news[-1], text="\n".join(marked),
                     old_start=olds[0] if olds else None, old_end=olds[-1] if olds else None)
