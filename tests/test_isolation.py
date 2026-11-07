# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Security guarantees around networking. These tests are the contract.

1. The core package (``src/carrier``) contains **no networking code**: no
   imports of socket servers, HTTP libraries, or the future optional
   ``carrier_net`` package. Network features must live in ``carrier_net``,
   a separate top-level package that the secure build leaves out.
2. The desktop app opens **no listening port**: it passes the page as an
   HTML string with pywebview's HTTP server disabled.
3. A whole review through the desktop API never binds or listens on a
   socket.

If you are adding a feature and one of these fails, the feature belongs in
the optional network package instead — see ARCHITECTURE.md.
"""

import ast
import socket
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "carrier"

# Modules that can open or serve network connections.
FORBIDDEN = {
    "socket", "socketserver", "http.server", "http.client", "urllib.request", "ssl",
    "asyncio.start_server", "requests", "httpx", "aiohttp", "bottle", "flask", "fastapi",
    "uvicorn", "websockets", "carrier_net",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Attribute) and node.attr in ("start_server", "create_server"):
            found.add("asyncio.start_server")
    return found


def test_core_has_no_network_imports():
    offenders = {}
    for py in SRC.rglob("*.py"):
        bad = {m for m in _imports(py) if m in FORBIDDEN or m.split(".")[0] in FORBIDDEN}
        if bad:
            offenders[str(py.relative_to(SRC))] = sorted(bad)
    assert not offenders, f"network code found in the core package: {offenders}"


def test_web_page_makes_no_network_requests():
    web = SRC / "web"
    js = (web / "app.js").read_text(encoding="utf-8")
    html = (web / "index.html").read_text(encoding="utf-8")
    for token in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "http://", "https://"):
        assert token not in js, f"app.js must not use {token}"
    assert "connect-src 'none'" in html and "default-src 'none'" in html
    assert "<script src=\"http" not in html and "<link rel=\"stylesheet\" href=\"http" not in html


def test_desktop_window_uses_no_http_server(monkeypatch: pytest.MonkeyPatch):
    """Run main() against a fake pywebview and check how the window is made."""
    calls: dict[str, object] = {}

    class _Hook:  # supports `window.events.closed += handler`
        def __iadd__(self, fn):
            return self

    class FakeWindow:
        def __init__(self) -> None:
            self.events = types.SimpleNamespace(closed=_Hook())

    fake = types.ModuleType("webview")
    fake.FOLDER_DIALOG = 20

    def create_window(title, url=None, html=None, js_api=None, **kw):
        calls["create"] = {"url": url, "html": html, "kw": kw}
        return FakeWindow()

    def start(**kw):
        calls["start"] = kw

    fake.create_window = create_window
    fake.start = start
    monkeypatch.setitem(sys.modules, "webview", fake)
    from carrier.desktop import main as desktop_main

    assert desktop_main.main() == 0
    assert calls["create"]["url"] is None and "<html" in calls["create"]["html"]
    assert "http_port" not in calls["create"]["kw"]
    assert calls["start"].get("http_server") is False


def test_review_never_opens_a_listening_socket(repo: Path, monkeypatch: pytest.MonkeyPatch):
    from carrier.desktop.api import DesktopApi

    def refuse(*_a, **_k):
        raise AssertionError("Debrief tried to open a listening socket")

    monkeypatch.setattr(socket.socket, "bind", refuse)
    monkeypatch.setattr(socket.socket, "listen", refuse)
    from carrier.flightdeck.manager import FlightDeck

    from .fakeclaude import FakeClaudeClient

    api = DesktopApi(backend_factory=lambda: None, pick_folder=lambda: str(repo),
                     deck_factory=lambda: FlightDeck(db_path=repo.parent / "c.db", client_factory=FakeClaudeClient))
    pid = api._rr().create_project("App", str(repo))["id"]
    assert api.db_projects()["ok"]
    ov = api.start_project(pid, False)["data"]
    first = ov["items"][0]["id"]
    assert api.item(first, True)["ok"]
    assert api.decide(first, "approve")["ok"]
    assert api.finish()["ok"]
    api._shutdown()
