# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Flight Deck in the real desktop page (Chromium via Playwright) with the
fake Claude Code client. Skipped if Playwright's Chromium isn't installed."""

import json
from pathlib import Path

import pytest

from carrier.desktop.api import DesktopApi
from carrier.desktop.main import build_html
from carrier.flightdeck.manager import FlightDeck

from .fakeclaude import ALL_CLIENTS, FakeClaudeClient
from .test_desktop import SHIM
from .test_flightdeck import make_repo

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def fdpage(tmp_path: Path):
    ALL_CLIENTS.clear()
    srv = make_repo(tmp_path / "ApiServer", {"schema/user.json": "{}\n"})
    cli = make_repo(tmp_path / "WebClient", {"src/client.py": "x\n"})
    folders = iter([str(srv), str(cli)])
    api = DesktopApi(pick_folder=lambda: next(folders),
                     deck_factory=lambda: FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient))
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Chromium not available: {exc}")
        pg = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.expose_function("__carrier_call", lambda name, args: json.dumps(getattr(api, name)(*json.loads(args))))
        pg.add_init_script(SHIM)
        html = build_html()
        pg.route("http://carrier.test/", lambda r: r.fulfill(body=html, content_type="text/html"))
        pg.goto("http://carrier.test/")
        yield pg, srv
        api._shutdown()
        browser.close()
        assert not errors, errors


def rr_new_project(page, name: str, goal: str, task: str, tags: str = "") -> None:
    """Create a project through the Ready Room's New project pane (folder from the fake picker)."""
    page.click("#rr-new-btn")
    page.click("#rrn-folder-btn")
    page.wait_for_selector(f"#rrn-folder:text-matches('{name}$')")
    page.fill("#rrn-goal", goal)
    page.fill("#rrn-task", task)
    page.fill("#rrn-tags", tags)
    page.click("#rrn-create")
    page.wait_for_selector(f"#rr-pane-project:not(.hidden) >> #rrp-tasks >> text={task[:20]}")


def test_flightdeck_ui_flow(fdpage):
    page, srv = fdpage
    shots = Path("/tmp/carrier-shots")
    shots.mkdir(exist_ok=True)
    page.wait_for_selector("#checks li")

    # Set up two projects in the Ready Room, plus a preset.
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", 'write schema/user.json {"email": "text"}', "backend")
    rr_new_project(page, "WebClient", "A web client for the API.",
                   "hello <img src=x onerror=\"window.__pwned=1\">", "frontend")
    assert page.locator("#rrp-tasks img").count() == 0

    # Link WebClient to ApiServer with contract globs.
    page.select_option("#rrl-other", label="ApiServer")
    page.fill("#rrl-note", "client uses the server's API schema")
    page.fill("#rrl-mine", "src/client.py")
    page.fill("#rrl-theirs", "schema/**/*.json")
    page.click("#rrl-add")
    page.wait_for_selector("#rrp-links >> text=⇄ ApiServer")
    page.screenshot(path=str(shots / "rr-project.png"), full_page=True)

    page.click(".rr-navbtn[data-pane='presets']")
    page.click("#rrps-new")
    page.fill("#rrps-name", "Auto edits")
    page.select_option("#rrps-mode", "acceptEdits")
    page.click("#rrps-save")
    page.wait_for_selector("#rrps-list >> text=Auto edits")

    # Tag filter shows only matching projects.
    page.click("#rr-tags .tag:text-is('backend')")
    page.wait_for_selector("#rr-list .rr-item >> nth=0")
    assert page.locator("#rr-list .rr-item").count() == 1
    page.click("#rr-tags .tag:text-is('backend')")
    page.screenshot(path=str(shots / "rr-presets.png"))

    page.click(".tool[data-tool='flightdeck']")
    page.wait_for_selector(".fd-project >> text=WebClient")
    assert "⇄ WebClient" in page.inner_text("#fd-projects")

    # Session 1 in the API server: the current task is sent and asks for permission.
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector(".perm .pactions >> text=Allow")
    page.screenshot(path=str(shots / "fd-permission.png"))
    page.click(".perm .pactions >> text=Allow")
    page.wait_for_selector(".turn-end")
    assert "shared contract with WebClient" in page.inner_text("#fd-transcript")  # warned before editing
    assert (srv / "schema/user.json").read_text().startswith('{"email"')
    assert page.locator(".tool .tname >> text=Write").count() == 1

    # Session 2 in the editor: its first briefing lists the related ApiServer work.
    page.click(".fd-project:has(.name:text-is('WebClient')) >> text=▶ Start")
    page.select_option("#fdn-preset", label="Auto edits")
    page.click("#fdn-start")
    page.wait_for_selector("#fd-status:text('waiting for you')")
    assert page.locator("#fd-transcript img").count() == 0  # task and reply rendered as text
    assert page.evaluate("window.__pwned") is None
    assert "has edited schema/user.json" in page.inner_text("#fd-transcript")
    assert "ApiServer" in page.inner_text("#fd-aware")

    # The ApiServer session changes the contract again while the editor session is active…
    page.click(".fd-sess:has-text('write schema')")
    page.wait_for_selector("#fd-status:text('waiting for you')")
    page.fill("#fd-input", 'write schema/user.json {"email": "email"}')
    page.press("#fd-input", "Enter")
    page.wait_for_selector(".perm:not(.done) .pactions >> text=Allow")
    page.click(".perm:not(.done) .pactions >> text=Allow")
    page.wait_for_selector("#fd-status:text('waiting for you')")
    assert "told session(s) #2" in page.inner_text("#fd-transcript")

    # …so the editor session's next message carries the change to Claude.
    page.click(".fd-sess:has-text('hello')")
    page.wait_for_selector("#fd-status:text('waiting for you')")
    assert "1 update(s) will be shared" in page.inner_text("#fd-aware")
    page.fill("#fd-input", "what changed?")
    page.press("#fd-input", "Enter")
    page.wait_for_selector("#fd-transcript >> text=changed the shared contract file")
    page.wait_for_selector("#fd-status:text('waiting for you')")
    page.screenshot(path=str(shots / "fd-session.png"))
    # Switch effort mid-session: reconnects, same conversation.
    page.select_option("#fd-effort", "high")
    page.wait_for_selector("#fd-transcript >> text=Effort: high")
    assert ALL_CLIENTS[-1].options.effort == "high"
    page.click(".tool[data-tool='flightdeck']")

    # Statuses in the sidebar, then hand the session to Debrief.
    assert page.locator(".fd-sess .st >> text=waiting for you").count() == 2
    page.click("#fd-debrief")
    page.wait_for_selector("#folder-ready:not(.hidden)")
    assert "Flight Deck session" in page.inner_text("#from-select")


def test_standalone_task_flow(fdpage):
    page, srv = fdpage
    page.wait_for_selector("#checks li")
    page.click(".tool[data-tool='readyroom']")
    rr_new_project(page, "ApiServer", "A small API server.", "first task")

    # A standalone task in a scratch folder.
    page.click(".tool[data-tool='flightdeck']")
    page.click("#fd-standalone-btn")
    page.fill("#fds-title", "try a parser idea")
    page.click("#fds-start")
    page.wait_for_selector("#fd-status:text('waiting for you')")
    assert "Standalone" in page.inner_text("#fd-meta")
    page.screenshot(path="/tmp/carrier-shots/fd-standalone.png")

    # Mark it done with my own summary, end the session, then add it to the project.
    page.click("#fd-done")
    page.check("input[name=fdd-how][value=me]")
    page.fill("#fdd-summary", "parser prototype works")
    page.click("#fdd-ok")
    page.wait_for_selector("#fd-meta:text-matches('task done')")
    page.click("#fd-end")
    page.fill("#prompt-line", "yes")
    page.click("#prompt-ok")
    page.wait_for_selector("#fd-assign:not(.hidden)")
    page.click("#fd-assign")
    page.click("#fda-ok")
    page.wait_for_selector("#banner >> text=history")

    # The Ready Room shows it in the history, plus the offered integration task.
    page.click(".tool[data-tool='readyroom']")
    page.click("#rr-list .rr-item:has-text('ApiServer')")
    page.wait_for_selector("#rrp-tasks >> text=Integrate standalone work")
    page.click(".history summary")
    hist = page.inner_text("#rrp-history")
    assert "try a parser idea" in hist and "parser prototype works" in hist and "standalone" in hist

    # Confirm dialogs: one-line input; the button only works once the word is typed.
    page.click("#rrp-remove")
    assert page.is_visible("#prompt-line") and not page.is_visible("#prompt-input")
    assert page.is_disabled("#prompt-ok")
    page.fill("#prompt-line", "remo")
    assert page.is_disabled("#prompt-ok")
    page.screenshot(path="/tmp/carrier-shots/confirm.png")
    page.press("#prompt-line", "Escape")
    assert page.locator("#rr-list .rr-item").count() == 1

    # Preview shows what Claude is told.
    page.click("#rrp-preview")
    preview = page.inner_text("#preview-text")
    assert "A small API server." in preview and "Integrate standalone work" in preview
