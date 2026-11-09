# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Import existing work, end to end in the real desktop page."""

import json
import subprocess
from pathlib import Path

import pytest

from carrier.core.llm import FakeBackend
from carrier.desktop.api import DesktopApi
from carrier.desktop.main import build_html
from carrier.flightdeck.manager import FlightDeck

from .fakeclaude import FakeClaudeClient
from .test_desktop import SHIM

playwright = pytest.importorskip("playwright.sync_api")

PLAN = {
    "summary": "Two strands of work.",
    "moves": [],
    "rule_sets": [{"name": "Modding", "rules": "- Never edit the game folder."}],
    "projects": [
        {"name": "Animation", "goal": "Animated ships.", "main": "mods/Anim", "rule_sets": ["Modding"],
         "folders": [{"path": "mods/AnimDemo", "kind": "work", "label": "demo"}],
         "tasks": [{"title": "Finish the puppet format", "details": ""}]},
        {"name": "Voice", "goal": "Reads text aloud. <img src=x onerror=alert(1)>", "main": "mods/Voice",
         "rule_sets": ["Modding"], "tasks": []},
    ],
}


@pytest.fixture
def impage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    ws = tmp_path / "Workspace"
    for d in ("mods/Anim", "mods/AnimDemo", "mods/Voice"):
        (ws / d).mkdir(parents=True)
        (ws / d / "README.md").write_text(f"{d}\n")
    (ws / "CLAUDE.md").write_text("# Workspace\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=ws, check=True)
    prompts: list[str] = []

    def reply(system: str, prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "I see **two** strands: animation and voice. <script>x</script>\n```carrier-plan\n" + \
                   json.dumps(PLAN) + "\n```"
        return "Fine — keep editing the plan."

    backend = FakeBackend(reply)
    api = DesktopApi(backend_factory=lambda: backend, pick_folder=lambda: str(ws),
                     deck_factory=lambda: FlightDeck(db_path=tmp_path / "c.db", client_factory=FakeClaudeClient))
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Chromium not available: {exc}")
        pg = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: errors.append(f"dialog: {d.message}"))
        pg.expose_function("__carrier_call", lambda name, args: json.dumps(getattr(api, name)(*json.loads(args))))
        pg.add_init_script(SHIM)
        html = build_html()
        pg.route("http://carrier.test/", lambda r: r.fulfill(body=html, content_type="text/html"))
        pg.goto("http://carrier.test/")
        yield pg, api, ws, prompts, errors
        api._shutdown()
        browser.close()


def test_import_talk_edit_and_create(impage):
    pg, api, ws, prompts, errors = impage
    pg.click(".tool[data-tool=readyroom]")
    pg.click("#rr-intro-import")
    pg.click("#im-folder-btn")
    pg.click("#im-scan")
    pg.wait_for_selector("#im-found:not(.hidden)")
    found = pg.inner_text("#im-found-list")
    assert "git repository" in found and "CLAUDE.md" in found
    pg.check("input[name=im-mode][value=in_place]")
    pg.click("#im-start")
    pg.wait_for_selector("#im-talk:not(.hidden)")
    assert "IN PLACE" in prompts[0] and "mods/Anim" in prompts[0]
    msg = pg.locator(".im-msg.assistant").first
    assert msg.locator("b").inner_text() == "two"                 # markdown rendered…
    assert pg.locator(".im-msg script, #im-projects img").count() == 0  # …but never as HTML
    assert pg.locator(".im-card").count() == 3                     # 1 rule set + 2 projects
    # Edit the plan, then talk: Claude sees the edit.
    name = pg.locator("#im-projects .im-card .head input").first
    name.fill("Puppeteer")
    pg.fill("#im-text", "Rename looks good?")
    pg.click("#im-send")
    pg.wait_for_selector(".im-msg >> nth=2")
    assert '"Puppeteer"' in prompts[1] and "Rename looks good?" in prompts[1]
    # A broken plan is reported, not applied.
    main = pg.locator("#im-projects .im-card").nth(1).locator("input[placeholder='e.g. mods/MyMod']")
    main.fill("mods/Nope")
    pg.click("#im-check")
    pg.wait_for_selector("#im-problems:not(.hidden)")
    assert "doesn't exist" in pg.inner_text("#im-problems")
    main.fill("mods/Voice")
    pg.click("#im-apply")
    pg.wait_for_selector("#prompt-line:visible")
    pg.fill("#prompt-line", "create")
    pg.press("#prompt-line", "Enter")
    pg.wait_for_selector("#rr-pane-project:not(.hidden)")
    names = sorted(p["name"] for p in api._store().projects())
    assert names == ["Puppeteer", "Voice"]
    anim = next(p for p in api._store().projects() if p["name"] == "Puppeteer")
    assert Path(anim["path"]) == (ws / "mods" / "Anim").resolve()
    assert [f["label"] for f in anim["folders"]] == ["demo"]
    assert api.im_recent()["data"] == []  # finished imports aren't offered again
    assert errors == []


def test_import_questions_and_rule_cards(impage, tmp_path):
    pg, api, ws, prompts, errors = impage
    plan_reply = "Planned.\n```carrier-plan\n" + json.dumps(PLAN) + "\n```"
    first = ("Let me ask.\n```carrier-questions\n" + json.dumps([
        {"question": "Split in place?", "header": "Layout",
         "options": [{"label": "In place (Recommended)", "description": "Nothing moves."},
                     {"label": "Reorganise", "description": "Move folders."}]}])
        + "\n```\n```carrier-rule\n" + json.dumps({"text": "- Never edit the game.", "project": "Animation"})
        + "\n```")
    replies = iter([first, plan_reply, "ok"])
    api._backend_factory = lambda: FakeBackend(lambda s, p: (prompts.append(p), next(replies))[1])
    pg.click(".tool[data-tool=readyroom]")
    pg.click("#rr-intro-import")
    pg.click("#im-folder-btn")
    pg.click("#im-scan")
    pg.wait_for_selector("#im-found:not(.hidden)")
    pg.click("#im-start")
    pg.wait_for_selector(".qcard")
    send = pg.locator(".qcard .pactions button.primary")
    assert send.is_disabled()  # every question needs an answer
    pg.click(".qopt >> text=In place (Recommended)")
    assert pg.locator(".qopt[aria-pressed=true]").count() == 1
    # Without a plan yet, the rule card offers to ask Claude to include it.
    assert pg.locator(".rcard select").input_value() == "ask"
    send.click()
    pg.wait_for_selector("#im-plan:not(.hidden)")
    assert "**Split in place?** In place (Recommended)" in prompts[-1]
    # The old questions are closed; the rule card can now go into the plan's projects.
    assert pg.locator(".qcard.done").count() == 1
    card = pg.locator(".rcard").first
    card.locator("select").select_option("p:0")
    card.locator("textarea").fill("- Never edit the game folder.")
    card.locator("button.primary").click()
    pg.wait_for_selector(".rcard.done")
    rules = pg.locator("#im-projects .im-card").first.locator("textarea").nth(1).input_value()
    assert rules.endswith("- Never edit the game folder.")
    saved = api.im_get(api.im_recent()["data"][0]["id"])["data"]
    assert saved["plan"]["projects"][0]["rules"].endswith("- Never edit the game folder.")
    assert saved["messages"][0]["rules"][0]["status"] == "applied"
    assert errors == []
