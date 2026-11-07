# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""The Carrier desktop app (a native window via pywebview).

One window hosts all three tools — Ready Room, Flight Deck and Debrief —
as tabs. Today only Debrief is functional.

* ``api.py``   — :class:`DesktopApi`, the object the page's JavaScript calls.
  It is a thin wrapper over :class:`carrier.debrief.session.ReviewSession` and has
  no pywebview imports, so it is tested directly (and in a browser).
* ``main.py``  — opens the window and wires the API to it.
* ``../web/``  — the page itself: plain HTML/CSS/JS, no build step, no
  external resources.

Security: the page is handed to the window as an HTML *string*
(``html=...``) with pywebview's HTTP server disabled, so the desktop app
opens **no network port**. The page talks to Python only through the
in-process JS bridge.
"""
