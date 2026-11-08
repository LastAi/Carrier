# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Open the Carrier desktop window.

Run with ``carrier-app``, ``carrier app`` or ``python -m carrier.desktop``.

How the page is loaded (and why it matters for security)
--------------------------------------------------------
``web/index.html`` references ``app.css`` and ``app.js``. Instead of letting
pywebview serve those files (which would start its built-in HTTP server on
a local port), we inline them into one HTML string and pass it as
``html=``. With ``http_server=False`` the app therefore listens on **no
port at all**; the page reaches Python only through pywebview's in-process
JavaScript bridge. ``tests/test_isolation.py`` checks this.

The page also carries a strict Content-Security-Policy: no network
requests of any kind (no CDNs, fonts or analytics), only its own inline
script and styles.

Platform notes
--------------
* Windows: uses the Edge WebView2 runtime (built into Windows 10/11).
* macOS:   uses the system WebKit.
* Linux:   needs GTK (``python3-gi gir1.2-webkit2-4.1``) or Qt
           (``pip install pywebview[qt]``).
"""

from __future__ import annotations

import sys
from importlib import resources

from carrier import __version__
from carrier.desktop.api import DesktopApi, dumps


def build_html() -> str:
    """index.html with app.css and app.js inlined (see module docstring)."""
    web = resources.files("carrier.web")
    html = web.joinpath("index.html").read_text(encoding="utf-8")
    css = web.joinpath("app.css").read_text(encoding="utf-8")
    js = web.joinpath("app.js").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="app.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<script src="app.js"></script>', f"<script>\n{js}\n</script>")
    return html.replace("{{VERSION}}", __version__)


def _log_when_windowless() -> None:
    """Under pythonw / a windowed build there is no console: sys.stderr is
    None and every error would vanish. Send output to a log file instead
    (next to the user settings), so problems can be diagnosed."""
    if sys.stderr is None or sys.stdout is None:
        from carrier.core.usersettings import config_dir

        config_dir().mkdir(parents=True, exist_ok=True)
        log = open(config_dir() / "desktop.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stderr = sys.stderr or log
        sys.stdout = sys.stdout or log


def _watchdog(window: object, every: float = 10.0, limit: float = 20.0) -> None:
    """Write every thread's stack to desktop.log if the window stops responding.

    A tiny script is evaluated in the page every few seconds. If the window's
    UI thread is blocked it never answers; after ``limit`` seconds the stacks
    of all Python threads go to the log (once), so a freeze can be diagnosed.
    """
    import faulthandler
    import threading
    import time

    def loop() -> None:
        time.sleep(15)  # let the window finish starting
        reported = False
        while True:
            done = threading.Event()

            def ping() -> None:
                try:
                    window.evaluate_js("1")  # type: ignore[attr-defined]
                except Exception:
                    pass
                done.set()

            threading.Thread(target=ping, daemon=True, name="carrier-ping").start()
            if not done.wait(limit):
                if not reported and sys.stderr is not None:
                    print(f"[watchdog] the window stopped responding for {limit:.0f}s; thread stacks:",
                          file=sys.stderr, flush=True)
                    try:
                        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    except Exception:
                        pass
                    reported = True
                done.wait()  # wait for it to recover (or the app to close)
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="carrier-watchdog").start()


def hide_console_windows() -> None:
    """Windows: start every child program (git, Claude Code, …) without a window.

    The app runs without a console (pythonw), so each console program it
    starts would otherwise flash up its own command window for a moment —
    many times a minute while Flight Deck and Debrief work. This makes
    ``CREATE_NO_WINDOW`` the default for every ``subprocess.Popen`` in the
    app (asyncio's subprocesses use it too). Code that passes its own
    ``creationflags`` keeps them.
    """
    if sys.platform != "win32":
        return
    import subprocess

    if getattr(subprocess.Popen, "_carrier_no_window", False):
        return
    original = subprocess.Popen.__init__
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    def init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not kwargs.get("creationflags") and len(args) < 13:  # creationflags is the 13th positional
            kwargs["creationflags"] = no_window
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = init  # type: ignore[method-assign]
    subprocess.Popen._carrier_no_window = True  # type: ignore[attr-defined]


def _check_models() -> None:
    """Ask Claude Code which models (and effort levels) it offers, in the background."""
    import os
    import threading

    if os.environ.get("CARRIER_NO_MODEL_CHECK"):
        return

    def run() -> None:
        try:
            from carrier.core.llm import refresh_models

            cat = refresh_models()
            print(f"[models] {', '.join(m['value'] for m in cat['models'])}", file=sys.stderr)
        except Exception as exc:  # no Claude Code, not signed in, offline: keep the saved list
            print(f"[models] couldn't ask Claude Code: {exc!r}", file=sys.stderr)

    threading.Thread(target=run, daemon=True, name="carrier-models").start()


def _fatal(message: str) -> None:
    """Show a startup error even when there is no console."""
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Carrier", 0x10)  # type: ignore[attr-defined]
        except Exception:
            pass


def main() -> int:
    _log_when_windowless()
    hide_console_windows()  # before anything starts git or Claude Code
    try:
        import webview
    except ImportError:
        _fatal("The desktop app needs pywebview. Re-run setup, or: pip install pywebview")
        return 1

    api = DesktopApi()
    window = webview.create_window(
        f"Carrier {__version__}",
        html=build_html(),
        js_api=api,
        width=1400,
        height=900,
        min_size=(900, 600),
        background_color="#14161a",
        text_select=True,  # allow copying guide answers
    )

    def emit(kind: str, data: object) -> None:
        try:
            window.evaluate_js(dumps(kind, data))
        except Exception:
            pass  # window closing

    def pick_folder() -> str | None:
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG  # pywebview 5 vs 6
        result = window.create_file_dialog(kind)
        return result[0] if result else None

    def pick_file(description: str, patterns: str) -> str | None:
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.OPEN if dialog else webview.OPEN_DIALOG  # pywebview 5 vs 6
        result = window.create_file_dialog(kind, file_types=(f"{description} ({patterns})", "All files (*.*)"))
        return result[0] if result else None

    def attention() -> None:
        """Flash the window's taskbar button until it's focused (Windows; elsewhere nothing)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(window.native.Handle.ToInt64())

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

            # FLASHW_ALL | FLASHW_TIMERNOFG: flash until the window comes to the front.
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass  # a flash is a nicety; never fail because of it

    api._bind(emit, pick_folder, pick_file)
    api._attention = attention
    window.events.closed += api._shutdown
    # Right-click menu for text boxes (spelling suggestions, copy/paste); Windows only.
    from carrier.desktop import editmenu

    editmenu.install()
    _watchdog(window)
    _check_models()
    # http_server=False + html= string: no local server is started.
    try:
        webview.start(http_server=False, private_mode=True)
    except Exception as exc:  # e.g. missing WebView2 / GTK
        hint = {"win32": "Install the Microsoft Edge WebView2 Runtime.",
                "linux": "Install GTK WebKit (python3-gi gir1.2-webkit2-4.1) or pywebview[qt]."
                }.get(sys.platform, "")
        _fatal(f"Carrier could not open its window: {exc}\n{hint}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
