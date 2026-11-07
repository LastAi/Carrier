# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Order items so the reviewer sees what matters first.

Rules, in order:

1. Within an item, keep one flag per criterion (the most severe, then the
   most confident) — duplicates across batches add nothing.
2. Flagged items come first, by severity score (high=3, medium=2, low=1),
   with a small bonus for each extra criterion that agrees.
3. Ties keep the original order, which follows the diff (file order), so
   related changes stay near each other.
4. Unflagged items follow at the end — they are reviewed quickly but are
   never hidden.

Item IDs are renumbered afterwards so "c1" is always the first to review.
"""

from __future__ import annotations

from carrier.debrief.models import SEVERITIES, ChangeItem, Flag

WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}


def dedupe_flags(flags: list[Flag]) -> list[Flag]:
    best: dict[str, Flag] = {}
    for f in flags:
        cur = best.get(f.criterion)
        if cur is None or (WEIGHT[f.severity], f.confidence) > (WEIGHT[cur.severity], cur.confidence):
            best[f.criterion] = f
    return sorted(best.values(), key=lambda f: (SEVERITIES.index(f.severity), -f.confidence))


def score(item: ChangeItem) -> float:
    if not item.flags:
        return 0.0
    top = max(WEIGHT[f.severity] for f in item.flags)
    return top + 0.25 * (len(item.flags) - 1)


def rank(items: list[ChangeItem]) -> list[ChangeItem]:
    for item in items:
        item.flags = dedupe_flags(item.flags)
    order = sorted(enumerate(items), key=lambda pair: (-score(pair[1]), pair[0]))
    ranked = [item for _, item in order]
    for n, item in enumerate(ranked, start=1):
        item.id = f"c{n}"
    return ranked
