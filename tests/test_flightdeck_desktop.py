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
        pg.carrier_api = api  # for tests that drive the session from Python
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


def test_question_and_rule_cards(fdpage):
    from carrier.flightdeck import sdk

    page, srv = fdpage
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "ask")
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector(".qcard .qopt")
    page.click(".qcard .qopt >> text=Reorganise")
    page.click(".qcard .pactions >> text=Send answers")
    page.wait_for_selector(".qcard.done >> text=Answered: Which layout? → Reorganise")
    assert ALL_CLIENTS[-1].answers == [{"Which layout?": "Reorganise"}]
    # Claude proposes a rule: an editable card; the edited text is what's saved.
    deck = page.carrier_api._fd()
    sid = next(iter(deck.runners))
    tool = sdk.propose_rule_tool(deck.runners[sid]._on_propose_rule)
    deck._run(tool.handler({"text": "- Keep the API <b>stable</b>.", "scope": "project", "why": "You asked"}),
              timeout=10)
    page.wait_for_selector(".rcard textarea")
    assert page.locator(".rcard b").count() == 0  # shown as text
    assert page.locator(".rcard select").input_value() == "project"
    page.fill(".rcard textarea", "- Keep the public API stable.")
    page.click(".rcard >> text=Add rule")
    page.wait_for_selector(".rcard.done >> text=Added to ApiServer's rules.")
    project = deck.store.session(sid)["project_id"]
    assert deck.room.project(project)["rules"] == "- Keep the public API stable."


def test_images_and_skills_in_the_page(fdpage, tmp_path):
    import base64

    page, srv = fdpage
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    shot = tmp_path / "shot.png"
    shot.write_bytes(png)
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "look at this")
    # Skills pane: write one by hand and save it into the project.
    page.click(".rr-navbtn[data-pane='skills']")
    page.click("#sk-new")
    page.fill("#sk-name", "Release API")
    page.fill("#sk-desc", "Use when releasing the API.")
    page.fill("#sk-body", "1. Run the tests.\n2. Tag the release.")
    page.click("#sk-save")
    page.wait_for_selector(".sk-item >> text=release-api")
    assert (srv / ".claude/skills/release-api/SKILL.md").exists()
    # Flight Deck: add an image with a note, send it.
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector(".turn-end")
    page.set_input_files("#fd-image-file", str(shot))
    page.wait_for_selector("#fd-attach .attach-thumb img")
    page.click("#fd-attach img")
    page.wait_for_selector("#modal-image:not(.hidden)")
    box = page.locator("#img-canvas").bounding_box()
    page.mouse.move(box["x"] + 1, box["y"] + 1)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 1, box["y"] + box["height"] - 1)
    page.mouse.up()
    page.fill("#img-note", "this <b>pixel</b>")
    page.click("#img-done")
    page.fill("#fd-input", "what is this?")
    page.click("#fd-send")
    page.wait_for_selector(".msg-user .msg-images img")
    assert page.locator(".msg-user figcaption b").count() == 0  # the note is text
    client = ALL_CLIENTS[-1]
    page.wait_for_selector(".turn-end >> nth=1")
    assert client.images and "this <b>pixel</b>" in client.prompts[-1]
    # Claude shows an image and proposes a skill.
    deck = page.carrier_api._fd()
    sid = next(iter(deck.runners))
    runner = deck.runners[sid]
    deck._run(runner._call_tool("show_image", {"path": str(shot), "caption": "the login screen"}), timeout=10)
    page.wait_for_selector(".msg-image img")
    assert page.inner_text(".msg-image figcaption") == "the login screen"
    deck._run(runner._call_tool("propose_skill", {"name": "deploy-api", "description": "Use when deploying.",
                                                   "body": "1. Deploy."}), timeout=10)
    card = page.locator(".rcard:has-text('suggests a skill')")
    card.wait_for()
    card.locator("textarea").fill("1. Deploy carefully.")
    card.locator("text=Add skill").click()
    page.wait_for_selector(".rcard.done >> text=Saved deploy-api to this project's skills.")
    assert "Deploy carefully" in (srv / ".claude/skills/deploy-api/SKILL.md").read_text()


def test_routines_and_gate_in_the_page(fdpage):
    page, srv = fdpage
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "first task")
    page.click("#rrp-routine-form summary")
    page.fill("#rro-name", "Push to GitHub")
    page.fill("#rro-text", "Commit and push the reviewed work.")
    page.select_option("#rro-gate", "reviewed")
    page.click("#rro-save")
    page.wait_for_selector(".routine-row >> text=Push to GitHub")
    assert "Review not clear yet" in page.inner_text("#rrp-review-state")
    page.click(".routine-row >> text=▶ Run")
    page.wait_for_selector("#rrp-tasks .routine-tag")
    assert page.locator("#rrp-tasks .task-row").first.inner_text().startswith("CURRENT")
    # Starting it from Flight Deck is refused while the review isn't clear.
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector("#banner:not(.hidden)")
    assert "waits until the project's review is clear" in page.inner_text("#banner")


def test_task_groups_and_takeoff_in_the_page(fdpage):
    page, srv = fdpage
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "main work")
    page.click("#rrp-group-form summary")
    page.fill("#tgf-name", "Documents")
    page.fill("#tgf-tags", "docs, writing")
    page.click("#tgf-save")
    page.wait_for_selector(".task-group >> text=Documents")
    page.fill("#rrp-task-title", "Draft the guide")
    page.select_option("#rrp-task-group", label="Documents")
    page.click("#rrp-task-add")
    page.wait_for_selector(".task-group .task-row >> text=Draft the guide")
    assert "Draft the guide" not in page.inner_text("#rrp-tasks")  # not in the main queue
    # A routine tied to the group, queued now with Run.
    page.click("#rrp-routine-form summary")
    page.fill("#rro-name", "Tidy docs")
    page.fill("#rro-text", "Tidy the docs.")
    page.select_option("#rro-group", label="Documents")
    page.click("#rro-save")
    page.wait_for_selector(".routine-row >> text=group: Documents")
    page.click(".routine-row >> text=▶ Run")
    page.wait_for_selector(".task-group .routine-tag")
    # Flight Deck: the group's current task has its own Start, and the routine waits for take-off.
    page.click(".tool[data-tool='flightdeck']")
    page.wait_for_selector(".fd-task-line.group >> text=Documents: Tidy docs")
    page.wait_for_selector("#fd-takeoff-list .takeoff-row >> text=Tidy docs")
    page.check("#fd-auto-takeoff")
    page.wait_for_selector("#prompt-line:visible")
    page.fill("#prompt-line", "yes")
    page.press("#prompt-line", "Enter")
    deck = page.carrier_api._fd()
    import time
    end = time.time() + 10
    while time.time() < end and not any(s["task"] == "Tidy docs" for s in deck.store.sessions()):
        time.sleep(0.2)
    assert any(s["task"] == "Tidy docs" for s in deck.store.sessions())


def test_work_and_change_cards_in_the_page(fdpage):
    import asyncio

    page, srv = fdpage
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "build the api")
    page.fill("#rrp-task-title", "write the manual")
    page.click("#rrp-task-add")
    page.wait_for_selector("#rrp-tasks >> text=write the manual")
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector(".turn-end")
    deck = page.carrier_api._fd()
    sid = next(iter(deck.runners))
    runner = deck.runners[sid]
    # New work: Claude suggests a new group; the user keeps that and names it.
    fut = asyncio.run_coroutine_threadsafe(runner._call_tool("propose_work", {
        "title": "Rate limiting", "details": "- limit login attempts", "suggest": "new_group",
        "new_group": "Security", "reasoning": "Separate from the API work."}), deck._loop)
    card = page.locator(".rcard:has-text('New work came up')")
    card.wait_for()
    assert "Claude suggests: a new group “Security”" in card.inner_text()
    card.locator("button:text('Add')").click()
    assert "Security" in fut.result(timeout=10)
    page.wait_for_selector(".rcard.done >> text=Added as task")
    pid = deck.store.session(sid)["project_id"]
    assert deck.store.task_groups(pid)[0]["name"] == "Security"
    # Pull another task in, part of it.
    manual = next(t for t in deck.store.tasks(pid) if t["title"] == "write the manual")
    fut = asyncio.run_coroutine_threadsafe(runner._call_tool("propose_task_change", {
        "kind": "pull_part", "task_id": manual["id"], "part": "- API reference page", "remaining": "- intro",
        "why": "The API needs its reference page now."}), deck._loop)
    card = page.locator(".rcard:has-text('Pull part of')")
    card.wait_for()
    card.locator("textarea").first.fill("- API reference page (endpoints only)")
    card.locator("button:text('Change the tasks')").click()
    assert "rest stays there" in fut.result(timeout=10)
    cur = deck.store.task(deck.store.session(sid)["task_id"])
    assert "API reference page (endpoints only)" in cur["details"]
    # The Ready Room shows each task's history of changes.
    page.click(".tool[data-tool='readyroom']")
    page.click("#rr-list .rr-item >> text=ApiServer")
    page.click("#rrp-tasks .task-row:has-text('build the api') >> text=Changes")
    page.wait_for_selector("#modal-history:not(.hidden)")
    text = page.inner_text("#history-list")
    assert "Pulled in work" in text and "by claude" in text


def test_alerts_when_claude_needs_you(fdpage):
    page, srv = fdpage
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "write notes.txt hello")
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector(".perm .pactions >> text=Allow")
    page.wait_for_selector(".fd-sess.needs")  # highlighted in Flight Deck
    # On another tab: a pulsing badge and a banner that leads back.
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector(".tool[data-tool=flightdeck].alerting")
    page.wait_for_selector("#alert-strip:not(.hidden)")
    assert "needs you" in page.inner_text("#alert-text") and page.inner_text("#fd-badge") == "1"
    page.click("#alert-go")
    page.wait_for_selector("#fd-session:not(.hidden)")
    assert page.locator("#alert-strip.hidden").count() == 1
    assert page.locator(".tool[data-tool=flightdeck].alerting").count() == 0
    # Do not disturb: no banner on other tabs, the badge stays.
    page.click("#btn-settings")
    page.check("#alerts-dnd")
    page.click("#settings-close")
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_timeout(600)
    assert page.locator("#alert-strip.hidden").count() == 1 and page.inner_text("#fd-badge") == "1"


def test_usage_limits_in_the_page(fdpage):
    import time

    from .test_usage import reply

    page, srv = fdpage
    logs = Path(__import__("os").environ["CARRIER_CLAUDE_LOGS"])
    (logs / "proj").mkdir()
    (logs / "proj" / "x.jsonl").write_text(reply(time.time() - 60, "m1", cwd=str(srv), inp=1200, out=300) + "\n")
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "limit-warn")
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.wait_for_selector("#fd-transcript .notice >> text=close to your 5-hour limit")
    page.wait_for_selector("#btn-usage.near:text('5h 85%')")
    # Claude stops at the limit: the session waits, with a Continue now button.
    deck = page.carrier_api._fd()
    sid = next(iter(deck.runners))
    deck.send(sid, "limit-hit 600")
    page.wait_for_selector("#fd-continue:not(.hidden)")
    page.wait_for_selector(".fd-sess .st:text-matches('usage limit — until')")
    page.wait_for_selector("#btn-usage.reached:text('5h full')")
    page.wait_for_selector("#fd-status:text-matches('continues')")
    # Starting more work now asks first.
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=＋ Task")
    page.select_option("#fdn-task-sel", "new")
    page.fill("#fdn-task", "another task")
    page.click("#fdn-start")
    page.wait_for_selector("#modal-confirm:not(.hidden) >> text=5-hour limit reached")
    page.click("#confirm-cancel")
    assert page.locator("#modal-fd-new:not(.hidden)").count() == 1  # back in the start dialog
    page.click("#modal-fd-new >> text=Cancel")
    # The usage panel: the limit and the tokens from Claude Code's logs.
    page.click("#btn-usage")
    page.wait_for_selector("#usage-limits >> text=5-hour limit")
    page.wait_for_selector("#usage-projects >> text=ApiServer")
    assert "2.5K" in page.inner_text("#usage-projects")  # 1200 in + 300 out + 1000 cache read
    page.screenshot(path="/tmp/carrier-shots/usage-panel.png")
    page.click("#usage-close")
    # Continue now: Claude is told to carry on.
    page.click("#fd-continue")
    page.wait_for_selector("#fd-continue", state="hidden")
    deadline = time.time() + 10
    while not ALL_CLIENTS[-1].prompts[-1].startswith("[Carrier] The limit that stopped") and time.time() < deadline:
        time.sleep(0.05)
    assert ALL_CLIENTS[-1].prompts[-1].startswith("[Carrier] The limit that stopped")
    # Settings save.
    page.click("#btn-settings")
    page.fill("#usage-hold-at", "70")
    page.press("#usage-hold-at", "Tab")
    page.uncheck("#usage-auto-resume")
    page.click("#settings-close")
    from carrier.core import usersettings

    s = usersettings.load()
    assert s["usage_hold_at"] == 70 and s["usage_auto_resume"] is False
    page.screenshot(path="/tmp/carrier-shots/usage.png")


def test_preapproved_actions(fdpage):
    page, srv = fdpage
    page.click(".tool[data-tool='readyroom']")
    page.wait_for_selector("#rr-pane-intro:not(.hidden)")
    rr_new_project(page, "ApiServer", "A small API server.", "write notes.md hi")
    # A permission card offers to pre-approve it for the whole project.
    page.click(".tool[data-tool='flightdeck']")
    page.click(".fd-project:has(.name:text-is('ApiServer')) >> text=▶ Start")
    page.click("#fdn-start")
    page.click(".perm .pactions >> text=Always allow editing files in this project")
    page.wait_for_selector(".perm .pactions >> text=pre-approved for this project")
    page.wait_for_selector("#fd-transcript .notice >> text=Pre-approved for this project")
    # The Ready Room shows it, and more can be added there.
    page.click(".tool[data-tool='readyroom']")
    page.click("#rr-pane-project >> text=Pre-approved actions")
    page.wait_for_selector("#rrp-allow-sum:text-matches('edit files')")
    assert page.is_checked("#rrp-allow-box [data-allow='edit']")
    assert page.is_disabled("#rrp-allow-sites")
    page.check("#rrp-allow-box [data-allow='read']")
    page.fill("#rrp-allow-commands", "git, npm,git")
    page.press("#rrp-allow-commands", "Tab")
    page.wait_for_selector("#rrp-allow-sum:text-matches('git, npm')")
    assert page.input_value("#rrp-allow-commands") == "git, npm"
    page.check("#rrp-allow-groups label:has-text('Java (JDK)') input")
    page.wait_for_selector("#rrp-allow-sum:text-matches('Java')")
    assert page.locator("#rrp-allow-groups .tag.suggest").count() == 1  # Git: the folder is a repository
    page.fill("#rrp-allow-commands", "bash")
    page.press("#rrp-allow-commands", "Tab")
    page.wait_for_selector("text=can run any command")  # refused, and the field shows what's saved
    import time

    deadline = time.time() + 5
    while page.input_value("#rrp-allow-commands") != "git, npm" and time.time() < deadline:
        time.sleep(0.05)
    assert page.input_value("#rrp-allow-commands") == "git, npm"
    deck = page.carrier_api._fd()
    pid = deck.store.projects()[0]["id"]
    a = deck.room.allowances(pid)
    assert a["read"] and a["edit"] and a["commands"] == ["git", "npm"] and a["groups"] == ["java"]
    import json as _json

    shared = _json.loads((srv / ".claude" / "settings.local.json").read_text())["permissions"]["allow"]
    assert "Bash(javap:*)" in shared and "Edit(/**)" in shared
    page.screenshot(path="/tmp/carrier-shots/allow.png")
