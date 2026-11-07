# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Flight Deck store + coordination rules (no model, no SDK)."""

import time
from pathlib import Path

import pytest

from carrier.flightdeck import coordination as C
from carrier.core.db import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "fd.db")


@pytest.fixture
def projects(store: Store):
    """Example: an API server ⇄ web client (linked via a JSON schema), plus an unrelated project."""
    srv = store.add_project("API Server", "/p/srv")
    cli = store.add_project("Web Client", "/p/cli")
    docs = store.add_project("Docs Site", "/p/docs")
    store.add_link(srv, cli, "client uses the server's API schema",
                   ["schema/**/*.json"], ["src/api-client/**", "*.schema.json"])
    s_srv = store.add_session(project_id=srv, task="add user endpoints", cwd="/p/srv", status="working")
    s_cli = store.add_session(project_id=cli, task="user settings page", cwd="/p/cli", status="idle")
    s_docs = store.add_session(project_id=docs, task="update docs", cwd="/p/docs", status="working")
    return {"server": srv, "client": cli, "docs": docs, "s_srv": s_srv, "s_cli": s_cli, "s_docs": s_docs}


def test_glob_matching():
    assert C.matches("schema/users/user.json", ["schema/**/*.json"])
    assert C.matches("schema/user.json", ["schema/**/*.json"])
    assert not C.matches("data/other/user.json", ["schema/**/*.json"])
    assert C.matches("deep/x/user.schema.json", ["*.schema.json"])  # no slash: anywhere
    assert C.matches("src/api-client/a/b.py", ["src/api-client/**"])
    assert not C.matches("src/api-clientx.py", ["src/api-client/**"])


def test_contract_edit_notifies_linked_session_only(store: Store, projects):
    s_srv = store.session(projects["s_srv"])
    notices = C.check_edit(store, s_srv, "schema/users/user.json")
    assert [n.kind for n in notices] == ["contract"]
    assert "Web Client" in notices[0].text and "user settings page" in notices[0].text

    notified = C.record_edit(store, s_srv, "schema/users/user.json", "Write")
    assert notified == [projects["s_cli"]]
    assert store.pending_events(projects["s_docs"]) == []  # unrelated project never hears about it
    # No duplicate notice for a second edit of the same file.
    assert C.record_edit(store, s_srv, "schema/users/user.json", "Edit") == []

    brief = C.briefing(store, store.session(projects["s_cli"]))
    assert "changed the shared contract file schema/users/user.json" in brief
    assert store.pending_events(projects["s_cli"]) == []  # delivered
    assert C.briefing(store, store.session(projects["s_docs"])) is None  # nothing related


def test_non_contract_edit_is_quiet(store: Store, projects):
    s_srv = store.session(projects["s_srv"])
    assert C.check_edit(store, s_srv, "src/routes.py") == []
    assert C.record_edit(store, s_srv, "src/routes.py", "Edit") == []


def test_same_folder_and_worktree_overlap(store: Store, projects):
    a1 = store.session(projects["s_srv"])
    a2_id = store.add_session(project_id=projects["server"], task="fix validation", cwd="/p/srv", status="working")
    a3_id = store.add_session(project_id=projects["server"], task="refactor db layer", cwd="/wt/3", worktree=1,
                              status="working")
    C.record_edit(store, a1, "src/db.py", "Edit")
    kinds = sorted(n.kind for n in C.check_edit(store, store.session(a2_id), "src/db.py"))
    assert kinds == ["same_file"]
    kinds = sorted(n.kind for n in C.check_edit(store, store.session(a3_id), "src/db.py"))
    assert kinds == ["worktree_overlap"]


def test_first_briefing_lists_related_then_only_news(store: Store, projects):
    s_cli = store.session(projects["s_cli"])
    C.record_edit(store, store.session(projects["s_srv"]), "src/routes.py", "Edit")
    first = C.briefing(store, s_cli)
    assert "API Server session" in first and "src/routes.py" in first
    assert "Docs Site" not in first
    assert C.briefing(store, store.session(projects["s_cli"])) is None  # nothing new
    time.sleep(0.01)
    C.record_edit(store, store.session(projects["s_srv"]), "src/new.py", "Write")
    assert "src/new.py" in C.briefing(store, store.session(projects["s_cli"]))


def test_ended_sessions_dont_count(store: Store, projects):
    store.update_session(projects["s_cli"], status="ended", ended=time.time())
    notices = C.check_edit(store, store.session(projects["s_srv"]), "schema/x.json")
    assert "no session is active there" in notices[0].text
    assert C.record_edit(store, store.session(projects["s_srv"]), "schema/x.json", "Write") == []


def test_allow_for_session_is_scoped():
    from carrier.flightdeck.sdk import allow_key

    assert allow_key("Bash", {"command": "git status"})[0] == "Bash:git"
    assert allow_key("Bash", {"command": "rm -rf build"})[0] == "Bash:rm"  # git approval doesn't cover rm
    assert allow_key("WebFetch", {"url": "https://docs.example.com/a"})[0] == "WebFetch:docs.example.com"
    assert allow_key("Edit", {"file_path": "x"})[0] == "Edit"
