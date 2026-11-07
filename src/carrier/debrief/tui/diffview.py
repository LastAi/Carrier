# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""DiffView — a scrollable diff with a line cursor and range selection.

Shows the rows from :mod:`debrief.sidebyside` either unified (one column)
or side by side (old | new). The reviewer can highlight a range of lines
to ask the guide about:

========================  ==============================================
Keys (when focused)       Action
========================  ==============================================
↑ / ↓  (or k / j)         move the cursor
Shift+↑ / Shift+↓         extend the highlight
← / →                     split view: choose the old (left) or new side
PgUp / PgDn, Home / End   jump
Esc                       clear the highlight
mouse drag                highlight a range (in split view, on one side)
========================  ==============================================

Colours come from a theme palette (:mod:`debrief.themes`). Colour is never
the only cue: every changed line also has a ``+``/``−`` marker, and removed
lines are struck through in the colour-blind presets.

Implementation note: this is a Textual ``ScrollView`` using the line API
(``render_line``), so only visible lines are rendered — big diffs stay fast.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Region, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from carrier.debrief.models import Selection
from carrier.debrief.sidebyside import Row, selection_from_rows

NUM_W = 5  # width of a line-number column
TAB_SIZE = 4


def _detab(text: str | None) -> str:
    """Expand tabs: a raw tab would make the terminal jump to its own tab stop
    and misalign every column after it."""
    return (text or "").expandtabs(TAB_SIZE)


class DiffView(ScrollView, can_focus=True):
    BINDINGS = [
        Binding("up,k", "move(-1)", "Up", show=False),
        Binding("down,j", "move(1)", "Down", show=False),
        Binding("shift+up", "extend(-1)", "Select up", show=False),
        Binding("shift+down", "extend(1)", "Select down", show=False),
        Binding("pageup", "move(-20)", show=False),
        Binding("pagedown", "move(20)", show=False),
        Binding("home", "jump(0)", show=False),
        Binding("end", "jump(-1)", show=False),
        Binding("left", "side('old')", "Old side", show=False),
        Binding("right", "side('new')", "New side", show=False),
        Binding("escape", "clear", "Clear selection", show=False),
    ]

    class SelectionChanged(Message):
        """Posted when the highlighted range changes (selection may be None)."""

        def __init__(self, selection: Selection | None) -> None:
            super().__init__()
            self.selection = selection

    def __init__(self, palette: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows: list[Row] = []
        self.split = False
        self.cursor = 0
        self.anchor: int | None = None  # set while a range is highlighted
        self.side = "new"  # split view: which side the selection refers to
        self._dragging = False
        self.set_palette(palette)

    # -- public API --------------------------------------------------------

    def set_palette(self, palette: dict[str, Any]) -> None:
        c = palette["colors"]
        strike = palette.get("strike_removed", False)
        self.styles_map = {
            "add": Style(color=c["added_fg"], bgcolor=c["added_bg"]),
            "del": Style(color=c["removed_fg"], bgcolor=c["removed_bg"]),
            "context": Style(color=c["context_fg"]),
            "hunk": Style(color=c["hunk_fg"], italic=True),
            "file": Style(color=c["file_fg"], bold=True),
            "num": Style(color=c["line_no_fg"]),
            "note": Style(color=c["line_no_fg"], italic=True),
            "sel": Style(bgcolor=c["selection_bg"]),
            "cursor": Style(color=c["accent"], bold=True),
        }
        # Strikethrough is applied to the removed *text* only, not its padding.
        self.strike_removed = strike
        self.refresh()

    def show(self, rows: list[Row], split: bool) -> None:
        self.rows = rows
        self.split = split
        self.cursor = self._first_code_row()
        self.anchor = None
        self.virtual_size = Size(self.size.width or 80, len(rows))
        self.scroll_home(animate=False)
        self.refresh()
        self._announce()

    @property
    def selection(self) -> Selection | None:
        """The highlighted lines, or None if nothing is highlighted."""
        if self.anchor is None:
            return None
        return selection_from_rows(self.rows, self.anchor, self.cursor, self.side if self.split else None)

    def clear_selection(self) -> None:
        self.anchor = None
        self.refresh()
        self._announce()

    # -- actions -------------------------------------------------------------

    def action_move(self, delta: int) -> None:
        self.anchor = None
        self._set_cursor(self.cursor + delta)

    def action_extend(self, delta: int) -> None:
        if self.anchor is None:
            self.anchor = self.cursor
        self._set_cursor(self.cursor + delta)

    def action_jump(self, where: int) -> None:
        self.anchor = None
        self._set_cursor(where if where >= 0 else len(self.rows) - 1)

    def action_side(self, side: str) -> None:
        if self.split and side != self.side:
            self.side = side
            self.refresh()
            self._announce()

    def action_clear(self) -> None:
        self.clear_selection()

    # -- mouse -----------------------------------------------------------------

    def _row_at(self, event: events.MouseEvent) -> int:
        return max(0, min(len(self.rows) - 1, event.y + int(self.scroll_offset.y)))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self.rows:
            return
        self.focus()
        if self.split:
            self.side = "old" if event.x < self.size.width // 2 else "new"
        row = self._row_at(event)
        if event.shift and self.anchor is None:
            self.anchor = self.cursor
        elif not event.shift:
            self.anchor = row
        self._dragging = True
        self.capture_mouse()
        self._set_cursor(row)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            self._set_cursor(self._row_at(event))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            if self.anchor == self.cursor and not event.shift:
                # A plain click selects that single line.
                self._announce()

    # -- rendering ---------------------------------------------------------------

    def on_resize(self) -> None:
        self.virtual_size = Size(self.size.width, len(self.rows))

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        idx = y + int(self.scroll_offset.y)
        if idx >= len(self.rows):
            return Strip.blank(width)
        text = self._row_text(self.rows[idx], idx, width)
        text.truncate(width, overflow="crop", pad=True)
        return Strip(list(text.render(self.app.console)), width)

    def _in_selection(self, idx: int) -> bool:
        if self.anchor is None:
            return False
        lo, hi = sorted((self.anchor, self.cursor))
        return lo <= idx <= hi

    def _row_text(self, row: Row, idx: int, width: int) -> Text:
        s = self.styles_map
        is_cursor = idx == self.cursor and self.has_focus
        gutter = Text("▶" if is_cursor else " ", style=s["cursor"])
        if row.kind in ("file", "hunk", "note"):
            label = f"▸ {row.new}" if row.kind == "file" else _detab(row.new)
            return gutter + Text(label, style=s[row.kind])
        sel = self._in_selection(idx)
        if not self.split:
            num_old = f"{row.old_no:>{NUM_W}}" if row.old_no else " " * NUM_W
            num_new = f"{row.new_no:>{NUM_W}}" if row.new_no else " " * NUM_W
            marker = {"add": "+", "del": "−"}.get(row.kind, " ")
            body = _detab(row.old if row.kind == "del" else row.new)
            prefix = gutter + Text(f"{num_old} {num_new} ", style=s["num"])
            body_t = Text(f"{marker} {body or ''}")
            if row.kind == "del" and self.strike_removed and body:
                body_t.stylize(Style(strike=True), 2, 2 + len(body))
            body_t.truncate(max(1, width - prefix.cell_len), overflow="crop", pad=True)
            body_t.stylize(s.get(row.kind, s["context"]))  # padding too: colour to the edge
            line = prefix + body_t
            if sel:
                line.stylize(s["sel"], 1)  # applied last so it wins over the line colour
            return line
        # Side by side: [gutter][old half] │ [new half]
        half = max(10, (width - 2) // 2)
        left = self._half(row.old_no, row.old, "del" if row.kind in ("del", "change") else
                          ("context" if row.kind == "context" else "empty"), half,
                          sel and self.side == "old")
        right = self._half(row.new_no, row.new, "add" if row.kind in ("add", "change") else
                           ("context" if row.kind == "context" else "empty"), half,
                           sel and self.side == "new")
        return gutter + left + Text("│", style=s["num"]) + right

    def _half(self, no: int | None, text: str | None, kind: str, width: int, selected: bool) -> Text:
        s = self.styles_map
        text = _detab(text)
        if kind == "empty":
            t = Text(" " * width, style=s["num"])
        else:
            marker = {"add": "+", "del": "−"}.get(kind, " ")
            num = f"{no:>{NUM_W}}" if no else " " * NUM_W
            body = Text(f"{marker} {text or ''}")
            if kind == "del" and self.strike_removed and text:
                body.stylize(Style(strike=True), 2, 2 + len(text))
            body.truncate(max(1, width - NUM_W - 1), overflow="crop", pad=True)
            body.stylize(s[kind])
            t = Text(f"{num} ", style=s["num"]) + body
        if selected:
            t.stylize(s["sel"])
        return t

    # -- helpers -----------------------------------------------------------------

    def _first_code_row(self) -> int:
        return next((n for n, r in enumerate(self.rows) if r.kind in ("add", "del", "change")), 0)

    def _set_cursor(self, idx: int) -> None:
        if not self.rows:
            return
        self.cursor = max(0, min(len(self.rows) - 1, idx))
        self.scroll_to_region(Region(0, self.cursor, 1, 1), animate=False, immediate=True)
        self.refresh()
        self._announce()

    def _announce(self) -> None:
        self.post_message(self.SelectionChanged(self.selection))

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()
