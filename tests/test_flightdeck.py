# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Flight Deck end to end with a fake Claude Code client (tests/fakeclaude.py)."""

import subprocess
import time
from pathlib import Path

import pytest

from carrier.flightdeck.manager import DeckError, FlightDeck

from .fakeclaude import ALL_CLIENTS, FakeClaudeClient


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def wait(deck: FlightDeck, sid: int, status: str = "idle", timeout: float = 10) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if deck.store.session(sid)["status"] == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"session {sid} stuck in {deck.store.session(sid)['status']}, wanted {status}")


def events(deck: FlightDeck, sid: int, kind: str | None = None) -> list[dict]:
    evs = [e["event"] for e in deck.poll(0)["events"] if e["session"] == sid]
    return [e for e in evs if kind is None or e["type"] == kind]


@pytest.fixture
def deck(tmp_path: Path):
    ALL_CLIENTS.clear()
    d = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    yield d
    d.close()


@pytest.fixture
def projects(tmp_path: Path, deck: FlightDeck):
    srv = make_repo(tmp_path / "ApiServer", {"schema/user.json": "{}\n", "src/app.py": "x\n"})
    cli = make_repo(tmp_path / "WebClient", {"src/api-client/client.py": "x\n"})
    docs = make_repo(tmp_path / "DocsSite", {"README.md": "a\n"})
    for p in (srv, cli, docs):
        deck.add_project(str(p))
    ids = {p["name"]: p["id"] for p in deck.overview()["projects"]}
    deck.add_link(ids["ApiServer"], ids["WebClient"], "client uses the server's API schema",
                  ["schema/**/*.json"], ["src/api-client/**"])
    return ids


def test_parallel_sessions_contract_awareness(deck: FlightDeck, projects):
    a = deck.start_session(projects["ApiServer"], "user endpoints", mode="acceptEdits", send_task=False)
    e = deck.start_session(projects["WebClient"], "settings page", mode="acceptEdits", send_task=False)
    o = deck.start_session(projects["DocsSite"], "update docs", mode="acceptEdits", send_task=False)

    # All three talk at the same time.
    deck.send(a["id"], "write schema/user.json {\"email\": \"text\"}")
    deck.send(e["id"], "hello")
    deck.send(o["id"], "write README.md b")
    for s in (a, e, o):
        wait(deck, s["id"])

    srv_client, cli_client, docs_client = ALL_CLIENTS
    # The server session was warned before editing the contract (policy: warn).
    assert any("shared contract with WebClient" in c for c in srv_client.contexts)
    assert (Path(a["cwd"]) / "schema/user.json").read_text().startswith('{"email"')
    # The editor session is told on its NEXT message; the unrelated project never hears anything.
    deck.send(e["id"], "next")
    wait(deck, e["id"])
    assert any("changed the shared contract file schema/user.json" in c for c in cli_client.contexts)
    assert not any("ApiServer" in c or "WebClient" in c for c in docs_client.contexts)
    # UI saw notices and streaming.
    assert any(ev["kind"] == "contract_changed" for ev in events(deck, e["id"], "notice"))
    assert events(deck, a["id"], "live") and events(deck, a["id"], "tool_use")
    assert deck.awareness(e["id"])["related"][0]["project"] == "ApiServer"
    assert deck.session(a["id"])["files"] == ["schema/user.json"]


def test_permission_prompt_flow(deck: FlightDeck, projects):
    s = deck.start_session(projects["DocsSite"], "update docs", send_task=False)  # mode default: asks
    deck.send(s["id"], "write README.md c")
    wait(deck, s["id"], "needs_you")
    req = events(deck, s["id"], "permission")[0]
    assert req["tool"] == "Write" and "README.md" in req["summary"]
    assert deck.session(s["id"])["pending"][0]["req_id"] == req["req_id"]
    deck.answer(s["id"], req["req_id"], "allow_always")
    wait(deck, s["id"])
    assert (Path(s["cwd"]) / "README.md").read_text() == "c\n"
    # "Allow always" means the next Write doesn't ask.
    deck.send(s["id"], "write README.md d")
    wait(deck, s["id"])
    assert len(events(deck, s["id"], "permission")) == 1
    # Deny path.
    s2 = deck.start_session(projects["DocsSite"], "second", send_task=False)
    deck.send(s2["id"], "write other.txt x")
    wait(deck, s2["id"], "needs_you")
    deck.answer(s2["id"], events(deck, s2["id"], "permission")[0]["req_id"], "deny", "not now")
    wait(deck, s2["id"])
    assert not (Path(s2["cwd"]) / "other.txt").exists()
    with pytest.raises(DeckError):
        deck.answer(s2["id"], "p-nope", "allow")


def test_same_folder_warning_and_worktree(deck: FlightDeck, projects):
    first = deck.start_session(projects["ApiServer"], "routing work", mode="acceptEdits", send_task=False)
    deck.send(first["id"], "write src/app.py v2")
    wait(deck, first["id"])
    choice = deck.isolation_choice(projects["ApiServer"])
    assert choice["needs_choice"] and choice["can_isolate"]

    shared = deck.start_session(projects["ApiServer"], "also routing", mode="acceptEdits", send_task=False)
    deck.send(shared["id"], "write src/app.py v3")
    wait(deck, shared["id"])
    assert any("in this same folder" in c for c in ALL_CLIENTS[1].contexts)

    iso = deck.start_session(projects["ApiServer"], "Big refactor!", isolation="worktree", mode="acceptEdits",
                             send_task=False)
    assert iso["worktree"] and iso["branch"].startswith(f"carrier/{iso['id']}-big-refactor")
    assert Path(iso["cwd"]).is_dir() and Path(iso["cwd"]) != Path(first["cwd"])
    deck.send(iso["id"], "write src/app.py v4")
    wait(deck, iso["id"])
    assert (Path(iso["cwd"]) / "src/app.py").read_text() == "v4\n"
    assert (Path(first["cwd"]) / "src/app.py").read_text() == "v3\n"  # untouched
    assert any("separate copy" in c for c in ALL_CLIENTS[2].contexts)

    # Debrief handoff points at the copy, from the session's start.
    target = deck.debrief_target(iso["id"])
    assert target["path"] == iso["cwd"] and target["from_ref"]
    with pytest.raises(DeckError, match="End the session first"):
        deck.remove_worktree(iso["id"])
    deck.end_session(iso["id"])
    with pytest.raises(DeckError, match="uncommitted"):
        deck.remove_worktree(iso["id"])
    deck.remove_worktree(iso["id"], force=True)
    assert not Path(iso["cwd"]).exists()


def test_block_policy_and_interrupt(deck: FlightDeck, projects):
    deck.set_policy("block")
    a = deck.start_session(projects["ApiServer"], "server", mode="acceptEdits", send_task=False)
    # Nobody is working on the linked editor: the contract edit is only a warning.
    deck.send(a["id"], "write schema/order.json {}")
    wait(deck, a["id"])
    assert ALL_CLIENTS[0].denied == [] and (Path(a["cwd"]) / "schema/order.json").exists()
    # With an editor session active, the same kind of edit is blocked.
    deck.start_session(projects["WebClient"], "settings page", mode="acceptEdits", send_task=False)
    deck.send(a["id"], "write schema/cart.json {}")
    wait(deck, a["id"])
    assert ALL_CLIENTS[0].denied == ["schema/cart.json"]
    assert not (Path(a["cwd"]) / "schema/cart.json").exists()

    deck.send(a["id"], "slow")
    wait(deck, a["id"], "working")
    with pytest.raises(DeckError, match="still working"):
        deck.send(a["id"], "again")
    deck.interrupt(a["id"])
    wait(deck, a["id"])


def test_restart_marks_paused_and_resume(tmp_path: Path, projects, deck: FlightDeck):
    s = deck.start_session(projects["DocsSite"], "update docs", mode="acceptEdits")
    wait(deck, s["id"])
    sdk_id = deck.store.session(s["id"])["sdk_session"]
    assert sdk_id and deck.store.session(s["id"])["cost_usd"] > 0
    deck.close()
    deck2 = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    try:
        assert deck2.store.session(s["id"])["status"] == "paused"
        assert [m["type"] for m in deck2.session(s["id"])["transcript"]][:2] == ["notice", "user"]
        with pytest.raises(DeckError, match="Resume"):
            deck2.send(s["id"], "hi")
        deck2.resume_session(s["id"])
        assert ALL_CLIENTS[-1].options.resume == sdk_id
        deck2.send(s["id"], "hi again")
        wait(deck2, s["id"])
    finally:
        deck2.close()


def test_validation(deck: FlightDeck, projects, tmp_path: Path):
    with pytest.raises(DeckError, match="one-line task"):
        deck.start_session(projects["ApiServer"], "  ")
    with pytest.raises(DeckError, match="two different"):
        deck.add_link(projects["ApiServer"], projects["ApiServer"])
    with pytest.raises(DeckError, match="not found"):
        deck.add_project(str(tmp_path / "missing"))
    plain = tmp_path / "plain"
    plain.mkdir()
    deck.add_project(str(plain))
    pid = next(p["id"] for p in deck.overview()["projects"] if p["name"] == "plain")
    assert deck.isolation_choice(pid)["can_isolate"] is False
    s = deck.start_session(pid, "notes", mode="acceptEdits", send_task=False)  # works without git
    with pytest.raises(DeckError, match="End this project's sessions"):
        deck.remove_project(pid)
    deck.end_session(s["id"])
    deck.remove_project(pid)


def test_ask_policy_prompts_even_with_auto_accept(deck: FlightDeck, projects):
    """Policy "ask": a conflicting edit gets a permission prompt with the reason,
    even in auto-accept mode; non-conflicting edits don't."""
    deck.set_policy("ask")
    a = deck.start_session(projects["ApiServer"], "server", mode="acceptEdits", send_task=False)
    deck.start_session(projects["WebClient"], "settings page", mode="acceptEdits", send_task=False)
    deck.send(a["id"], "write src/app.py fine")
    wait(deck, a["id"])
    assert not events(deck, a["id"], "permission")
    deck.send(a["id"], "write schema/order.json {}")
    wait(deck, a["id"], "needs_you")
    req = events(deck, a["id"], "permission")[0]
    deck.answer(a["id"], req["req_id"], "allow")
    wait(deck, a["id"])
    assert (Path(a["cwd"]) / "schema/order.json").exists()



def test_review_findings_regressions(deck: FlightDeck, projects, tmp_path: Path):
    """Cross-project edits, exact de-dup, briefing overflow, cost across resets."""
    from carrier.flightdeck import coordination as C

    st = deck.store
    a = deck.start_session(projects["ApiServer"], "server", mode="acceptEdits", send_task=False)
    e = deck.start_session(projects["WebClient"], "client", mode="acceptEdits", send_task=False)
    # The editor session edits a file inside the ApiServer folder…
    srv_path = Path(next(p["path"] for p in deck.overview()["projects"] if p["name"] == "ApiServer"))
    pid, rel = deck.locate(st.session(e["id"]), str(srv_path / "src/app.py"))
    assert (pid, rel) == (projects["ApiServer"], "src/app.py")
    C.record_edit(st, st.session(e["id"]), rel, "Edit", project_id=pid)
    # …so the ApiServer session gets a same-file warning for it.
    kinds = [n.kind for n in C.check_edit(st, st.session(a["id"]), "src/app.py")]
    assert kinds == ["same_file"]

    # Exact-path de-dup: two different contract files both produce notices.
    C.record_edit(st, st.session(a["id"]), "schema/x/data.json", "Write")
    C.record_edit(st, st.session(a["id"]), "schema/data.json", "Write")
    assert len(st.pending_events(e["id"])) == 2

    # Briefing overflow: extra notices stay pending instead of being dropped.
    for i in range(20):
        C.record_edit(st, st.session(a["id"]), f"schema/n{i}.json", "Write")
    first = C.briefing(st, st.session(e["id"]))
    assert "more notices will follow" in first
    assert len(st.pending_events(e["id"])) == 22 - C.MAX_BRIEFING_LINES

    # Cost survives a restart/resume (connection cost restarts at 0).
    deck.send(e["id"], "hi")
    wait(deck, e["id"])
    before = st.session(e["id"])["cost_usd"]
    deck.end_session(e["id"])
    st.update_session(e["id"], status="paused", ended=None)
    deck.resume_session(e["id"])
    deck.send(e["id"], "again")
    wait(deck, e["id"])
    assert st.session(e["id"])["cost_usd"] > before


def test_task_sessions_rules_presets_and_completion(deck: FlightDeck, projects):
    room = deck.room
    pid = projects["ApiServer"]
    room.update_project(pid, goal="Serve the public API.", rules="Never change the schema version.")
    room.set_global_rules("Write tests for new code.")
    preset = room.save_preset("Careful", mode="acceptEdits", policy="block")[0]
    room.update_project(pid, preset_id=preset["id"])
    room.add_task(pid, "Add rate limiting", "Per user, 100 req/min.")
    task = room.current_task(pid)
    s = deck.start_task(task["id"])
    wait(deck, s["id"])
    client = ALL_CLIENTS[-1]
    append = client.options.system_prompt["append"]
    assert "Serve the public API." in append and "Never change the schema version." in append
    assert "Write tests for new code." in append and "Add rate limiting" in append
    assert client.prompts[0].startswith("Add rate limiting\n\nPer user")  # task sent with details
    assert client.options.permission_mode == "acceptEdits"  # from the project's preset
    assert deck.store.session(s["id"])["policy"] == "block"
    assert deck.store.task(task["id"])["status"] == "active"

    # Mark done: Claude writes the summary, saved to history; next task becomes current.
    room.add_task(pid, "Next thing")
    deck.complete_task(s["id"])
    wait(deck, s["id"])
    done = deck.store.task(task["id"])
    assert done["status"] == "done" and done["summary"].startswith("Done:")
    assert room.current_task(pid)["title"] == "Next thing"
    assert "[Carrier] The user marked this task as done" in client.prompts[-1]

    # Ending a session on an unfinished task puts it back to planned.
    s2 = deck.start_task(room.current_task(pid)["id"], send_task=False)
    deck.end_session(s2["id"])
    assert deck.store.task(room.current_task(pid)["id"])["status"] == "planned"


def test_standalone_task_scratch_upgrade_assign(deck: FlightDeck, projects, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CARRIER_CONFIG_HOME", str(tmp_path / "cfg"))
    t = deck.create_standalone("prototype a parser")
    assert Path(t["cwd"]).is_dir() and (Path(t["cwd"]) / ".git").exists()  # scratch, git-tracked
    s = deck.start_task(t["id"], mode="acceptEdits")
    wait(deck, s["id"])
    deck.send(s["id"], "write parser.py x")
    wait(deck, s["id"])
    assert (Path(t["cwd"]) / "parser.py").exists()
    assert "Standalone task" in ALL_CLIENTS[-1].options.system_prompt["append"]
    assert [x["title"] for x in deck.overview()["standalone"]] == ["prototype a parser"]
    with pytest.raises(DeckError, match="End the task's session"):
        deck.assign_standalone(t["id"], projects["ApiServer"])
    deck.complete_task(s["id"], summary="Parser handles simple input.")
    deck.end_session(s["id"])
    p = deck.assign_standalone(t["id"], projects["ApiServer"])
    assert p["history"][0]["origin"] == "standalone"
    assert p["tasks"][0]["title"].startswith("Integrate standalone work")

    # Upgrade: a standalone task in a chosen folder becomes a project.
    folder = tmp_path / "newproj"
    folder.mkdir()
    t2 = deck.create_standalone("explore idea", folder=str(folder))
    up = deck.upgrade_standalone(t2["id"], "New Project", goal="An idea worth keeping.")
    assert up["name"] == "New Project" and up["tasks"][0]["title"] == "explore idea"
    with pytest.raises(DeckError, match="no longer|not found|Folder"):
        deck.create_standalone("x", folder=str(tmp_path / "missing"))


def test_review_findings_tasks(deck: FlightDeck, projects, tmp_path: Path, monkeypatch):
    room = deck.room
    pid = projects["ApiServer"]
    # A preset chosen at start puts its rule set into what Claude is told.
    rs = room.save_rule_set("Tests", "ALWAYS WRITE TESTS")[0]
    pre = room.save_preset("Testy", rule_set_id=rs["id"])[0]
    room.add_task(pid, "Do a thing")
    t = room.current_task(pid)
    s = deck.start_task(t["id"], preset_id=pre["id"], send_task=False)
    wait(deck, s["id"])
    assert "ALWAYS WRITE TESTS" in ALL_CLIENTS[-1].options.system_prompt["append"]

    # Stopping while Claude writes the summary does not mark the task done.
    monkeypatch.setattr(deck, "SUMMARY_PROMPT", "slow summary please")
    deck.complete_task(s["id"])
    time.sleep(0.3)
    deck.interrupt(s["id"])
    wait(deck, s["id"])
    assert deck.store.task(t["id"])["status"] != "done"
    assert any(e.get("kind") == "task_not_done" for e in events(deck, s["id"], "notice"))
    deck.end_session(s["id"])


def test_paused_standalone_session_can_be_upgraded(tmp_path: Path, projects, deck: FlightDeck, monkeypatch):
    monkeypatch.setenv("CARRIER_CONFIG_HOME", str(tmp_path / "cfg"))
    t = deck.create_standalone("idea")
    s = deck.start_task(t["id"], send_task=False)
    wait(deck, s["id"])
    deck.close()  # simulate quitting Carrier: the session becomes paused
    d2 = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    try:
        assert d2.store.session(s["id"])["status"] == "paused"
        up = d2.upgrade_standalone(t["id"], "Idea")
        assert up["history"] == [] and up["tasks"][0]["title"] == "idea"
        assert d2.store.session(s["id"])["status"] == "ended"
        assert d2.store.task(t["id"])["status"] == "planned"
    finally:
        d2.close()


def test_extra_folders(deck: FlightDeck, projects, tmp_path: Path):
    room = deck.room
    pid = projects["ApiServer"]
    extra = make_repo(tmp_path / "shared-lib", {"lib.py": "x\n"})
    p = room.add_folder(pid, str(extra))
    assert p["folders"][0]["label"] == "shared-lib"
    with pytest.raises(Exception, match="already part"):
        room.add_folder(pid, str(extra))
    room.add_task(pid, "touch the lib")
    s = deck.start_task(room.current_task(pid)["id"], send_task=False)
    wait(deck, s["id"])
    opts = ALL_CLIENTS[-1].options
    assert str(extra.resolve()) in [str(Path(d).resolve()) for d in opts.add_dirs]
    assert "shared-lib:" in opts.system_prompt["append"]
    row = deck.store.session(s["id"])
    assert deck.locate(row, str(extra / "lib.py")) == (pid, "shared-lib/lib.py")
    deck.end_session(s["id"])
    assert room.remove_folder(pid, p["folders"][0]["path"])["folders"] == []


def test_model_and_effort_for_tasks(deck: FlightDeck, projects):
    room = deck.room
    pid = projects["ApiServer"]
    pre = room.save_preset("Deep", model="opus", effort="high")[0]
    assert pre["effort"] == "high"
    room.add_task(pid, "hard problem")
    t = room.current_task(pid)
    # The start dialog's choice wins over the preset.
    s = deck.start_task(t["id"], preset_id=pre["id"], effort="max", send_task=False)
    wait(deck, s["id"])
    client = ALL_CLIENTS[-1]
    assert client.options.model == "opus" and client.options.effort == "max"
    assert s["effort"] == "max"
    # Model switches live; effort reconnects and resumes the same conversation.
    deck.set_model(s["id"], "sonnet")
    assert client.options.model == "sonnet" and deck.store.session(s["id"])["model"] == "sonnet"
    deck.set_effort(s["id"], "low")
    fresh = ALL_CLIENTS[-1]
    assert fresh is not client and fresh.options.effort == "low"
    assert fresh.options.resume == deck.store.session(s["id"])["sdk_session"]
    assert deck.store.session(s["id"])["effort"] == "low"
    with pytest.raises(DeckError, match="Unknown effort"):
        deck.set_effort(s["id"], "extreme")
    deck.end_session(s["id"])


def test_shared_and_reference_folders(deck: FlightDeck, projects, tmp_path: Path):
    """An edit in a folder that is one project's main folder and another's work
    folder is logged for both; edits in reference folders are blocked."""
    room = deck.room
    api_id, web_id = projects["ApiServer"], projects["WebClient"]
    api_path = Path(deck.store.project(api_id)["path"])
    room.add_folder(web_id, str(api_path), label="server")           # work: ApiServer's main folder
    ref = make_repo(tmp_path / "specs", {"spec.md": "x\n"})
    room.add_folder(web_id, str(ref), kind="reference")
    room.add_task(web_id, "touch both")
    s = deck.start_task(room.current_task(web_id)["id"], send_task=False)
    wait(deck, s["id"])
    row = deck.store.session(s["id"])
    assert deck.owners(row, str(api_path / "schema" / "user.json")) == [
        (web_id, "server/schema/user.json"), (api_id, "schema/user.json")]
    assert str(ref.resolve()) in [str(Path(d).resolve()) for d in ALL_CLIENTS[-1].options.add_dirs]
    assert "READ ONLY" in ALL_CLIENTS[-1].options.system_prompt["append"]
    runner = deck.runners[s["id"]]

    async def edit(path: Path) -> dict:
        data = {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
        out = await runner._before_edit(data, None, None)
        if (out.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny":
            await runner._after_edit(data, None, None)  # the edit went ahead (maybe with a warning)
        return out

    blocked = deck._run(edit(ref / "spec.md"))
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "reference folder" in blocked["hookSpecificOutput"]["permissionDecisionReason"]
    deck._run(edit(api_path / "schema" / "user.json"))
    logged = {(e["project_id"], e["path"]) for e in deck.store._q("SELECT project_id, path FROM edits")}
    assert {(web_id, "server/schema/user.json"), (api_id, "schema/user.json")} <= logged
    deck.end_session(s["id"])


def test_questions_and_proposed_rules(deck: FlightDeck, projects):
    import asyncio

    from carrier.flightdeck import sdk

    s = deck.start_session(projects["DocsSite"], "plan the layout", send_task=False)
    client = ALL_CLIENTS[-1]
    # Carrier's propose_rule tool is offered, and needs no permission prompt.
    assert "carrier" in client.options.mcp_servers and sdk.PROPOSE_RULE in client.options.allowed_tools
    # AskUserQuestion becomes a question card; the answers go back to Claude Code.
    deck.send(s["id"], "ask")
    wait(deck, s["id"], "needs_you")
    q = events(deck, s["id"], "question")[0]
    assert q["questions"][0]["options"][0]["label"] == "In place"
    assert deck.session(s["id"])["pending"][0]["req_id"] == q["req_id"]
    deck.answer(s["id"], q["req_id"], "answers", {"Which layout?": "In place"})
    wait(deck, s["id"])
    assert client.answers == [{"Which layout?": "In place"}]
    assert events(deck, s["id"], "question_done")[0]["decision"] == "answered"
    deck.send(s["id"], "ask")
    wait(deck, s["id"], "needs_you")
    deck.answer(s["id"], events(deck, s["id"], "question")[1]["req_id"], "skip")
    wait(deck, s["id"])
    assert client.answers[-1] is None
    # propose_rule shows a card; the user's edited version is saved where they choose.
    runner = deck.runners[s["id"]]
    tool = sdk.propose_rule_tool(runner._on_propose_rule)
    out = asyncio.run(tool.handler({"text": "- Keep docs short.", "scope": "project", "why": "You said so"}))
    assert "rule card" in out["content"][0]["text"]
    prop = events(deck, s["id"], "rule_proposal")[0]
    assert prop["text"] == "- Keep docs short." and prop["scope"] == "project"
    room = deck.room
    pid = s["project_id"]
    assert room.apply_rule("- Keep docs very short.", "project", pid).startswith("Added to")
    assert room.project(pid)["rules"].endswith("- Keep docs very short.")
    room.apply_rule("- Global one.", "global")
    assert room.overview()["global"]["rules"].endswith("- Global one.")
    msg = room.apply_rule("- Shared.", "rule_set", pid, new_rule_set="Docs style")
    assert "attached" in msg
    rs = next(r for r in room.store.rule_sets() if r["name"] == "Docs style")
    assert rs["id"] in room.store.project_rule_set_ids(pid)
    room.apply_rule("- More.", "rule_set", pid, rule_set_id=rs["id"])
    assert room.store.rule_set(rs["id"])["body"] == "- Shared.\n- More."


def test_workspace_folders_claim_new_folders(deck: FlightDeck, projects, tmp_path: Path):
    """A workspace folder's existing contents are read only; a new folder Claude
    creates there joins the project as a work folder."""
    ws = tmp_path / "ModTools"
    (ws / "mods" / "Other").mkdir(parents=True)
    (ws / "mods" / "Other" / "a.txt").write_text("x\n")
    main = ws / "mods" / "Voice"
    main.mkdir()
    room = deck.room
    pid = room.create_project("Voice", str(main))["id"]
    room.add_folder(pid, str(ws), kind="workspace")  # may contain the main folder
    room.add_task(pid, "release tooling")
    s = deck.start_session(pid, "release tooling", mode="acceptEdits", send_task=False)
    client = ALL_CLIENTS[-1]
    assert str(ws) in client.options.add_dirs
    # Editing an existing folder in the workspace is blocked.
    deck.send(s["id"], f"write {ws / 'mods' / 'Other' / 'a.txt'} changed")
    wait(deck, s["id"])
    assert (ws / "mods" / "Other" / "a.txt").read_text() == "x\n"
    # Writing into a new folder there adds it as a work folder, and the edit goes ahead.
    deck.send(s["id"], f"write {ws / 'tools' / 'release' / 'export.sh'} echo hi")
    wait(deck, s["id"])
    assert (ws / "tools" / "release" / "export.sh").read_text() == "echo hi\n"
    folders = {f["label"]: f for f in room.project(pid)["folders"]}
    assert folders["tools"]["kind"] == "work" and folders["ModTools"]["kind"] == "workspace"
    assert any("Added the new folder tools" in e["text"] for e in events(deck, s["id"], "notice"))
    # Its files are now the project's own: a second write needs no new folder.
    deck.send(s["id"], f"write {ws / 'tools' / 'release' / 'more.sh'} echo 2")
    wait(deck, s["id"])
    assert (ws / "tools" / "release" / "more.sh").exists()
    # The create_work_folder tool does the same without writing a file.
    runner = deck.runners[s["id"]]
    out = deck._run(runner._call_tool("create_work_folder", {"path": str(ws / "publish" / "Voice")}), timeout=10)
    assert "now a work folder" in out and (ws / "publish" / "Voice").is_dir()
    assert "publish" in {f["label"] for f in room.project(pid)["folders"]}
    err = deck._run(runner._call_tool("create_work_folder", {"path": str(ws / "mods" / "Other")}), timeout=10)
    assert err.startswith("Error")
    # A workspace folder can't become a plain work folder while it holds the project's folders.
    from carrier.readyroom.manager import RoomError
    deck.end_session(s["id"])
    with pytest.raises(RoomError, match="only be a workspace"):
        room.set_folder_kind(pid, str(ws.resolve()), "work")


def test_cross_project_tools_and_pasted_images(deck: FlightDeck, projects, tmp_path: Path):
    import base64

    room = deck.room
    pid = projects["DocsSite"]
    room.update_project(pid, rules="- Release notes in CHANGELOG.md.")
    from carrier.core import skills
    skills.save(room.project(pid)["path"], "release", "Use when releasing.", "1. Bump the version.")
    s = deck.start_session(projects["ApiServer"], "copy release setup", mode="acceptEdits", send_task=False)
    runner = deck.runners[s["id"]]
    listing = deck._run(runner._call_tool("list_projects", {}), timeout=10)
    assert "DocsSite" in listing and "ApiServer" in listing
    detail = deck._run(runner._call_tool("get_project", {"name": "docssite"}), timeout=10)
    assert "Release notes in CHANGELOG.md" in detail and "release — Use when releasing." in detail
    assert deck._run(runner._call_tool("get_project", {"name": "nope"}), timeout=10).startswith("Error")
    # propose_skill makes a card; nothing is written until the user approves.
    deck._run(runner._call_tool("propose_skill", {"name": "Release Mod", "description": "Use when releasing.",
                                                   "body": "1. Build."}), timeout=10)
    prop = events(deck, s["id"], "skill_proposal")[0]
    assert prop["name"] == "release-mod"
    assert not skills.exists(room.project(projects["ApiServer"])["path"], "release-mod")
    # show_image: only real images, sniffed from their bytes.
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    (tmp_path / "shot.png").write_bytes(png)
    (tmp_path / "fake.png").write_text("not an image")
    deck._run(runner._call_tool("show_image", {"path": str(tmp_path / "shot.png"), "caption": "Look here"}), timeout=10)
    img = events(deck, s["id"], "image")[0]
    assert img["mime"] == "image/png" and img["caption"] == "Look here"
    bad = deck._run(runner._call_tool("show_image", {"path": str(tmp_path / "fake.png")}), timeout=10)
    assert bad.startswith("Error")
    # Pasted images go to Claude Code as image blocks, with the user's note.
    deck.send(s["id"], "what's wrong here?", [{"mime": "image/png", "data": base64.b64encode(png).decode(),
                                                "note": "the red box"}])
    wait(deck, s["id"])
    client = ALL_CLIENTS[-1]
    assert client.images[-1][0]["source"]["media_type"] == "image/png"
    assert "the red box" in client.prompts[-1] and "what's wrong here?" in client.prompts[-1]
    assert events(deck, s["id"], "user")[-1]["images"][0]["note"] == "the red box"
    with pytest.raises(ValueError):
        deck.send(s["id"], "x", [{"mime": "image/png", "data": base64.b64encode(b"nope").decode()}])


def test_auto_takeoff_starts_clear_routines(deck: FlightDeck, projects):
    from carrier.readyroom import routines as R

    room = deck.room
    pid = projects["DocsSite"]
    gid = room.save_group(pid, "Docs", "normal")["groups"][0]["id"]
    R.save(room, pid, "Tidy docs", "Tidy them.", "manual", group_id=gid)
    R.save(room, pid, "Push", "git push", "manual", "reviewed")
    for r in room.store.routines(pid):
        R.run(room, r["id"])
    ov = deck.overview()
    assert {q["title"]: bool(q["blockers"]) for q in ov["takeoff"]} == {"Tidy docs": False, "Push": True}
    group = next(p for p in ov["projects"] if p["id"] == pid)["groups"][0]
    assert group["current_task"]["title"] == "Tidy docs"
    assert deck.takeoff_tick(every=0) == []  # off by default
    deck.set_auto_takeoff(True)
    deck.takeoff_tick(every=0)
    started = [s for s in deck.store.sessions(pid) if s["task"] == "Tidy docs"]
    assert len(started) == 1  # the gated "Push" waits for a clear review
    assert [q["title"] for q in deck.overview()["takeoff"]] == ["Push"]
    deck.takeoff_tick(every=0)
    assert len([s for s in deck.store.sessions(pid) if s["task"] == "Tidy docs"]) == 1  # not started twice


def test_claude_asks_where_new_work_goes(deck: FlightDeck, projects):
    import asyncio

    room = deck.room
    pid = projects["DocsSite"]
    room.add_task(pid, "Write intro")
    gid = room.save_group(pid, "Art", "low")["groups"][0]["id"]
    s = deck.start_task(room.current_task(pid)["id"], mode="acceptEdits", send_task=False)
    runner = deck.runners[s["id"]]
    listing = deck._run(runner._call_tool("list_tasks", {}), timeout=10)
    assert "Main queue" in listing and "Art (priority low)" in listing and "This session's task" in listing

    def ask(tool, args, decision, payload):
        fut = asyncio.run_coroutine_threadsafe(runner._call_tool(tool, args), deck._loop)
        wait(deck, s["id"], "needs_you")
        req = deck.session(s["id"])["pending"][0]["req_id"]
        deck.answer(s["id"], req, decision, payload)
        return fut.result(timeout=10)

    # Claude suggests a new group; the user picks the existing Art group instead.
    out = ask("propose_work", {"title": "Draw icons", "details": "- 16px icons", "suggest": "new_group",
                               "new_group": "Icons", "reasoning": "Separate art work."},
              "work", {"title": "Draw icons", "details": "- 16px icons", "where": "queue", "group_id": gid,
                       "position": "end"})
    assert "Don't do it in this session" in out
    assert [t["title"] for t in room.queue(pid, gid)] == ["Draw icons"]
    card = events(deck, s["id"], "work_proposal")[0]
    assert card["suggest"]["suggest"] == "new_group" and any(q["name"] == "Art" for q in card["queues"])
    # Or into the current task: its scope grows, on record.
    out = ask("propose_work", {"title": "Fix typo", "details": "- in README", "suggest": "current"},
              "work", {"title": "Fix typo", "details": "- in README", "where": "current"})
    assert "part of it" in out
    cur = deck.store.task(s["task_id"])
    assert "Fix typo" in cur["details"]
    assert room.task_history(cur["id"])[-1]["by"] == "claude"
    # Declined: nothing changes.
    out = ask("propose_work", {"title": "Rewrite all", "details": "x", "suggest": "main"}, "decline", "")
    assert "didn't add" in out and "Rewrite all" not in [t["title"] for t in deck.store.tasks(pid)]
    assert any(e["text"] == "Not added." for e in events(deck, s["id"], "proposal_done"))


def test_editing_a_running_task_tells_the_session(deck: FlightDeck, projects):
    room = deck.room
    pid = projects["DocsSite"]
    room.add_task(pid, "Write intro", "- one paragraph")
    s = deck.start_task(room.current_task(pid)["id"], mode="acceptEdits", send_task=False)
    client = ALL_CLIENTS[-1]
    before = dict(deck.store.task(s["task_id"]))
    room.update_task(s["task_id"], details="- two paragraphs\n- a diagram")
    after = deck.store.task(s["task_id"])
    assert deck.notify_task_change(s["task_id"], before, after) == [s["id"]]
    end = time.time() + 10
    while not client.prompts and time.time() < end:
        time.sleep(0.02)
    wait(deck, s["id"])
    msg = client.prompts[-1]
    assert msg.startswith("[Carrier Ready Room] The user just changed the task")
    assert "- - one paragraph" in msg and "+ - a diagram" in msg
    assert any(e["kind"] == "task_changed" for e in events(deck, s["id"], "notice"))
    # A change that changes nothing isn't sent.
    assert deck.notify_task_change(s["task_id"], after, after) == []


def test_bring_back_an_own_copy(deck: FlightDeck, projects):
    import subprocess

    room = deck.room
    pid = projects["ApiServer"]
    room.add_task(pid, "Add notes")
    s = deck.start_task(room.current_task(pid)["id"], isolation="worktree", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "write notes.txt from the copy")
    wait(deck, s["id"])
    copy = Path(deck.store.session(s["id"])["cwd"])
    with pytest.raises(DeckError, match="End the session"):
        deck.bring_back(s["id"], "merge")
    deck.end_session(s["id"])
    with pytest.raises(DeckError, match="aren't committed"):
        deck.bring_back(s["id"], "merge")
    subprocess.run(["git", "add", "-A"], cwd=copy, check=True)
    subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "notes"], cwd=copy, check=True)
    assert [c["id"] for c in deck.leftover_copies(pid)] == [s["id"]]
    out = deck.bring_back(s["id"], "merge")
    main = Path(deck.store.project(pid)["path"])
    assert "Merged" in out["text"] and (main / "notes.txt").read_text() == "from the copy\n"
    assert not copy.exists() and deck.leftover_copies(pid) == []


def test_question_fallbacks(deck: FlightDeck, projects):
    import asyncio

    s = deck.start_session(projects["DocsSite"], "colours", mode="acceptEdits", send_task=False)
    # Routed questions don't trigger the fallback…
    deck.send(s["id"], "ask")
    wait(deck, s["id"], "needs_you")
    deck.answer(s["id"], events(deck, s["id"], "question")[0]["req_id"], "skip")
    wait(deck, s["id"])
    assert events(deck, s["id"], "question_fallback") == []
    # …a question that never reached Carrier is shown in the chat.
    deck.send(s["id"], "ask-unrouted")
    wait(deck, s["id"])
    fb = events(deck, s["id"], "question_fallback")
    assert fb and fb[0]["questions"][0]["question"] == "Which colour?"
    # Carrier's own ask_user tool works whatever Claude Code does.
    runner = deck.runners[s["id"]]
    fut = asyncio.run_coroutine_threadsafe(runner._call_tool("ask_user", {"questions": [
        {"question": "Size?", "options": [{"label": "Small"}, {"label": "Large"}]}]}), deck._loop)
    wait(deck, s["id"], "needs_you")
    q = [e for e in events(deck, s["id"], "question") if e["questions"][0]["question"] == "Size?"][0]
    deck.answer(s["id"], q["req_id"], "answers", {"Size?": "Large"})
    assert fut.result(timeout=10) == "The user answered:\n- Size? → Large"


def test_bring_back_refuses_unsafe_merges(deck: FlightDeck, projects):
    import subprocess

    room = deck.room
    pid = projects["ApiServer"]
    main = Path(deck.store.project(pid)["path"])
    room.add_task(pid, "Edit app")
    s = deck.start_task(room.current_task(pid)["id"], isolation="worktree", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "write src/app.py from the copy")
    wait(deck, s["id"])
    copy = Path(deck.store.session(s["id"])["cwd"])
    deck.end_session(s["id"])
    git = lambda cwd, *a: subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", *a], cwd=cwd,
                                         check=True, capture_output=True)
    git(copy, "commit", "-qam", "copy edit")
    # A conflicting change in the main folder: the merge is refused and nothing is left half-done.
    (main / "src" / "app.py").write_text("from main\n")
    git(main, "commit", "-qam", "main edit")
    with pytest.raises(DeckError, match="conflicts .*src/app.py"):
        deck.bring_back(s["id"], "merge")
    assert not (main / ".git" / "MERGE_HEAD").exists() and (main / "src" / "app.py").read_text() == "from main\n"
    # A detached HEAD is refused (the merge would be lost when switching back).
    git(main, "checkout", "-q", "--detach")
    with pytest.raises(DeckError, match="detached HEAD"):
        deck.bring_back(s["id"], "merge")
    git(main, "checkout", "-q", "-")
    # Discard removes the copy and its branch.
    branch = deck.store.session(s["id"])["branch"]
    assert "Copy and branch" in deck.bring_back(s["id"], "discard")["text"]
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", branch], cwd=main, capture_output=True).returncode != 0
