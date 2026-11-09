# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Side-by-side rows, selections, themes, user settings and ReviewSession."""

from pathlib import Path

import pytest

from carrier.core import themes, usersettings
from carrier.core.gitutil import parse_diff
from carrier.core.llm import FakeBackend
from carrier.debrief.session import ReviewSession, SessionError, inspect_folder, start_tracking
from carrier.debrief.sidebyside import hunk_rows_split, hunk_rows_unified, selection_from_rows

DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,4 +1,5 @@
 keep
-old1
-old2
+new1
+new2
+new3
 tail
"""


def _hunk():
    return parse_diff(DIFF)[0].hunks[0]


def test_split_rows_pair_changes():
    rows = hunk_rows_split(_hunk())
    kinds = [r.kind for r in rows]
    assert kinds == ["hunk", "context", "change", "change", "add", "context"]
    assert (rows[2].old_no, rows[2].old, rows[2].new_no, rows[2].new) == (2, "old1", 2, "new1")
    assert (rows[4].old_no, rows[4].new_no, rows[4].new) == (None, 4, "new3")
    assert (rows[5].old_no, rows[5].new_no) == (4, 5)


def test_unified_rows_and_selection_sides():
    rows = hunk_rows_unified(_hunk())
    assert [r.kind for r in rows] == ["hunk", "context", "del", "del", "add", "add", "add", "context"]
    # Selecting only removed lines -> refers to the old file.
    sel = selection_from_rows(rows, 2, 3)
    assert (sel.side, sel.start, sel.end, sel.text) == ("old", 2, 3, "old1\nold2")
    # Context + added lines only -> new-file lines.
    sel = selection_from_rows(rows, 4, 7)
    assert (sel.side, sel.start, sel.end) == ("new", 2, 5)
    # Mixed removed + other lines -> "both": nothing highlighted is dropped.
    sel = selection_from_rows(rows, 1, 5)
    assert sel.side == "both" and (sel.old_start, sel.old_end) == (1, 3) and (sel.start, sel.end) == (1, 3)
    assert sel.text.splitlines() == [" keep", "-old1", "-old2", "+new1", "+new2"]
    assert "before →" in sel.label()


def test_split_selection_by_side():
    rows = hunk_rows_split(_hunk())
    assert selection_from_rows(rows, 2, 4, side="old").text == "old1\nold2"
    assert selection_from_rows(rows, 4, 2, side="new").text == "new1\nnew2\nnew3"  # reversed ok
    assert selection_from_rows(rows, 0, 0) is None  # only the @@ row
    # Starting on a deleted-only row but asking for the new side still finds new lines.
    assert selection_from_rows(rows, 4, 5, side="new").text == "new3\ntail"


def test_themes_resolve_and_custom_colors():
    for p in themes.list_presets():
        t = themes.resolve(p["name"])
        assert set(t["colors"]) == set(themes.KEYS)
    t = themes.resolve("red-green", {"added_fg": "#ABCDEF", "bogus": "#000000", "removed_fg": "red"})
    assert t["colors"]["added_fg"] == "#abcdef"  # applied
    assert "bogus" not in t["colors"]
    assert t["colors"]["removed_fg"] == themes.PRESETS["red-green"]["colors"]["removed_fg"]  # invalid ignored
    assert themes.resolve("nope")["name"] == "default"


def test_user_settings_roundtrip():
    assert usersettings.load()["theme"] == "default"
    usersettings.update(theme="blue-yellow", junk=1)
    assert usersettings.load()["theme"] == "blue-yellow"
    for i in range(15):
        usersettings.remember_repo(f"/r{i}")
    recent = usersettings.load()["recent_repos"]
    assert recent[0] == "/r14" and len(recent) == usersettings.MAX_RECENT


def test_start_tracking_plain_folder(tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello")
    assert inspect_folder(str(folder))["is_repo"] is False
    info = start_tracking(str(folder))
    assert info["is_repo"] and info["has_commits"] and info["commits"]


def test_session_full_flow(repo: Path, claude_home: Path, fake_backend: FakeBackend):
    s = ReviewSession(repo, backend=fake_backend)
    try:
        ov = s.start(progress=lambda m: None)
        first = ov["items"][0]
        assert first["severity"] == "high"
        detail = s.item(first["id"], split=True)
        assert detail["split"] and any(r["kind"] == "change" or r["kind"] == "add" for r in detail["rows"])
        assert s.explain(first["id"])["why_source"] == "request"

        # Highlight rows and ask about them.
        rows = detail["rows"]
        idx = [n for n, r in enumerate(rows) if r["kind"] in ("add", "change")]
        sel = s.selection(first["id"], True, idx[0], idx[1], side="new")
        assert sel["path"] == "save.py" and sel["side"] == "new"
        ans = s.ask(first["id"], "why sleep?", sel)
        assert ans["selection_label"].startswith("save.py lines")
        prompt = fake_backend.calls[-1]["prompt"]
        assert "Code the reviewer highlighted" in prompt and sel["text"] in prompt

        with pytest.raises(SessionError):
            s.decide(first["id"], "fix", "")  # fix needs an instruction
        s.decide(first["id"], "fix", "keep a test")
        s.add_note(first["id"], "remember this")
        result = s.finish()
        assert result["checkpoint_moved"] and result["fixes"] and "keep a test" in result["fixes_text"]
        assert result["undecided"] == len(ov["items"]) - 1
        with pytest.raises(SessionError, match="finished"):
            s.decide(first["id"], "approve")  # a finished review is closed
    finally:
        s.close()


def test_session_resume_and_nothing_to_review(repo: Path):
    s = ReviewSession(repo, backend=None)
    ov = s.start()
    s.decide(ov["items"][0]["id"], "approve")
    s.close()
    s2 = ReviewSession(repo, backend=None)
    assert s2.start()["decided"] == 1  # resumed
    s2.finish()
    with pytest.raises(SessionError, match="Nothing to review"):
        s2.start()
    s2.close()


def test_not_a_repo(tmp_path: Path):
    with pytest.raises(SessionError):
        ReviewSession(tmp_path, backend=None)


def test_close_cancels_running_work(repo: Path):
    """Closing a session must not leave callers blocked on a running model call."""
    import asyncio
    import threading

    started = threading.Event()

    class SlowBackend:
        async def ask(self, **kw):
            started.set()
            await asyncio.sleep(60)
            return "{}"

    s = ReviewSession(repo, backend=None)
    ov = s.start()
    s.backend = SlowBackend()
    result: dict = {}

    def call():
        try:
            s.explain(ov["items"][0]["id"])
        except SessionError as exc:
            result["error"] = str(exc)

    t = threading.Thread(target=call)
    t.start()
    assert started.wait(5)
    s.close()
    t.join(5)
    assert not t.is_alive() and "closed" in result.get("error", "")


def test_settings_concurrent_updates_are_not_lost():
    import threading

    threads = [threading.Thread(target=usersettings.remember_repo, args=(f"/p{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(usersettings.load()["recent_repos"]) == 8


def test_cancel_start_saves_nothing(repo: Path):
    """The Cancel button: stops preparing, leaves no review and no snapshot pin."""
    import asyncio
    import subprocess
    import threading

    from carrier.core.config import load_config
    from carrier.debrief.store import all_reviews

    started = threading.Event()

    class SlowBackend:
        async def ask(self, **kw):
            started.set()
            await asyncio.sleep(60)
            return "{}"

    s = ReviewSession(repo, backend=SlowBackend())
    result: dict = {}

    def run():
        try:
            s.start()
        except SessionError as exc:
            result["error"] = str(exc)

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(5)
    assert s.cancel_start() is True
    t.join(5)
    assert not t.is_alive() and "Cancelled" in result["error"]
    assert all_reviews(load_config(repo)) == []
    refs = subprocess.run(["git", "for-each-ref", "refs/carrier/debrief/reviews"], cwd=repo,
                          capture_output=True, text=True).stdout
    assert refs.strip() == ""
    # Starting again afterwards works.
    s.backend = None
    assert s.start()["items"]
    s.close()


def test_project_review_parts_context_tasks_and_fix_task(repo: Path, claude_home: Path, tmp_path: Path):
    """A project review: extra folder included, goal/rules reach the agents, items name
    the Flight Deck task, and requested fixes come back as the project's next task."""
    import subprocess

    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.readyroom.manager import ReadyRoom

    from .conftest import scripted_handler

    lib = tmp_path / "lib"
    lib.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=lib, check=True)
    (lib / "x.py").write_text("A = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=lib, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=lib, check=True)
    (lib / "x.py").write_text("A = 2\n")

    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    pid = room.create_project("App", str(repo), goal="Keeps user data safe.", first_task="later")["id"]
    room.update_project(pid, rules="Never weaken tests.")
    room.add_folder(pid, str(lib))
    tid = store.add_task("Make saves robust", pid)
    sid = store.add_session(project_id=pid, task="t", cwd=str(repo), task_id=tid, status="ended")
    store.add_edit(sid, pid, "save.py", "Edit")
    store.add_edit(sid, pid, "lib/x.py", "Edit")

    from carrier.debrief import projects as DP

    DP.set_builtin_option(store, "security", "effort", "xhigh", pid)
    DP.set_builtin_option(store, "security", "model", "opus", pid)
    backend = FakeBackend(scripted_handler)
    s = ReviewSession(repo, backend=backend, project=project_spec(store, pid))
    ov = s.start()
    assert ov["project"] == "App" and ov["folders"] == ["lib"]
    paths = [p for it in ov["items"] for p in it["paths"]]
    assert "lib/x.py" in paths
    assert any("Make saves robust" in it["tasks"] for it in ov["items"])
    sec = [c for c in backend.calls if "Security" in c["prompt"][:400]]
    assert sec and all(c["effort"] == "xhigh" and c["model"] == "opus" for c in sec)
    prompts = "\n".join(c["prompt"] for c in backend.calls)
    assert "Keeps user data safe." in prompts and "Never weaken tests." in prompts
    assert all(c.get("add_dirs") in (None, [str(lib)], [str(lib.resolve())]) for c in backend.calls)
    first = ov["items"][0]["id"]
    s.decide(first, "fix", "keep the old test")
    res = s.finish()
    assert res["fix_task"]["title"].startswith("Fix review findings (1)")
    queue = room.project(pid)["tasks"]
    assert queue[0]["title"].startswith("Fix review findings") and "keep the old test" in queue[0]["details"]
    # Both folders' checkpoints moved: nothing left to review.
    s.close()
    s2 = ReviewSession(repo, backend=None, project=project_spec(store, pid))
    with pytest.raises(SessionError, match="Nothing to review"):
        s2.start()
    s2.close()


def test_project_review_robustness(repo: Path, claude_home: Path, tmp_path: Path):
    """Review findings: a broken extra folder blocks finish without moving any
    checkpoint; a deleted one doesn't break listing; a swapped folder isn't resumed."""
    import shutil
    import subprocess

    from carrier.core import gitutil
    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.debrief.store import is_stale
    from carrier.readyroom.manager import ReadyRoom

    def mk(name: str) -> Path:
        d = tmp_path / name
        d.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=d, check=True)
        (d / "x.py").write_text("A = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-qm", "x"], cwd=d, check=True)
        (d / "x.py").write_text("A = 2\n")
        return d

    lib = mk("lib")
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    pid = room.create_project("App", str(repo))["id"]
    room.add_folder(pid, str(lib))
    s = ReviewSession(repo, backend=None, project=project_spec(store, pid))
    s.start()
    before = gitutil.get_checkpoint(repo)
    shutil.rmtree(lib / ".git")  # the extra folder breaks mid-review
    with pytest.raises(SessionError, match="no longer a git repository"):
        s.finish()
    assert gitutil.get_checkpoint(repo) == before  # nothing moved
    shutil.rmtree(lib)  # …and is deleted: staleness checks still work
    assert is_stale(s.cfg, s.review) is True
    s.close()

    # Swap the extra folder for a different one with the same label: not resumed.
    room.remove_folder(pid, str(lib.resolve()))
    lib2 = mk("other-lib")
    room.add_folder(pid, str(lib2), label="lib")
    s2 = ReviewSession(repo, backend=None, project=project_spec(store, pid))
    ov = s2.start()
    assert ov["folders"] == ["lib"] and any("lib/x.py" in it["paths"] for it in ov["items"])
    assert s2.review.parts[0]["repo"] == str(lib2.resolve()) or Path(s2.review.parts[0]["repo"]).resolve() == lib2.resolve()
    s2.close()


def test_projects_keep_their_own_checkpoints(repo: Path, claude_home: Path, tmp_path: Path):
    """Projects A and B both work in one shared folder: finishing B's review must not
    mark A's changes there as reviewed."""
    import subprocess

    from carrier.core import gitutil
    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.readyroom.manager import ReadyRoom

    mains = {}
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=d, check=True)
        (d / "x.txt").write_text("1\n")
        mains[name] = d
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    a = room.create_project("A", str(mains["a"]))["id"]
    b = room.create_project("B", str(mains["b"]))["id"]
    room.add_folder(a, str(repo), label="app")
    room.add_folder(b, str(repo), label="app")
    room.set_folder_options(a, str(repo), part="**")
    room.set_folder_options(b, str(repo), part="**")
    sb = ReviewSession(mains["b"], backend=None, project=project_spec(store, b))
    ov = sb.start()
    assert any(p.startswith("app/") for it in ov["items"] for p in it["paths"])
    sb.finish()
    sb.close()
    assert gitutil.resolve(repo, gitutil.checkpoint_ref(str(b)))  # B's own marker moved…
    assert gitutil.get_checkpoint(repo, str(a)) is None          # …A's did not
    sa = ReviewSession(mains["a"], backend=None, project=project_spec(store, a))
    assert any(p.startswith("app/") for it in sa.start()["items"] for p in it["paths"])  # A still has it all
    sa.close()


def test_projects_split_from_one_repository(tmp_path: Path, claude_home: Path):
    """Two projects whose folders are subfolders of one repository (a workspace
    split in place) review only their own folders, with their own checkpoints."""
    import subprocess

    from carrier.core import gitutil
    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.readyroom.manager import ReadyRoom

    ws = tmp_path / "workspace"
    for d in ("mods/anim", "mods/demo", "tools/editor"):
        (ws / d).mkdir(parents=True)
        (ws / d / "main.txt").write_text("one\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=ws, check=True)
    for d in ("mods/anim", "mods/demo", "tools/editor"):
        (ws / d / "main.txt").write_text("one\ntwo\n")

    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    anim = room.create_project("Animation", str(ws / "mods" / "anim"))["id"]
    room.add_folder(anim, str(ws / "mods" / "demo"), label="demo")  # same repository, as work
    editor = room.create_project("Editor", str(ws / "tools" / "editor"))["id"]

    sa = ReviewSession(ws / "mods" / "anim", backend=None, project=project_spec(store, anim))
    paths = sorted(p for it in sa.start(from_ref="HEAD")["items"] for p in it["paths"])
    assert paths == ["demo/main.txt", "main.txt"]  # relative to each folder; editor not included
    sa.finish()
    sa.close()
    assert gitutil.get_checkpoint(ws, str(anim))
    assert gitutil.get_checkpoint(ws, str(editor)) is None

    se = ReviewSession(ws / "tools" / "editor", backend=None, project=project_spec(store, editor))
    ov = se.start(from_ref="HEAD")
    assert [p for it in ov["items"] for p in it["paths"]] == ["main.txt"]
    se.close()
    # The animation review is finished; nothing new there since.
    sa2 = ReviewSession(ws / "mods" / "anim", backend=None, project=project_spec(store, anim))
    with pytest.raises(SessionError, match="Nothing to review"):
        sa2.start()
    sa2.close()


def test_worktree_of_a_subfolder_project(tmp_path: Path, monkeypatch):
    import subprocess

    from carrier.flightdeck import worktrees

    ws = tmp_path / "ws"
    (ws / "mods" / "anim").mkdir(parents=True)
    (ws / "mods" / "anim" / "a.txt").write_text("x\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=ws, check=True)
    monkeypatch.setattr(worktrees, "worktrees_root", lambda: tmp_path / "copies")
    folder, branch = worktrees.create(ws / "mods" / "anim", "Animation", 7, "Do it")
    assert folder == tmp_path / "copies" / "animation" / "7" / "mods" / "anim"
    assert (folder / "a.txt").exists()
    worktrees.remove(ws / "mods" / "anim", folder)
    assert not (tmp_path / "copies" / "animation" / "7").exists()


def test_whole_project_scan(tmp_path: Path, claude_home: Path):
    """A scan reviews every file as it is now with the chosen agents, and moves no checkpoint."""
    import json
    import subprocess

    from carrier.core import gitutil
    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.readyroom.manager import ReadyRoom

    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("TIMEOUT = 37\n")
    (proj / "package-lock.json").write_text("{}\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=proj, check=True)
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    pid = room.create_project("P", str(proj))["id"]
    seen: list[str] = []

    def handler(system: str, prompt: str) -> str:
        seen.append(system + prompt)
        return json.dumps({"flags": []})

    s = ReviewSession(proj, backend=FakeBackend(handler), project=project_spec(store, pid))
    with pytest.raises(SessionError, match="Pick at least one"):
        s.start(scan=True, agents=[])
    ov = s.start(scan=True, agents=["security"])
    paths = [p for it in ov["items"] for p in it["paths"]]
    assert paths == ["src/app.py"]  # the whole project, lock files left out, with no history needed
    assert s.review.scan and "whole-project scan" in "".join(seen)
    assert s.review.criteria_used == ["security"]
    s.finish()
    s.close()
    assert gitutil.get_checkpoint(proj, str(pid)) is None  # a scan doesn't mark anything reviewed


def test_shared_folders_in_project_reviews(repo: Path, claude_home: Path, tmp_path: Path):
    """A work folder shared with another project: the agents are told, changes by the other
    project's sessions are marked as that project's work, and the folder can be limited to this
    project's own changes, given a note, or left out of reviews."""
    import subprocess

    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.readyroom import ownership
    from carrier.readyroom.manager import ReadyRoom, RoomError

    from .conftest import scripted_handler

    shared = tmp_path / "shared"
    shared.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=shared, check=True)
    for name in ("mine.py", "theirs.py", "hand.py"):
        (shared / name).write_text("A = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=shared, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=shared, check=True)
    for name in ("mine.py", "theirs.py", "hand.py"):
        (shared / name).write_text("A = 2\n")

    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    pid = room.create_project("App", str(repo))["id"]
    room.add_folder(pid, str(shared))
    other_main = tmp_path / "anim"
    other_main.mkdir()
    oid = room.create_project("Animation", str(other_main))["id"]
    room.add_folder(oid, str(shared))
    assert room.project(pid)["folders"][0]["shared_with"] == ["Animation"]
    # Edits in a shared folder are logged for every project using it.
    mine_t = store.add_task("Add ship formats", pid)
    their_t = store.add_task("New animation format", oid)
    s_mine = store.add_session(project_id=pid, task="t", cwd=str(repo), task_id=mine_t, status="ended")
    s_their = store.add_session(project_id=oid, task="t", cwd=str(other_main), task_id=their_t, status="ended")
    for p in (pid, oid):
        store.add_edit(s_mine, p, "shared/mine.py", "Edit")
        store.add_edit(s_their, p, "shared/theirs.py", "Edit")

    assert room.project(pid)["folders"][0]["share"] == "shared"  # two projects work in it
    with pytest.raises(RoomError, match="Animation"):
        room.set_folder_options(pid, str(shared), share="own")

    # A shared space: only this project's part (none yet) and files its sessions changed.
    backend = FakeBackend(scripted_handler)
    s = ReviewSession(repo, backend=backend, project=project_spec(store, pid))
    ov = s.start()
    paths = {p for it in ov["items"] for p in it["paths"]}
    assert "shared/mine.py" in paths and "shared/theirs.py" not in paths and "shared/hand.py" not in paths
    s.close()

    # Its part (patterns) and the files it created there are its own too.
    room.set_folder_options(pid, str(shared), part="theirs.py", note="Only ships/ and mine.py are ours.")
    ownership.claim(store, pid, str(shared), "hand.py")
    backend = FakeBackend(scripted_handler)
    s = ReviewSession(repo, backend=backend, project=project_spec(store, pid))
    ov = s.start(fresh=True)
    tasks = {p: it["tasks"] for it in ov["items"] for p in it["paths"]}
    assert {"shared/mine.py", "shared/theirs.py", "shared/hand.py"} <= set(tasks)
    assert tasks["shared/theirs.py"] == ["New animation format (Animation)"]
    prompts = "\n".join(c["prompt"] for c in backend.calls)
    assert "shared space other projects work in too (Animation)" in prompts
    assert "files matching theirs.py" in prompts and "Only ships/ and mine.py are ours." in prompts
    assert "Edited by another project's session: Animation" in prompts
    s.close()

    # Not reviewed at all.
    room.set_folder_options(pid, str(shared), review="skip")
    s = ReviewSession(repo, backend=None, project=project_spec(store, pid))
    ov = s.start(fresh=True)
    assert not any(p.startswith("shared/") for it in ov["items"] for p in it["paths"])
    s.close()


def test_reviews_leave_out_other_projects_main_folders(tmp_path: Path, claude_home: Path):
    """A work folder that holds another project's main folder: that one isn't reviewed."""
    import subprocess

    from carrier.core.db import Store
    from carrier.debrief.projects import project_spec
    from carrier.readyroom.manager import ReadyRoom

    ws, main = tmp_path / "ws", tmp_path / "outer"
    (ws / "tools" / "inner").mkdir(parents=True)
    main.mkdir()
    for d in (ws, main):
        for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=d, check=True)
    (main / "m.txt").write_text("1\n")
    (ws / "top.txt").write_text("1\n")
    (ws / "tools" / "inner" / "in.txt").write_text("1\n")
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    outer = room.create_project("Outer", str(main))["id"]
    room.create_project("Inner", str(ws / "tools" / "inner"))
    room.add_folder(outer, str(ws), label="ws")
    assert room.project(outer)["folders"][0]["foreign"] == ["Inner"]
    spec = project_spec(store, outer)
    assert "Inner" in spec.folder_notes.get("ws", "")
    s = ReviewSession(main, backend=None, project=spec)
    paths = sorted(p for it in s.start()["items"] for p in it["paths"])
    assert paths == ["m.txt", "ws/top.txt"]
    s.close()
