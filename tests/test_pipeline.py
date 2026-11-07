# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""End-to-end prepare → decide → finish, with a scripted fake model."""

import subprocess
from pathlib import Path

import pytest

from carrier.core import gitutil

from carrier.debrief import hooks
from carrier.core.config import load_config
from carrier.debrief.guide import explain
from carrier.core.llm import BackendError, FakeBackend
from carrier.debrief.models import Decision
from carrier.core.notes import collect_notes
from carrier.debrief.pipeline import NothingToReview, prepare
from carrier.debrief.record import finish
from carrier.debrief.store import load_review, review_dir


async def test_full_review_cycle(repo: Path, claude_home: Path, fake_backend: FakeBackend):
    review = await prepare(repo, fake_backend)

    # Grouping: save.py + test deletion grouped; the rest repaired into per-file items.
    first = review.items[0]
    assert first.title == "Add retry to save loading"
    assert set(first.paths) == {"save.py", "tests/test_save.py"}
    assert {p for i in review.items[1:] for p in i.paths} == {"notes.txt", "util.py"}

    # Flagged item ranked first, with the transcript linked.
    assert first.flags and first.flags[0].criterion == "tests-weakened"
    assert first.events and first.events[0].user_request.startswith("Make loading saves retry")

    # Criteria that hide rationale must not see Claude's explanation.
    crit_prompts = [c["prompt"] for c in fake_backend.calls if "review panel" in str(c["system"])]
    scope = next(p for p in crit_prompts if "Unrequested changes" in p)
    assert "I'll add a retry loop" not in scope
    risky = next(p for p in crit_prompts if "hard to undo" in p)
    assert "I'll add a retry loop" in risky

    # Saved and reloadable.
    cfg = load_config(repo)
    saved = load_review(review_dir(cfg, review.id) / "review.json")
    assert saved.items[0].flags[0].reason == first.flags[0].reason

    # Reusing: same changes -> same review, no new model calls.
    n_calls = len(fake_backend.calls)
    again = await prepare(repo, fake_backend)
    assert again.id == review.id and len(fake_backend.calls) == n_calls

    # Guide explanation is normalised and sourced.
    notes, _ = collect_notes(cfg)
    exp = await explain(review, first, notes, cfg, fake_backend)
    assert exp["why_source"] == "request" and exp["questions"]

    # Decide and finish.
    first.decision = Decision(action="fix", instruction="Keep a test for corrupt saves.")
    review.items[1].decision = Decision(action="approve")
    result = finish(cfg, review)
    assert result.checkpoint_moved
    record = result.record.read_text()
    assert "Needs fix" in record and "Not reviewed" in record
    fixes = result.fixes.read_text()
    assert "Keep a test for corrupt saves." in fixes and "```diff" in fixes
    assert gitutil.get_checkpoint(repo) == review.target

    # After finishing, nothing new to review.
    with pytest.raises(NothingToReview):
        await prepare(repo, fake_backend)

    # A new change after the checkpoint shows up alone.
    (repo / "util.py").write_text("X = 2\n")
    nxt = await prepare(repo, None)
    assert [i.paths for i in nxt.items] == [["util.py"]]


async def test_no_ai_mode(repo: Path):
    review = await prepare(repo, None)
    assert all(not i.flags for i in review.items)
    assert len(review.items) == 4  # one per file


async def test_from_ref(repo: Path):
    review = await prepare(repo, None, from_ref="HEAD")
    assert "util.py" not in {p for i in review.items for p in i.paths}  # committed before HEAD
    with pytest.raises(gitutil.GitError):
        await prepare(repo, None, from_ref="no-such-ref")


async def test_failing_backend_degrades_gracefully(repo: Path):
    def broken(system: str, prompt: str) -> str:
        raise BackendError("not logged in")

    review = await prepare(repo, FakeBackend(broken))
    assert len(review.items) == 4  # fell back to per-file grouping
    assert any("grouped by file" in w for w in review.warnings)
    assert sum("criteria failed" in w for w in review.warnings) == 1  # collapsed into one


def test_hook_install_is_idempotent_and_removable(repo: Path):
    path, changed = hooks.install_stop_hook(repo)
    assert changed and path.name == "settings.local.json"
    assert hooks.install_stop_hook(repo)[1] is False
    # The personal settings file is ignored locally, so it never shows up in reviews.
    assert "settings.local.json" in (repo / ".git" / "info" / "exclude").read_text()
    assert hooks.remove_stop_hook(repo) == [path]
    assert "debrief" not in path.read_text()
    # A group shared with another hook: ours is removed, theirs is kept.
    import json
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "echo mine"},
        {"type": "command", "command": hooks.hook_command()}]}]}}))
    assert hooks.remove_stop_hook(repo) == [path]
    left = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"]
    assert left == [{"type": "command", "command": "echo mine"}]
    # A hook from another Debrief install (e.g. the compiled app) is updated, not duplicated.
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": '"C:/Debrief/debrief-cli.exe" prepare --background --quiet'}]}]}}))
    assert hooks.install_stop_hook(repo)[1] is True
    cmds = [h["command"] for g in json.loads(path.read_text())["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds == [hooks.hook_command()]


def test_lock(repo: Path):
    import os
    import time
    cfg = load_config(repo)
    with hooks.held_lock(cfg) as ok:
        assert ok
        with hooks.held_lock(cfg) as second:
            assert not second  # already held
    assert not hooks.lock_path(cfg).exists()
    # A lock owned by someone else is never removed by us…
    hooks.lock_path(cfg).write_text("999999")
    hooks.release_lock(cfg)
    assert hooks.lock_path(cfg).exists()
    # …but a stale one (no heartbeat) is taken over.
    old = time.time() - hooks.LOCK_STALE_SECONDS - 5
    os.utime(hooks.lock_path(cfg), (old, old))
    assert hooks.acquire_lock(cfg)
    hooks.release_lock(cfg)


async def test_stale_review_never_moves_checkpoint_backwards(repo: Path):
    """Review A is left open; B is prepared and finished; finishing A must not
    move the checkpoint back to A's older snapshot."""
    cfg = load_config(repo)
    a = await prepare(repo, None)
    a.status = "in_progress"
    from carrier.debrief.store import save_review
    save_review(cfg, a)
    (repo / "util.py").write_text("X = 99\n")
    b = await prepare(repo, None, reuse=False)
    assert finish(cfg, b).checkpoint_moved
    assert load_review(review_dir(cfg, a.id) / "review.json").status == "superseded"
    result = finish(cfg, a)
    assert not result.checkpoint_moved
    assert gitutil.get_checkpoint(repo) == b.target


async def test_snapshot_survives_gc(repo: Path):
    cfg = load_config(repo)
    review = await prepare(repo, None)
    review.status = "in_progress"  # opened in the UI…
    from carrier.debrief.store import save_review
    save_review(cfg, review)
    (repo / "util.py").write_text("X = 5\n")
    await prepare(repo, None)  # …then the Stop hook prepares again, moving refs/carrier/debrief/pending
    subprocess.run(["git", "gc", "--prune=now", "-q"], cwd=repo, check=True)
    assert finish(cfg, review).checkpoint_moved
