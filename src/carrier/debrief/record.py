# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Finishing a review: the record, the fix list, and the checkpoint.

Two files are written next to ``review.json``:

``record.md``
    A readable account of the review — every item, its flags, the guide's
    explanation, questions asked and the reviewer's decision. Worth
    committing: it is the project's memory of *why* changes were accepted.

``fixes.md``
    Only written when something needs work. It is phrased as a prompt for
    Claude Code, so ``debrief fixes --run`` (or pasting it) starts the next
    task directly.

Finishing also moves the checkpoint to the review's snapshot, so the next
review starts exactly where this one ended — unless the review is *stale*
(another review finished after it was prepared), in which case the
checkpoint is left alone so it can never move backwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from carrier.core import gitutil
from carrier.core.config import Config
from carrier.debrief.guide import render_explanation
from carrier.debrief.models import ChangeItem, Review
from carrier.debrief.store import OPEN_STATUSES, is_stale, review_dir, save_review, supersede

ACTION_LABEL = {"approve": "✅ Approved", "fix": "🔧 Needs fix", "revert": "↩️ Revert",
                "skip": "⏭ Skipped", None: "— Not reviewed"}


def _item_record(review: Review, item: ChangeItem) -> str:
    d = item.decision
    lines = [f"### {item.id}. {item.title}", "",
             f"**Decision:** {ACTION_LABEL[d.action if d else None]}  ",
             f"**Files:** {', '.join(item.paths)}", ""]
    if item.tasks:
        lines += [f"**From task:** {', '.join(t['title'] for t in item.tasks)}", ""]
    if item.summary:
        lines += [item.summary, ""]
    if d and d.instruction:
        lines += [f"**Instruction:** {d.instruction}", ""]
    for note in item.reviewer_notes:
        lines += [f"**Reviewer note:** {note}", ""]
    if item.flags:
        lines.append("**Flags:**")
        lines += [f"- `{f.severity}` **{f.criterion}** — {f.reason}" for f in item.flags]
        lines.append("")
    if item.explanation:
        lines += ["<details><summary>Guide's explanation</summary>", "",
                  render_explanation(item.explanation), "", "</details>", ""]
    if item.chat:
        lines += ["<details><summary>Questions</summary>", ""]
        for t in item.chat:
            about = f" _(about {t.selection.label()})_" if t.selection else ""
            lines += [f"**Q:**{about} {t.question}", "", t.answer, ""]
        lines += ["</details>", ""]
    return "\n".join(lines)


def render_record(review: Review) -> str:
    counts: dict[str, int] = {}
    for i in review.items:
        key = i.decision.action if i.decision else "undecided"
        counts[key] = counts.get(key, 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    head = [
        f"# Review {review.id}" + (f" — {review.project_name}" if review.project_name else ""),
        "",
        f"- **Range:** `{review.base[:10]}` → snapshot `{review.target[:10]}`",
        *[f"- **Folder {p['label']}** (`{p['repo']}`): `{p['base'][:10]}` → `{p['target'][:10]}`"
          for p in review.parts],
        f"- **Items:** {len(review.items)} ({summary})",
        f"- **Criteria:** {', '.join(review.criteria_used) or 'none (no-AI mode)'}",
        f"- **Notes read:** {', '.join(review.notes_used) or 'none'}",
        "",
    ]
    if review.warnings:
        head += ["**Warnings:**", *[f"- {w}" for w in review.warnings], ""]
    return "\n".join(head + ["## Items", ""] + [_item_record(review, i) for i in review.items])


def render_fixes(review: Review) -> str | None:
    """A Claude Code prompt listing every fix/revert, or None if there are none."""
    todo = [i for i in review.items if i.decision and i.decision.action in ("fix", "revert")]
    if not todo:
        return None
    out = [
        "A human reviewed your recent changes and asked for the follow-ups below.",
        "Work through them in order. For each one, make the change, then briefly",
        "say what you did. Do not make other changes.",
        "",
    ]
    if review.parts:
        out += ["Files named `<folder>/…` are in this project's extra folders:",
                *[f"- `{p['label']}/` = {p['repo']}" for p in review.parts], ""]
    for n, item in enumerate(todo, start=1):
        d = item.decision
        assert d is not None
        verb = "Revert" if d.action == "revert" else "Fix"
        out.append(f"## {n}. {verb}: {item.title}")
        out.append(f"Files: {', '.join(item.paths)}")
        if d.action == "revert":
            out.append("Undo this change (restore the previous behaviour):")
        if d.instruction:
            out.append(f"Instruction: {d.instruction}")
        out += ["", "The change as reviewed:", "```diff", review.item_diff(item), "```", ""]
    return "\n".join(out)


@dataclass
class FinishResult:
    record: Path
    fixes: Path | None
    checkpoint_moved: bool
    message: str  # human-readable note about the checkpoint


def finish(cfg: Config, review: Review) -> FinishResult:
    """Move the checkpoint, write record (and fixes), mark done.

    Order matters: the checkpoint is moved *first*, so a git failure leaves
    the review open instead of "done but checkpoint not moved".
    """
    # All folders or none: check every one first (and undo on failure below),
    # so a broken extra folder can't leave the checkpoints half-moved.
    parts = review.all_parts()
    for part in parts:
        if not gitutil.is_repo(Path(part["repo"])):
            raise gitutil.GitError(f"{part['repo']} is missing or no longer a git repository; "
                                   "fix it (or remove it from the project), then finish again.")
    moved = not is_stale(cfg, review)
    if moved:
        done: list[tuple[Path, str | None]] = []
        try:
            for part in parts:
                repo = Path(part["repo"])
                previous = gitutil.resolve(repo, gitutil.checkpoint_ref(review.checkpoint_ns))
                gitutil.set_checkpoint(repo, part["target"], review.checkpoint_ns)
                done.append((repo, previous))
        except gitutil.GitError:
            for repo, previous in done:
                try:
                    if previous:
                        gitutil.set_checkpoint(repo, previous, review.checkpoint_ns)
                    else:
                        gitutil.git(repo, "update-ref", "-d", gitutil.checkpoint_ref(review.checkpoint_ns),
                                    check=False)
                except gitutil.GitError:
                    pass
            raise
        message = (f"Checkpoint moved to snapshot {review.target[:10]}." if not review.parts else
                   f"Checkpoints moved in all {len(review.all_parts())} folders.")
    else:
        # Another review finished after this one was prepared. Moving the
        # checkpoint here would send it *backwards* and re-show that work.
        message = ("Checkpoint not moved: another review finished after this one was "
                   "prepared, so this review's range is out of date.")
        review.warnings.append(message)
    folder = review_dir(cfg, review.id)
    folder.mkdir(parents=True, exist_ok=True)
    review.status = "done"
    record_path = folder / "record.md"
    record_path.write_text(render_record(review), encoding="utf-8")
    fixes_path = None
    if (fixes := render_fixes(review)) is not None:
        fixes_path = folder / "fixes.md"
        fixes_path.write_text(fixes, encoding="utf-8")
    save_review(cfg, review)
    for part in review.all_parts():
        try:
            gitutil.drop_snapshot(Path(part["repo"]), review.id)  # the checkpoint keeps it reachable now
        except gitutil.GitError:
            pass  # folder gone: nothing to unpin
    if moved:
        supersede(cfg, keep_id=review.id, statuses=OPEN_STATUSES)
    return FinishResult(record_path, fixes_path, moved, message)
