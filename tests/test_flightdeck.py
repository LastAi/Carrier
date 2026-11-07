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
