# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Debrief's terminal review screen (part of Carrier).

Layout::

    ┌ header: repo · progress ──────────────────────────────────────────┐
    │ items          │ c3. Add retry to loader            (scrolls)       │
    │ ○ ● Add retry… │ flags · guide explanation · conversation           │
    │ ✓   Rename var │────────────────────────────────────────────────────│
    │ ✎ ● Delete…    │ Diff · unified   (v split · Tab to focus · ⇧↓ select)│
    │                │  12  12   keep                                     │
    │                │  13      − old line                                │
    │                │      13  + new line                                │
    │ Asking about save.py lines 13–14 (after the change)                 │
    │ [ Ask about this change… ]                                          │
    └ a approve · f fix · r revert · s skip · n note · v split · t theme ┘

Behaviour worth knowing when changing this file:

* Every decision, explanation and answer is saved to review.json
  immediately, so quitting (q) never loses work; ``debrief`` resumes.
* Explanations are generated lazily when an item is opened; exactly one
  item ahead is prefetched.
* Highlighted lines in the diff (:class:`DiffView`) are attached to the
  next question, so the guide knows exactly which code you mean.
* Colours come from the user's theme (``t``), shared with the desktop app.
* With no backend (``--no-ai``) the guide is off; the diff and decisions
  still work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Footer, Header, Input, Label, ListItem, ListView, Markdown,
                             OptionList, Static)
from textual.widgets.option_list import Option

from carrier.debrief import guide

from carrier.core import themes, usersettings
from carrier.core.config import Config
from carrier.core.llm import Backend, BackendError
from carrier.debrief.models import ChangeItem, ChatTurn, Decision, Review, Selection
from carrier.core.notes import Note, append_review_note
from carrier.debrief.sidebyside import item_rows
from carrier.debrief.store import save_review
from carrier.debrief.tui.diffview import DiffView

DECISION_MARK = {"approve": "[green]✓[/]", "later": "[green]✓⁺[/]", "fix": "[yellow]✎[/]", "revert": "[red]↺[/]",
                 "skip": "[dim]·[/]", None: "[dim]○[/]"}


def item_label(item: ChangeItem, palette: dict[str, Any]) -> str:
    mark = DECISION_MARK[item.decision.action if item.decision else None]
    sev = item.top_severity
    dot = f"[{palette['colors']['sev_' + sev]}]●[/]" if sev else " "
    return f"{mark} {dot} {escape(item.title)}"


class ItemRow(ListItem):
    """One row in the left-hand list; keeps a handle on its label to update it."""

    def __init__(self, item: ChangeItem, palette: dict[str, Any]) -> None:
        self.item = item
        self.label = Label(item_label(item, palette))
        super().__init__(self.label)

    def refresh_label(self, palette: dict[str, Any]) -> None:
        self.label.update(item_label(self.item, palette))


class PromptScreen(ModalScreen[str | None]):
    """Ask for one line of text (fix instruction, note).

    Returns the text — ``""`` if Enter was pressed on an empty line — or
    ``None`` if cancelled with Esc. Callers must treat ``None`` as "do
    nothing"; an empty string can still be a valid answer (e.g. a revert
    with no reason).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self._title, self._placeholder, self._value = title, placeholder, value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title)
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted, "#prompt-input")
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Cancel"), Binding("y", "yes", "Yes")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal():
                yield Button("Finish (y)", variant="primary", id="yes")
                yield Button("Keep reviewing (esc)", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ThemeScreen(ModalScreen[str | None]):
    """Pick a colour theme (including colour-blind presets)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Colour theme — applies here and in the desktop app")
            presets = themes.list_presets()
            yield OptionList(*[Option(("● " if p["name"] == self._current else "  ") + p["label"],
                                      id=p["name"]) for p in presets], id="theme-list")
            yield Label("[dim]Custom colours can be set in the desktop app's Settings.[/]")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DebriefApp(App[bool]):
    """Returns True from ``run()`` if the reviewer chose to finish the review."""

    TITLE = "Carrier · Debrief"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("a", "decide('approve')", "Approve"),
        Binding("f", "decide('fix')", "Fix"),
        Binding("r", "decide('revert')", "Revert"),
        Binding("s", "decide('skip')", "Skip"),
        Binding("n", "note", "Note"),
        Binding("slash", "ask", "Ask"),
        Binding("v", "toggle_split", "Split"),
        Binding("d", "toggle_diff", "Diff", show=False),
        Binding("t", "theme", "Theme"),
        Binding("F", "finish", "Finish"),
        Binding("q", "quit_review", "Quit"),
    ]

    def __init__(self, review: Review, cfg: Config, notes: list[Note], backend: Backend | None) -> None:
        super().__init__()
        self.review = review
        self.cfg = cfg
        self.notes = notes
        self.backend = backend
        self.current: ChangeItem | None = None
        prefs = usersettings.load()
        self.split = prefs["diff_mode"] == "split"
        self.palette = themes.resolve(prefs["theme"], prefs["custom_colors"])
        self.show_diff = True
        self._explaining: dict[str, asyncio.Task[Any]] = {}

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        with Horizontal(id="body"):
            yield ListView(*[ItemRow(i, self.palette) for i in self.review.items], id="items")
            with Vertical(id="right"):
                with VerticalScroll(id="detail"):
                    yield Static(id="item-head")
                    yield Static(id="banner")
                    yield Static("Flags", classes="section-title")
                    yield Static(id="flags")
                    yield Static("Guide", classes="section-title")
                    yield Markdown(id="explanation")
                    yield Static("Conversation", classes="section-title", id="chat-title")
                    yield Markdown(id="chat")
                yield Static(id="diff-bar")
                yield DiffView(self.palette, id="diff")
        yield Static(id="sel-label")
        yield Input(placeholder="Ask about this change…  (/ to type · Tab into the diff to highlight lines)",
                    id="ask")
        yield Footer()

    def on_mount(self) -> None:
        self.review.status = "in_progress"
        self._save()
        self._update_title()
        banner = self.query_one("#banner", Static)
        if self.review.warnings:
            banner.update("\n".join(f"⚠ {escape(w)}" for w in self.review.warnings))
        else:
            banner.display = False
        self._set_selection_label(None)
        items = self.query_one("#items", ListView)
        if self.review.items:
            first = next((n for n, i in enumerate(self.review.items) if i.decision is None), 0)
            items.index = first
            self.show_item(self.review.items[first])
        items.focus()

    # -- showing an item --------------------------------------------------

    @on(ListView.Highlighted, "#items")
    def highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, ItemRow):
            self.show_item(event.item.item)

    def show_item(self, item: ChangeItem) -> None:
        self.current = item
        files = ", ".join(item.paths)
        status = f"  [dim]({item.decision.action})[/]" if item.decision else ""
        self.query_one("#item-head", Static).update(
            f"{item.id}. {escape(item.title)}{status}\n[dim]{escape(files)}[/]\n{escape(item.summary)}"
        )
        self._render_flags(item)
        self._render_explanation(item)
        self._render_diff(item)
        self._render_chat(item)
        self.query_one("#detail", VerticalScroll).scroll_home(animate=False)
        if item.explanation is None:
            self.ensure_explanation(item)

    def _render_flags(self, item: ChangeItem) -> None:
        flags = self.query_one("#flags", Static)
        if not item.flags:
            flags.update("[dim]No criteria flagged this change.[/]")
            return
        colors = self.palette["colors"]
        lines = []
        for f in item.flags:
            lines.append(f"[{colors['sev_' + f.severity]}]● {f.severity.upper()}[/] [b]{escape(f.criterion)}[/] — "
                         f"{escape(f.reason)}")
            if f.evidence:
                lines.append(f"   [dim]evidence: {escape(f.evidence)}[/]")
        flags.update("\n".join(lines))

    def _render_explanation(self, item: ChangeItem) -> None:
        md = self.query_one("#explanation", Markdown)
        if item.explanation is not None:
            md.update(guide.render_explanation(item.explanation))
        elif self.backend is None:
            md.update("_No-AI mode: the guide is off. You can still read the diff and decide._")
        else:
            md.update("_The guide is reading this change…_")

    def _render_diff(self, item: ChangeItem) -> None:
        mode = "side by side (← old · → new)" if self.split else "unified"
        self.query_one("#diff-bar", Static).update(
            f"[b]Diff[/] · {mode}   [dim]v toggle view · Tab to focus · ⇧↑/⇧↓ or drag to highlight · "
            "Esc clears[/]")
        diff = self.query_one(DiffView)
        diff.show(item_rows(self.review, item, self.split), self.split)
        for w in (diff, self.query_one("#diff-bar")):
            w.display = self.show_diff

    def _render_chat(self, item: ChangeItem, pending: str | None = None,
                     pending_sel: Selection | None = None) -> None:
        def turn(q: str, a: str, sel: Selection | None) -> str:
            about = f" _(about {sel.label()})_" if sel else ""
            return f"**You:**{about} {q}\n\n{a}"

        parts = [turn(t.question, t.answer, t.selection) for t in item.chat]
        parts += [f"**Note:** {n}" for n in item.reviewer_notes]
        if pending:
            parts.append(turn(pending, "_thinking…_", pending_sel))
        has = bool(parts)
        self.query_one("#chat-title", Static).display = has
        chat = self.query_one("#chat", Markdown)
        chat.display = has
        chat.update("\n\n---\n\n".join(parts))

    # -- selection ---------------------------------------------------------

    @on(DiffView.SelectionChanged)
    def selection_changed(self, event: DiffView.SelectionChanged) -> None:
        self._set_selection_label(event.selection)

    def _set_selection_label(self, sel: Selection | None) -> None:
        label = self.query_one("#sel-label", Static)
        label.display = sel is not None
        if sel is not None:
            label.update(f"[b]Asking about[/] {escape(sel.label())}  [dim](Esc in the diff to clear)[/]")

    # -- guide calls (background) ----------------------------------------

    def ensure_explanation(self, item: ChangeItem) -> None:
        if self.backend is None or item.explanation is not None or item.id in self._explaining:
            return
        self._explaining[item.id] = asyncio.ensure_future(self._explain(item))

    async def _explain(self, item: ChangeItem) -> None:
        assert self.backend is not None
        try:
            item.explanation = await guide.explain(self.review, item, self.notes, self.cfg, self.backend)
        except BackendError as exc:
            if self.current is item:
                self.query_one("#explanation", Markdown).update(f"_Guide failed: {exc}_")
            return
        finally:
            self._explaining.pop(item.id, None)
        self._save()
        if self.current is item:
            self._render_explanation(item)
            # Prefetch exactly one item ahead — only from the item being read,
            # otherwise each prefetch would trigger the next.
            nxt = self._next_undecided(after=item)
            if nxt is not None:
                self.ensure_explanation(nxt)

    @on(Input.Submitted, "#ask")
    def ask_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        event.input.value = ""
        if not question or self.current is None:
            return
        if self.backend is None:
            self.notify("The guide is off in no-AI mode.", severity="warning")
            return
        diff = self.query_one(DiffView)
        self.answer(self.current, question, diff.selection)
        diff.clear_selection()

    @work(exclusive=False)
    async def answer(self, item: ChangeItem, question: str, selection: Selection | None) -> None:
        assert self.backend is not None
        self._render_chat(item, pending=question, pending_sel=selection)
        try:
            reply = await guide.ask(self.review, item, question, self.cfg, self.backend, selection)
        except BackendError as exc:
            reply = f"_Guide failed: {exc}_"
        item.chat.append(ChatTurn(question=question, answer=reply, selection=selection))
        self._save()
        if self.current is item:
            self._render_chat(item)
            self.query_one("#detail", VerticalScroll).scroll_end(animate=False)

    # -- decisions ---------------------------------------------------------

    def action_decide(self, action: str) -> None:
        item = self.current
        if item is None:
            return
        if action == "fix":
            existing = item.decision.instruction if item.decision else ""
            self.push_screen(
                PromptScreen("What should change? (sent to Claude Code in the fix list)",
                             "e.g. keep the retry but log each failure", existing),
                # A fix needs an instruction: empty or cancelled records nothing.
                lambda text: self._record(item, "fix", text) if text else None,
            )
        elif action == "revert":
            self.push_screen(
                PromptScreen("Revert this change. Optional reason (Enter to skip, Esc to cancel):", "reason"),
                lambda text: self._record(item, "revert", text) if text is not None else None,
            )
        else:
            self._record(item, action, "")

    def _record(self, item: ChangeItem, action: str, instruction: str) -> None:
        item.decision = Decision(action=action, instruction=instruction,
                                 decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._save()
        self._refresh_rows()
        self._update_title()
        nxt = self._next_undecided(after=item)
        if nxt is None:
            self.show_item(item)
            self.notify("Every item has a decision. Press F to finish the review.", timeout=6)
        else:
            self.query_one("#items", ListView).index = self.review.items.index(nxt)

    def action_note(self) -> None:
        item = self.current
        if item is None:
            return

        def done(text: str | None) -> None:
            if not text:
                return
            path = append_review_note(self.cfg, item.title, item.paths, text)
            item.reviewer_notes.append(text)
            self._save()
            self._render_chat(item)
            self.notify(f"Note added to {path.relative_to(self.cfg.repo).as_posix()}")

        self.push_screen(PromptScreen("Add a project note (future reviews will read it):"), done)

    def action_ask(self) -> None:
        self.query_one("#ask", Input).focus()

    def action_toggle_split(self) -> None:
        self.split = not self.split
        usersettings.update(diff_mode="split" if self.split else "unified")
        if self.current:
            self._render_diff(self.current)

    def action_toggle_diff(self) -> None:
        self.show_diff = not self.show_diff
        if self.current:
            self._render_diff(self.current)

    def action_theme(self) -> None:
        def done(name: str | None) -> None:
            if not name:
                return
            prefs = usersettings.update(theme=name)
            self.palette = themes.resolve(name, prefs["custom_colors"])
            self.query_one(DiffView).set_palette(self.palette)
            self._refresh_rows()
            if self.current:
                self._render_flags(self.current)
            self.notify(f"Theme: {self.palette['label']}")

        self.push_screen(ThemeScreen(self.palette["name"]), done)

    def action_finish(self) -> None:
        undecided = len(self.review.items) - self.review.decided_count
        msg = "Finish this review and move the checkpoint?"
        if undecided:
            msg += f"\n{undecided} item(s) have no decision and will be recorded as not reviewed."

        def done(ok: bool | None) -> None:
            if ok:
                self.exit(True)

        self.push_screen(ConfirmScreen(msg), done)

    def action_quit_review(self) -> None:
        self._save()
        self.exit(False)

    # -- helpers -----------------------------------------------------------

    def _next_undecided(self, after: ChangeItem) -> ChangeItem | None:
        items = self.review.items
        start = items.index(after) + 1
        for item in items[start:] + items[:start]:
            if item.decision is None and item is not after:
                return item
        return None

    def _refresh_rows(self) -> None:
        for row in self.query(ItemRow):
            row.refresh_label(self.palette)

    def _update_title(self) -> None:
        r = self.review
        flagged = sum(1 for i in r.items if i.flags)
        self.sub_title = (f"{len(r.items)} items · {flagged} flagged · "
                          f"{r.decided_count}/{len(r.items)} decided")

    def _save(self) -> None:
        save_review(self.cfg, self.review)
