# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Drive the Textual UI headlessly: open, read, ask, decide, finish."""

from pathlib import Path

from textual.widgets import Input

from carrier.core.config import load_config
from carrier.core.llm import FakeBackend
from carrier.core.notes import collect_notes
from carrier.debrief.pipeline import prepare
from carrier.debrief.store import load_review, review_dir
from carrier.debrief.tui.app import DebriefApp, PromptScreen


async def _app(repo: Path, backend):
    review = await prepare(repo, backend)
    cfg = load_config(repo)
    notes, _ = collect_notes(cfg)
    return DebriefApp(review, cfg, notes, backend), review, cfg


async def test_walkthrough(repo: Path, claude_home: Path, fake_backend: FakeBackend):
    app, review, cfg = await _app(repo, fake_backend)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        first = review.items[0]
        assert app.current is first
        assert first.explanation and first.explanation["what"].startswith("Retries")

        # Ask a question.
        await pilot.press("slash")
        assert isinstance(app.focused, Input)
        for ch in "why sleep?":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert first.chat and "sleeps 1s" in first.chat[0].answer

        # Back to the list; mark fix with an instruction.
        app.query_one("#items").focus()
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        for ch in "add test":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause()
        assert first.decision and first.decision.action == "fix"
        assert first.decision.instruction == "add test"
        assert app.current is review.items[1]  # moved to next undecided

        await pilot.press("a")  # approve item 2
        await pilot.press("s")  # skip item 3
        await pilot.press("n")  # note on the last item (stays selected once all are decided)
        await pilot.pause()
        for ch in "remember":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert (cfg.notes_file).read_text().count("remember") == 1

        await pilot.press("F")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert app.return_value is True
    saved = load_review(review_dir(cfg, review.id) / "review.json")
    assert saved.items[0].decision.action == "fix"
    assert saved.items[-1].reviewer_notes == ["remember"]


async def test_quit_saves_and_no_ai_mode(repo: Path):
    app, review, cfg = await _app(repo, None)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        await pilot.press("q")
    assert app.return_value is False
    saved = load_review(review_dir(cfg, review.id) / "review.json")
    assert saved.status == "in_progress" and saved.items[0].decision.action == "approve"


async def test_cancelled_revert_records_nothing_and_prefetch_is_bounded(
        repo: Path, claude_home: Path, fake_backend: FakeBackend):
    for i in range(6):  # more items, to see whether prefetch runs away
        (repo / f"extra{i}.py").write_text(f"v = {i}\n")
    app, review, cfg = await _app(repo, fake_backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        explained = sum(1 for i in review.items if i.explanation)
        assert explained <= 2, f"prefetch chained through {explained} items"
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert review.items[0].decision is None
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")  # revert with no reason is allowed
        await pilot.pause()
        assert review.items[0].decision.action == "revert"


async def test_highlight_lines_and_ask_split_and_theme(repo: Path, claude_home: Path,
                                                       fake_backend: FakeBackend):
    from carrier.core import usersettings
    from carrier.debrief.tui.diffview import DiffView

    app, review, cfg = await _app(repo, fake_backend)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        diff = app.query_one(DiffView)
        diff.focus()
        await pilot.press("shift+down", "shift+down")
        sel = diff.selection
        assert sel is not None and sel.path == "save.py"
        assert "Asking about" in str(app.query_one("#sel-label").render())
        await pilot.press("slash")
        for ch in "explain":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.3)
        turn = review.items[0].chat[-1]
        assert turn.selection is not None and turn.selection.path == "save.py"
        assert "Code the reviewer highlighted" in fake_backend.calls[-1]["prompt"]
        assert diff.selection is None  # cleared after asking

        # Split view: toggle and select the old side.
        app.query_one("#items").focus()
        await pilot.press("v")
        assert diff.split and usersettings.load()["diff_mode"] == "split"
        diff.focus()
        await pilot.press("left", "shift+down")
        if diff.selection:
            assert diff.selection.side == "old"

        # Theme picker: choose the red-green preset.
        app.query_one("#items").focus()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert usersettings.load()["theme"] == "red-green"
        assert app.palette["name"] == "red-green"
