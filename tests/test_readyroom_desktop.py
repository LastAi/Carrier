# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Ready Room in the real desktop page: review agents and "Draft with Claude"."""

import json
from pathlib import Path

import pytest

from carrier.core.llm import FakeBackend
from carrier.desktop.api import DesktopApi
from carrier.desktop.main import build_html
from carrier.flightdeck.manager import FlightDeck

from .fakeclaude import FakeClaudeClient
from .test_desktop import SHIM

playwright = pytest.importorskip("playwright.sync_api")


def drafter(system: str, prompt: str) -> str:
    if "You set up a new project" in system:
        assert "Folder: Game" in prompt and "main.py" in prompt and "A turn-based game" in prompt
        assert "secret code" not in prompt  # file contents (other than the README's start) aren't read
        return json.dumps({"name": "Game", "goal": "A turn-based <b>strategy</b> game.",
                           "rules": "- Keep old save files loading.", "first_task": "Load v1 saves",
                           "first_task_details": "- Read the old format\n- Add a test", "tags": ["game", "python"]})
    if "You write tasks" in system:
        assert "Tasks already queued" in prompt
        return json.dumps({"title": "Make old saves load", "details": "- Handle v1 files\n- Add a test"})
    if "You write routines" in system:
        return json.dumps({"name": "Push to GitHub", "instructions": "- Commit and push.", "trigger": "manual",
                           "gate": "reviewed"})
    if "review agents for Debrief" in system:
        return json.dumps({"title": "Save compatibility", "severity": "high",
                           "prompt": "Flag changes that stop old <b>save files</b> from loading."})
    assert "What the user wants" in prompt
    return "- Never add a dependency without asking.\n- Keep old save files loading."


@pytest.fixture
def rrpage(tmp_path: Path):
    proj = tmp_path / "Game"
    proj.mkdir()
    backend = FakeBackend(drafter)
    api = DesktopApi(backend_factory=lambda: backend, pick_folder=lambda: str(proj),
                     deck_factory=lambda: FlightDeck(db_path=tmp_path / "c.db", client_factory=FakeClaudeClient))
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
        yield pg, api, backend
        api._shutdown()
        browser.close()
        assert not errors, errors


def test_draft_rules_and_review_agents(rrpage):
    page, api, backend = rrpage
    shots = Path("/tmp/carrier-shots")
    shots.mkdir(exist_ok=True)
    # No projects yet: the app opens on the Ready Room, with the setup checks.
    page.wait_for_selector("#view-readyroom:not(.hidden) #checks li")
    page.click("#rr-intro-new")
    page.click("#rrn-folder-btn")
    page.fill("#rrn-goal", "A small strategy game.")
    page.click("#rrn-create")
    page.wait_for_selector("#rr-pane-project:not(.hidden)")

    # Draft project rules: nothing changes until "Use this draft", then it's unsaved until Save.
    page.click("#rr-pane-project .draft-btn[data-draft='project_rules']")
    page.fill("#draft-desc", "no new libraries without asking; old saves must keep working")
    page.click("#draft-go")
    page.wait_for_selector("#draft-result:not(.hidden)")
    page.screenshot(path=str(shots / "rr-draft.png"))
    assert "A small strategy game." in backend.calls[-1]["prompt"]  # the goal is given as context
    page.click("#draft-use")
    assert "Never add a dependency" in page.input_value("#rrp-rules")
    assert page.is_visible("#rrp-dirty")
    page.click("#rrp-save")
    page.wait_for_selector("#rrp-dirty.hidden", state="attached")
    pid = api._store().projects()[0]["id"]
    assert "Never add a dependency" in api._store().project(pid)["rules"]

    # Review agents for this project: switch a built-in off, add one with a draft.
    page.click("#rrp-agents-btn")
    page.wait_for_selector("#rr-pane-agents:not(.hidden)")
    assert "Review agents — Game" in page.inner_text("#rra-title")
    page.locator(".builtin-row:has-text('Unrequested') input[type=checkbox]").uncheck()
    # Severity, model and effort per built-in, for this project only.
    row = ".builtin-row[data-name='security']"
    page.select_option(f"{row} select[data-key='effort']", "max")
    page.wait_for_selector(f"{row} select[data-key='effort'].set")
    page.select_option(f"{row} select[data-key='model']", "opus")
    page.wait_for_selector(f"{row} select[data-key='model'].set")
    page.select_option(f"{row} select[data-key='severity']", "low")
    page.wait_for_selector(f"{row} select[data-key='severity'].set")
    page.screenshot(path=str(shots / "rr-agent-options.png"))
    page.click("#rra-new")
    page.select_option("#rra-model", "haiku")
    page.select_option("#rra-effort", "low")
    page.click("#rr-pane-agents .draft-btn")
    page.fill("#draft-desc", "catch anything that breaks loading old saves")
    page.click("#draft-go")
    page.wait_for_selector("#draft-result:not(.hidden)")
    page.click("#draft-use")
    assert page.input_value("#rra-title-in") == "Save compatibility"
    assert page.input_value("#rra-severity") == "high"
    page.click("#rra-save")
    page.wait_for_selector("#rra-list >> text=Save compatibility")
    page.screenshot(path=str(shots / "rr-agents.png"), full_page=True)

    from carrier.debrief.projects import effective_criteria

    names = {c.name: c for c in effective_criteria(api._store(), pid, Path(api._store().project(pid)["path"]))}
    assert "scope-creep" not in names and names["save-compatibility"].severity == "high"
    assert (names["save-compatibility"].model, names["save-compatibility"].effort) == ("haiku", "low")
    assert (names["security"].effort, names["security"].model, names["security"].severity) == ("max", "opus", "low")
    assert "tests-weakened" in names

    page.click("#rra-back")
    page.wait_for_selector("#rr-pane-project:not(.hidden)")
    page.wait_for_selector("#rrp-agents-summary:text-matches('1 for this project')")


def test_new_project_shows_up_in_debrief(rrpage):
    """Regression: Debrief loaded its project list only at startup, so a project
    created in the Ready Room afterwards never appeared."""
    page, api, _ = rrpage
    page.wait_for_selector("#view-readyroom:not(.hidden)")
    page.click(".tool[data-tool='debrief']")
    page.wait_for_selector("#db-projects >> text=No projects yet")
    page.click(".tool[data-tool='readyroom']")
    page.click("#rr-intro-new")
    page.click("#rrn-folder-btn")
    page.fill("#rrn-task", "first task")
    page.click("#rrn-create")
    page.wait_for_selector("#rr-pane-project:not(.hidden)")
    page.click(".tool[data-tool='debrief']")
    page.wait_for_selector("#db-projects button:has-text('Game')")
    assert "needs setup" in page.inner_text("#db-projects")  # the folder isn't in git yet
    page.click("#db-projects button:has-text('Game')")
    page.click("#db-folders button:text('Start tracking')")
    page.wait_for_selector("#folder-ready:not(.hidden)")
    assert page.locator("#db-folders li.ok").count() == 1


def test_work_area_width_and_centring(rrpage):
    pg, api, _backend = rrpage
    pg.set_viewport_size({"width": 2560, "height": 1000})
    pg.click(".tool[data-tool=readyroom]")
    pg.wait_for_selector("#rr-pane-intro:not(.hidden)")
    box = pg.locator("#rr-pane-intro").bounding_box()
    assert box["width"] <= 961  # normal width, left-aligned
    left = box["x"]
    pg.click("#btn-settings")
    pg.select_option("#content-width", "wide")
    pg.check("#content-center")
    pg.click("#settings-close")
    pg.wait_for_timeout(300)
    box = pg.locator("#rr-pane-intro").bounding_box()
    assert 1400 <= box["width"] <= 1441 and box["x"] > left + 200  # wider and centred
    s = api.settings()["data"]
    assert s["content_width"] == "wide" and s["content_center"] is True


def test_draft_tasks_and_routines(rrpage):
    pg, api, _backend = rrpage
    pg.click(".tool[data-tool=readyroom]")
    pg.click("#rr-new-btn")
    pg.click("#rrn-folder-btn")
    pg.fill("#rrn-task", "first task")
    pg.click("#rrn-create")
    pg.wait_for_selector("#rr-pane-project:not(.hidden)")
    # A task: title and details come back for editing; nothing is added until "Add task".
    pg.click(".draft-btn[data-draft=task]")
    pg.fill("#draft-desc", "old saves crash")
    pg.click("#draft-go")
    pg.wait_for_selector("#draft-result:not(.hidden)")
    assert pg.input_value("#draft-agent-title") == "Make old saves load"
    pg.click("#draft-use")
    assert pg.input_value("#rrp-task-title") == "Make old saves load"
    assert pg.input_value("#rrp-task-details").startswith("- Handle v1 files")
    pg.click("#rrp-task-add")
    pg.wait_for_selector("#rrp-tasks >> text=Make old saves load")
    pid = api.rr_overview()["data"]["projects"][0]["id"]
    added = [t for t in api.rr_project(pid)["data"]["tasks"] if t["title"] == "Make old saves load"][0]
    assert "Add a test" in added["details"]
    # A routine: name, instructions, trigger and gate are filled into the form.
    pg.click(".draft-btn[data-draft=routine]")
    pg.fill("#draft-desc", "push when reviewed")
    pg.click("#draft-go")
    pg.wait_for_selector("#draft-routine-row:not(.hidden)")
    assert pg.input_value("#draft-gate") == "reviewed"
    pg.click("#draft-use")
    assert pg.input_value("#rro-name") == "Push to GitHub" and pg.input_value("#rro-gate") == "reviewed"
    assert pg.input_value("#rro-text") == "- Commit and push."
    pg.click("#rro-save")
    pg.wait_for_selector(".routine-row >> text=Push to GitHub")


def test_draft_a_new_project(rrpage, tmp_path):
    pg, api, _backend = rrpage
    game = tmp_path / "Game"
    (game / "main.py").write_text("print('secret code')\n")
    (game / "README.md").write_text("# Game\nA turn-based game.\n")
    pg.click(".tool[data-tool=readyroom]")
    pg.click("#rr-new-btn")
    pg.click("#rrn-folder-btn")
    pg.click("#rr-pane-new .draft-btn[data-draft=project]")
    pg.wait_for_selector("#draft-project-note:not(.hidden)")
    pg.fill("#draft-desc", "a strategy game; never break old saves; first load v1 saves")
    pg.click("#draft-go")
    pg.wait_for_selector("#draft-project-row:not(.hidden)")
    assert pg.input_value("#draft-p-task") == "Load v1 saves"
    pg.fill("#draft-p-name", "Strategy Game")  # edits are kept
    pg.click("#draft-use")
    assert pg.input_value("#rrn-name") == "Strategy Game"
    assert pg.input_value("#rrn-goal") == "A turn-based <b>strategy</b> game."
    assert pg.input_value("#rrn-tags") == "game, python"
    pg.click("#rrn-create")
    pg.wait_for_selector("#rr-pane-project:not(.hidden) >> #rrp-tasks >> text=Load v1 saves")
    p = api.rr_project(api.rr_overview()["data"]["projects"][0]["id"])["data"]
    assert p["name"] == "Strategy Game" and p["rules"] == "- Keep old save files loading."
    assert p["tags"] == ["game", "python"] and "Add a test" in p["tasks"][0]["details"]
    assert pg.locator("#rrp-goal b").count() == 0  # shown as text


def test_shared_folder_review_settings(rrpage, tmp_path):
    pg, api, _backend = rrpage
    shared = tmp_path / "Shared"
    shared.mkdir()
    (tmp_path / "Anim").mkdir()
    room = api._rr()
    pid = room.create_project("Game", str(tmp_path / "Game"))["id"]
    room.add_folder(pid, str(shared))
    oid = room.create_project("Anim", str(tmp_path / "Anim"))["id"]
    room.add_folder(oid, str(shared))
    pg.click(".tool[data-tool=readyroom]")
    pg.click("#rr-list .rr-item:has-text('Game')")
    chip = "#rrp-folders .folder-chip"
    pg.wait_for_selector(f"{chip} >> text=shared with Anim")
    assert pg.input_value(f"{chip} select[aria-label='Sharing of Shared']") == "shared"
    pg.click(f"{chip} >> text=Part…")
    pg.fill("#prompt-input", "ships/**\ndata/ship_*.csv")
    pg.click("#prompt-ok")
    pg.wait_for_selector(f"{chip} >> text=Part ✓")
    pg.check(f"{chip} input[aria-label='Leave Shared out of reviews']")
    pg.wait_for_function("document.querySelector(\"#rrp-folders input[type=checkbox]\").checked")
    pg.click(f"{chip} >> text=Note…")
    pg.fill("#prompt-input", "Animation's export folder; only ships/ is ours.")
    pg.click("#prompt-ok")
    pg.wait_for_selector(f"{chip} >> text=Note ✓")
    f = room.project(pid)["folders"][0]
    assert f["part"] == ["ships/**", "data/ship_*.csv"] and f["review"] == "skip"
    assert f["note"].startswith("Animation's export folder")
    # Making it this project's alone is refused while Anim works there too.
    pg.select_option(f"{chip} select[aria-label='Sharing of Shared']", "own")
    pg.wait_for_selector("#banner >> text=stays a shared space")
    assert room.project(pid)["folders"][0]["share"] == "shared"
    # Adding Game's main folder (what the picker returns) to Anim as a work folder offers a reference folder.
    pg.click("#rr-list .rr-item:has-text('Anim')")
    pg.wait_for_selector(f"{chip} >> text=Shared")
    pg.click("#rrp-addfolder-btn")
    pg.wait_for_selector("#confirm-text >> text=Game's main folder")
    pg.click("#confirm-ok")
    pg.wait_for_selector(f"{chip}.ref")
    kinds = {f["label"]: f["kind"] for f in room.project(oid)["folders"]}
    assert kinds["Game"] == "reference"
    pg.screenshot(path="/tmp/carrier-shots/shared-folder.png")
