# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Right-click menu for text boxes (spelling suggestions, cut/copy/paste).

pywebview turns the browser's own right-click menu off unless it runs in
debug mode, and debug mode would also turn on developer tools. Text boxes
still underline misspelled words, but with no menu there is no way to pick
a correction. This module turns the menu back on with only the editing
items — spelling suggestions, add to dictionary, undo/redo, cut, copy,
paste, select all, emoji — and removes everything that would navigate or
reload the page (back, reload, save as, print, inspect, …).

Windows (WebView2) only for now: on macOS and Linux pywebview offers no
hook to do the same. Everything here is best effort: if WebView2 doesn't
offer the hooks, the app simply keeps working without the menu.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

# Menu items (CoreWebView2ContextMenuItem.Name) that act on the page rather
# than on the text being edited. Anything not listed — spelling suggestions
# included — is kept.
BLOCKED = {
    "back", "forward", "reload", "saveAs", "savePageAs", "print", "createQrCode", "share",
    "webCapture", "inspectElement", "openLinkInNewWindow", "copyLinkLocation", "copyLinkToHighlight",
    "openImageInNewWindow", "saveImageAs", "copyImageLocation", "saveMediaAs", "openMediaInNewWindow",
    "readAloud", "translate", "visualSearch", "searchWeb", "addToCollections", "pictureInPicture",
    "loop", "showAllControls",
}
# Note: spelling suggestions have no specific name (WebView2 calls them
# "other"), so "other" must never be blocked.

_logged = 0  # item names are written to desktop.log for the first few menus



def _filter(_sender: Any, args: Any) -> None:
    """ContextMenuRequested handler: drop page items and tidy separators."""
    global _logged
    try:
        items = args.MenuItems
        if _logged < 3:
            _logged += 1
            _log("menu items: " + ", ".join(f"{items[i].Name}({items[i].Kind})" for i in range(items.Count)))
        for i in range(items.Count - 1, -1, -1):
            if str(items[i].Name) in BLOCKED:
                items.RemoveAt(i)
        # Separators left at the ends or doubled up by the removals.
        def is_sep(item: Any) -> bool:
            return "Separator" in str(item.Kind)
        for i in range(items.Count - 1, 0, -1):
            if is_sep(items[i]) and is_sep(items[i - 1]):
                items.RemoveAt(i)
        while items.Count and is_sep(items[0]):
            items.RemoveAt(0)
        while items.Count and is_sep(items[items.Count - 1]):
            items.RemoveAt(items.Count - 1)
        if not items.Count:
            args.Handled = True  # nothing useful left (e.g. right-click on plain text): no menu
    except Exception:
        traceback.print_exc()


def _log(msg: str) -> None:
    try:
        print(f"[edit menu] {msg}", file=sys.stderr)
    except Exception:
        pass


def _prepare(core: Any) -> None:
    """Turn the menu on and attach the filter. Runs on the UI thread, before
    the page loads: WebView2 applies the setting from the next navigation."""
    core.Settings.AreDefaultContextMenusEnabled = True
    core.ContextMenuRequested += _filter


def install() -> None:
    """Call before ``webview.start()``.

    pywebview switches the menu off while setting up WebView2 and then loads
    the page (``EdgeChrome.load_html``/``load_url``). Changing the setting
    later only takes effect on the *next* page load, which never comes, so
    we step in right before that first load: same thread, no waiting.
    ``CARRIER_NO_EDIT_MENU=1`` turns this off.
    """
    import os

    if sys.platform != "win32" or os.environ.get("CARRIER_NO_EDIT_MENU"):
        return
    try:
        from webview.platforms import edgechromium
    except Exception as exc:  # not WebView2 (or pywebview changed)
        _log(f"not available: {exc!r}")
        return
    cls = edgechromium.EdgeChrome
    if getattr(cls, "_carrier_edit_menu", False):
        return

    def wrap(original: Any) -> Any:
        def load(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not getattr(self, "_carrier_menu_ready", False):
                self._carrier_menu_ready = True
                try:
                    _prepare(self.webview.CoreWebView2)
                    _log("on")
                except Exception as exc:
                    _log(f"not available: {exc!r}")
            return original(self, *args, **kwargs)
        return load

    for name in ("load_html", "load_url"):
        if hasattr(cls, name):
            setattr(cls, name, wrap(getattr(cls, name)))
    cls._carrier_edit_menu = True
