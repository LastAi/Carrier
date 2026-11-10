# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Drive the real desktop page in Chromium (Playwright).

pywebview's bridge is replaced by a tiny shim that forwards
``window.pywebview.api.<name>(...)`` to a real :class:`DesktopApi`, so this
exercises the actual HTML/JS and the actual Python API together — only the
native window is missing. Skipped if Playwright's Chromium isn't installed.
"""

import json
from pathlib import Path

import pytest

import subprocess

from carrier.desktop.api import DesktopApi
from carrier.desktop.main import build_html
from carrier.core.llm import FakeBackend
from carrier.flightdeck.manager import FlightDeck

from .conftest import scripted_handler
from .fakeclaude import FakeClaudeClient

playwright = pytest.importorskip("playwright.sync_api")

SHIM = """
window.pywebview = { api: new Proxy({}, { get: (_, name) =>
  (...args) => window.__carrier_call(name, JSON.stringify(args)).then(JSON.parse) }) };
"""

EVIL = '<img src=x onerror="window.__pwned=1"> **bold**'


def handler(system: str, prompt: str) -> str:
    if "Reviewer's question" in prompt:
        return EVIL  # model output must never become live HTML
    if prompt.startswith("# Health check: Security sweep"):
        return json.dumps({"findings": [{"file": "save.py", "line": 1, "title": "Path from the user is opened as is",
                                         "detail": EVIL, "suggestion": "Check the path first.", "severity": "high"}]})
    if prompt.startswith("# Health check:"):
        return json.dumps({"findings": []})
    if "tidy its task queues" in system:
        import re
        ids = re.findall(r"- (#\d+) ", prompt)
        return json.dumps({"message": f"{ids[0]} looks stale.", "show": ids[:1],
                           "proposals": [{"kind": "drop", "ids": ids[:1], "why": "stale"}]})
    if "findings of Debrief's" in system:  # the walk-through
        import re
        ids = re.findall(r"- (F\d+) ", prompt)
        return json.dumps({"message": f"One group: {ids[0]} is real. <img src=x onerror=\"window.__pwned=1\">",
                           "show": ids[:1], "proposals": [{"kind": "task", "ids": ids, "title": "Check paths",
                                                           "details": "Validate the path."}]})
    return scripted_handler(system, prompt)


def make_lib(tmp_path: Path) -> Path:
    """An extra folder for the project: one commit, then an uncommitted change."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=lib, check=True)
    (lib / "shared.py").write_text("A = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=lib, check=True)
    subprocess.run(["git", "commit", "-qm", "lib"], cwd=lib, check=True)
    (lib / "shared.py").write_text("A = 2\n")
    return lib


def make_api(tmp_path: Path, repo: Path, backend_factory, with_project: bool = True) -> DesktopApi:
    api = DesktopApi(backend_factory=backend_factory, pick_folder=lambda: str(repo),
                     deck_factory=lambda: FlightDeck(db_path=tmp_path / "carrier.db",
                                                     client_factory=FakeClaudeClient))
    if with_project:
        room = api._rr()
        p = room.create_project("App", str(repo), goal="A small app that saves data.")
        room.add_folder(p["id"], str(make_lib(tmp_path)))
        # A Flight Deck session for a task edited save.py: the review should say so.
        tid = api._store().add_task("Make saves robust", p["id"])
        sid = api._store().add_session(project_id=p["id"], task="Make saves robust", cwd=str(repo),
                                       task_id=tid, status="ended")
        api._store().add_edit(sid, p["id"], "save.py", "Edit")
    return api


@pytest.fixture
def page(repo: Path, claude_home: Path, tmp_path: Path):
    api = make_api(tmp_path, repo, lambda: FakeBackend(handler))
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium not available: {exc}")
        pg = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))

        def call(name: str, args_json: str) -> str:
            method = getattr(api, name)
            assert not name.startswith("_")
            return json.dumps(method(*json.loads(args_json)))

        pg.expose_function("__carrier_call", call)
        pg.add_init_script(SHIM)
        # Serve the page from a fake URL so the init script (the shim) runs.
        html = build_html()
        pg.route("http://carrier.test/", lambda route: route.fulfill(body=html, content_type="text/html"))
        pg.goto("http://carrier.test/")
        pg.errors = errors  # type: ignore[attr-defined]
        yield pg
        api._shutdown()
        browser.close()
        assert not errors, errors


def test_tool_tabs(page):
    # With a project, the app opens on Flight Deck.
    page.wait_for_selector("#view-flightdeck:not(.hidden)")
    assert page.is_visible(".tool.on[data-tool='flightdeck']")
    assert page.is_visible("#view-flightdeck") and not page.is_visible("#view-start")
    page.click(".tool[data-tool='readyroom']")
    assert page.is_visible("#view-readyroom")
    page.click(".tool[data-tool='debrief']")
    assert page.is_visible("#view-start") and not page.is_visible("#view-readyroom")


def test_desktop_full_flow(page, tmp_path: Path):
    shots = Path("/tmp/carrier-shots")
    shots.mkdir(exist_ok=True)
    page.wait_for_selector("#view-flightdeck:not(.hidden)")
    page.click(".tool[data-tool='debrief']")
    page.click("#db-projects button:has-text('App')")
    page.wait_for_selector("#folder-ready:not(.hidden)")
    assert page.locator("#db-folders li.ok").count() == 2  # main + the "lib" extra folder
    page.screenshot(path=str(shots / "start.png"))
    page.click("#btn-start")
    page.wait_for_selector("#view-review:not(.hidden)")
    assert "lib/shared.py" in page.inner_text("#item-list")  # extra folder reviewed too
    assert "Make saves robust" in page.inner_text("#item-list")  # the task that made the change
    page.wait_for_selector("#guide .part")  # explanation arrived
    assert page.locator("#item-list li").count() >= 2
    assert "HIGH" in page.inner_text("#flags")
    page.screenshot(path=str(shots / "review-unified.png"))

    # Side by side, then drag-highlight two lines on the new side.
    page.click("#mode-split")
    page.wait_for_selector("#diff td[data-side='new']")
    cells = page.locator("#diff td.code.add[data-side='new']")
    assert cells.count() >= 3
    cells.nth(3).scroll_into_view_if_needed()
    a, b = cells.nth(1).bounding_box(), cells.nth(3).bounding_box()
    page.mouse.move(a["x"] + 20, a["y"] + 5)
    page.mouse.down()
    page.mouse.move(b["x"] + 20, b["y"] + 5)
    page.mouse.up()
    page.wait_for_selector("#sel-chip:not(.hidden)")
    assert "save.py lines" in page.inner_text("#sel-label")
    assert "after the change" in page.inner_text("#sel-label")
    page.screenshot(path=str(shots / "review-split-selected.png"))

    # Ask about the selection; the (malicious) answer must render as text.
    page.fill("#ask-input", "why sleep here?")
    page.press("#ask-input", "Enter")
    page.wait_for_selector("#chat .turn .about")
    assert "about save.py lines" in page.inner_text("#chat")
    assert page.locator("#chat img").count() == 0
    assert page.evaluate("window.__pwned") is None
    assert page.locator("#chat b").count() >= 1  # safe markdown still works
    assert page.locator("#sel-chip.hidden").count() == 1  # cleared after asking

    # Needs fix via keyboard, with an instruction.
    page.click("#item-head")  # move focus out of the textarea
    page.keyboard.press("f")
    page.wait_for_selector("#modal-prompt:not(.hidden)")
    page.fill("#prompt-input", "keep a test for corrupt saves")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("#modal-prompt.hidden", state="attached")
    assert page.locator("#item-list li .dec-fix").count() == 1

    # Revert prompt cancelled with Esc records nothing.
    page.keyboard.press("r")
    page.wait_for_selector("#modal-prompt:not(.hidden)")
    page.keyboard.press("Escape")
    assert page.locator("#item-list li .dec-revert").count() == 0

    # Colour-blind theme changes the diff colours.
    before = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--added-fg')")
    page.click("#btn-settings")
    page.click(".theme-opt >> text=Red–green safe")
    after = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--added-fg')")
    assert before.strip() != after.strip()
    page.screenshot(path=str(shots / "settings.png"))
    page.click("#settings-close")

    # Finish.
    page.click("#btn-finish")
    page.click("#finish-actions >> text=Finish review")
    page.wait_for_selector("text=Review finished.")
    assert "added to App as its next task" in page.inner_text("#finish-body")
    assert page.locator("#finish-actions >> text=Open in Flight Deck").count() == 1
    page.screenshot(path=str(shots / "finished.png"))
    page.click("#finish-actions >> text=Open in Flight Deck")
    page.wait_for_selector(".fd-task-line >> text=Fix review findings")


def test_untracked_folder_can_be_tracked(tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    api = make_api(tmp_path, folder, lambda: None, with_project=False)
    pid = api._rr().create_project("Plain", str(folder))["id"]
    info = api.db_project(pid)["data"]
    assert info["ready"] is False and api.start_project(pid, False)["ok"] is False
    assert api.db_track(pid, str(tmp_path))["ok"] is False  # not one of the project's folders
    assert api.db_track(pid, str(folder.resolve()))["data"]["ready"] is True
    assert api.open_file("/etc/passwd")["ok"] is False  # no session / outside .carrier
    api._shutdown()


def test_open_file_only_uses_finish_outputs(repo: Path, tmp_path: Path):
    api = make_api(tmp_path, repo, lambda: None)
    pid = api._store().projects()[0]["id"]
    ov = api.start_project(pid, False)["data"]
    (repo / ".carrier" / "evil.bat").write_text("calc")
    assert api.open_file(str(repo / ".carrier" / "evil.bat"))["ok"] is False
    api.decide(ov["items"][0]["id"], "approve")
    res = api.finish()["data"]
    assert api.open_file(str(repo / ".carrier" / "evil.bat"))["ok"] is False
    assert api.finish()["ok"] is False  # double finish refused
    assert res["record"].endswith("record.md")
    api._shutdown()


def test_page_cannot_review_arbitrary_folders(repo: Path, tmp_path: Path):
    api = make_api(tmp_path, repo, lambda: None)
    for name in ("start", "inspect", "choose_folder", "track", "launch_fixes"):
        assert not hasattr(api, name)
    pid = api._store().projects()[0]["id"]
    sid = api._store().add_session(project_id=pid, task="t", cwd=str(tmp_path), status="ended")
    assert api.start_worktree(sid, False)["ok"] is False  # not a worktree session
    assert api.db_worktree(sid)["ok"] is False
    api._shutdown()


def test_hide_console_windows_sets_no_window_flag(monkeypatch):
    """On Windows every child program starts without a console window."""
    import subprocess as sp

    from carrier.desktop import main as M

    seen = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setattr(sp, "Popen", FakePopen)
    monkeypatch.setattr(sp, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    M.hide_console_windows()
    sp.Popen(["git", "status"])
    assert seen["creationflags"] == 0x08000000
    seen.clear()
    sp.Popen(["cmd"], creationflags=0x10)  # explicit flags are kept (e.g. opening a terminal)
    assert seen["creationflags"] == 0x10


def test_edit_menu_is_switched_on_before_the_first_page_load(monkeypatch):
    """The right-click menu setting only applies from the next page load, so it
    must be set right before pywebview loads the page (fake WebView2 here)."""
    import sys
    import types

    from carrier.desktop import editmenu

    order = []

    class FakeSettings:
        def __setattr__(self, name, value):
            order.append((name, value))

    class Core:
        Settings = FakeSettings()

        class Event(list):
            def __iadd__(self, fn):
                self.append(fn)
                order.append(("handler", fn.__name__))
                return self
        ContextMenuRequested = Event()

    class EdgeChrome:
        def __init__(self):
            self.webview = types.SimpleNamespace(CoreWebView2=Core())

        def load_html(self, content, _):
            order.append(("navigate", content))

    fake = types.ModuleType("webview.platforms.edgechromium")
    fake.EdgeChrome = EdgeChrome
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", fake)
    import webview.platforms as platforms
    monkeypatch.setattr(platforms, "edgechromium", fake, raising=False)
    monkeypatch.setattr(editmenu.sys, "platform", "win32")
    editmenu.install()
    e = EdgeChrome()
    e.load_html("<p>page</p>", "")
    e.load_html("<p>again</p>", "")
    assert order[:3] == [("AreDefaultContextMenusEnabled", True), ("handler", "_filter"), ("navigate", "<p>page</p>")]
    assert order.count(("handler", "_filter")) == 1  # once per window


def test_health_check_panel(page):
    page.wait_for_selector("#view-flightdeck:not(.hidden)")
    page.click(".tool[data-tool='debrief']")
    page.wait_for_selector("#db-projects button:has-text('health: 12 due')")
    page.click("#db-projects button:has-text('App')")
    page.click("#health-box summary")
    page.wait_for_selector("#health-due >> text=12 due")
    assert page.locator("#health-agents .hc-agent").count() == 12
    for cb in page.locator("#health-agents input[type=checkbox]").all():
        if cb.get_attribute("aria-label") != "Run Security sweep":
            cb.uncheck()
    assert "1 check(s)" in page.inner_text("#health-est")
    page.click("#btn-health")
    page.wait_for_selector("#btn-health-report >> text=Open the health report (1 open)")
    assert page.is_visible("#health-due") and "11 due" in page.inner_text("#health-due")
    # The report: laid out like a review, with the code around the problem.
    page.click("#btn-health-report")
    page.wait_for_selector("#view-health:not(.hidden)")
    page.wait_for_selector("#hc-list li.hc-item.active >> text=Path from the user is opened as is")
    page.wait_for_selector("#hc-code tr.focus")
    assert page.locator("#hc-code tr.focus td.no").first.inner_text() == "1"
    assert "save.py:1" in page.inner_text("#hc-where")
    assert page.evaluate("window.__pwned") is None  # model output is text, never HTML
    page.screenshot(path="/tmp/carrier-shots/health-report.png")
    # Walk through with Claude: its message links the finding, and a proposal card applies it.
    page.click("#hc-guide-btn")
    page.wait_for_selector("#hc-guide .guide-msg.claude >> text=One group")
    assert page.evaluate("window.__pwned") is None
    page.wait_for_selector("#hc-guide .guide-prop >> text=One task for these findings")
    page.fill("#hc-guide .guide-prop input[type=text]", "Check user paths")
    page.click("#hc-guide .guide-prop button:has-text('Apply')")
    page.wait_for_selector("#hc-guide .guide-prop.applied >> text=finding(s) → task #")
    page.wait_for_selector("#hc-state >> text=task made")
    page.click("#hc-guide-btn")  # hide it again
    page.keyboard.press("o")  # nothing to reopen: no change
    page.keyboard.press("d")  # dismiss: hidden from the list
    page.wait_for_selector("#hc-list >> text=No open findings.")
    page.check("#hc-show-quiet")
    page.wait_for_selector("#hc-list li.hc-item.quiet")
    page.keyboard.press("Escape")
    page.wait_for_selector("#view-start:not(.hidden)")
    # Settings live in the Ready Room.
    page.click("#btn-health-settings")
    page.wait_for_selector("#rr-pane-health:not(.hidden)")
    page.wait_for_selector("#rrh-title >> text=Health checks — App")
    # Every check at once: the project's model for all of them.
    page.select_option("#rrh-group select[aria-label='Model']", "opus")
    page.wait_for_selector("#rrh-group select.set")
    page.screenshot(path="/tmp/carrier-shots/health-readyroom.png", full_page=True)
    page.click("#rrh-new")
    page.fill("#rrh-title-in", "Save compatibility")
    page.fill("#rrh-prompt", "Report code that would stop old save files from loading.")
    page.select_option("#rrh-due", "always")
    page.click("#rrh-save")
    page.wait_for_selector("#rrh-list >> text=Save compatibility")
    page.click("#rrh-back")
    page.wait_for_selector("#rrp-health-summary >> text=12 of 12 built-in checks on")
    page.wait_for_selector("#rrp-health-summary >> text=1 for this project only")
    # Tidy tasks with Claude: the task it talks about is highlighted; dropping keeps it in the history.
    page.click("#rrp-tidy-btn")
    page.wait_for_selector("#rr-pane-tidy:not(.hidden)")
    page.wait_for_selector("#tidy-guide .guide-msg.claude >> text=looks stale")
    page.wait_for_selector("#tidy-queues li.hl")
    n = page.locator("#tidy-queues li").count()
    page.click("#tidy-guide .guide-prop button:has-text('Apply')")
    page.wait_for_selector("#tidy-guide .guide-prop.applied >> text=Dropped 1 task")
    page.wait_for_function(f"document.querySelectorAll('#tidy-queues li').length === {n - 1}")
    page.screenshot(path="/tmp/carrier-shots/tidy.png", full_page=True)
    page.click("#tidy-back")
    page.wait_for_selector("#rr-pane-project:not(.hidden)")
