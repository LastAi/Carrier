# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""A project's pre-approved actions: what sessions may do without asking."""

from pathlib import Path

import pytest

from carrier.flightdeck import sdk
from carrier.flightdeck.manager import FlightDeck
from carrier.readyroom.manager import RoomError

from .fakeclaude import ALL_CLIENTS, FakeClaudeClient
from .test_flightdeck import events, make_repo, wait


def test_matching(tmp_path: Path):
    work, ref, other = tmp_path / "work", tmp_path / "ref", tmp_path / "elsewhere"
    for d in (work, ref, other):
        d.mkdir()
    allow = {**sdk.ALLOW_DEFAULT, "read": True, "edit": True, "web_fetch": True, "sites": ["python.org"],
             "commands": ["git", "pytest"], "tools": ["mcp__gh__get_issue"]}

    def ok(tool: str, inp: dict) -> str:
        return sdk.preapproved(tool, inp, allow, work, [str(work), str(ref)], [str(work)])

    assert ok("Read", {"file_path": str(work / "a.py")}) == "read"
    assert ok("Read", {"file_path": str(ref / "b.md")}) == "read"
    assert ok("Grep", {"pattern": "x"}) == "read"  # no path: the session's folder
    assert ok("Glob", {"pattern": "*.py", "path": "src"}) == "read"  # relative to the session's folder
    assert ok("Read", {"file_path": str(other / "secret")}) == ""
    assert ok("Read", {"file_path": str(work / ".." / "elsewhere" / "x")}) == ""
    assert ok("Edit", {"file_path": str(work / "a.py")}) == "edit"
    assert ok("Write", {"file_path": str(ref / "b.md")}) == ""  # reference folders: reading only
    assert ok("Edit", {"file_path": str(work / ".git" / "config")}) == ""  # could make git run commands
    assert ok("Write", {"file_path": str(work / ".claude" / "settings.json")}) == ""
    assert ok("WebFetch", {"url": "https://docs.python.org/3/"}) == "web_fetch"
    assert ok("WebFetch", {"url": "https://evilpython.org/"}) == ""
    assert ok("WebSearch", {"query": "x"}) == ""
    assert ok("Bash", {"command": "git status"}) == "commands"
    assert ok("Bash", {"command": "git status && rm -rf /"}) == ""  # chained: still asks
    assert ok("Bash", {"command": "pytest -q > out.txt"}) == "commands"  # writes in the work folder; edits allowed
    assert ok("Bash", {"command": "pytest -q > ../elsewhere/out.txt"}) == ""
    assert ok("Bash", {"command": "git log $(whoami)"}) == ""
    assert ok("Bash", {"command": "npm install"}) == ""
    assert ok("mcp__gh__get_issue", {}) == "tools"
    pa = sdk.project_allow("Bash", {"command": "npm test && git status"}, allow, work, [str(work)], [str(work)])
    assert pa["change"] == {"commands": ["npm"]} and pa["label"] == "Always allow `npm` in this project"
    assert [p["listed"] for p in pa["programs"]] == [False, True]
    pa = sdk.project_allow("Bash", {"command": "npm test $(id)"}, allow, work, [str(work)], [str(work)])
    assert pa["change"] is None and "command substitution" in pa["reason"]
    assert sdk.project_allow("mcp__carrier__ask_user", {}) is None


@pytest.fixture
def deck(tmp_path: Path):
    ALL_CLIENTS.clear()
    d = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    yield d
    d.close()


def test_sessions_use_the_projects_preapprovals(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    with pytest.raises(RoomError):
        deck.room.set_allowances(pid, commands="git status; rm -rf x")  # not a whole command
    s = deck.start_session(pid, "job", send_task=False)  # default mode: asks
    deck.send(s["id"], "write notes.md hi")
    wait(deck, s["id"], "needs_you")
    req = events(deck, s["id"], "permission")[0]
    assert req["project_label"] == "Always allow editing files in this project"
    deck.answer(s["id"], req["req_id"], "allow_project")
    wait(deck, s["id"])
    assert deck.room.allowances(pid)["edit"] is True
    import json

    assert "pre-approved actions — edit: on" in json.dumps(deck.store.project_log(pid), ensure_ascii=False)
    # A new session of the project doesn't ask any more.
    s2 = deck.start_session(pid, "job 2", send_task=False)
    deck.send(s2["id"], "write more.md yo")
    wait(deck, s2["id"])
    assert not events(deck, s2["id"], "permission") and (Path(s2["cwd"]) / "more.md").read_text() == "yo\n"
    # Turned off again: it asks again.
    deck.room.set_allowances(pid, edit=False)
    deck.send(s2["id"], "write more.md again")
    wait(deck, s2["id"], "needs_you")


def test_templates_copy_preapprovals(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    deck.room.set_allowances(pid, read=True, commands="git, npm")
    extras = deck.room._template_extras(pid)
    q = make_repo(tmp_path / "Proj2", {"b.txt": "b\n"})
    pid2 = deck.add_project(str(q))["projects"][0]["id"]
    deck.room._apply_template_extras(pid2, extras)
    a = deck.room.allowances(pid2)
    assert a["read"] and a["commands"] == ["git", "npm"] and not a["edit"]


def test_shell_commands(tmp_path: Path):
    from carrier.flightdeck import shellcheck as S

    work = tmp_path / "mod"
    work.mkdir()
    (work / "build.sh").write_text("echo hi\n")

    def check(cmd: str, entries: list[str], **kw) -> S.Verdict:
        return S.check(cmd, entries, work, [str(work)], [str(work)], **kw)

    java = ('JP="/c/Program Files/Java/jdk-17/bin/javap.exe"; CORE=/d/Starsector/starsector-core; '
            '"$JP" -p -c -cp "$CORE/starfarer_obf.jar" \'com.fs.starfarer.util.return\' '
            '| awk \'/private static java.io.File/,/publ/\'')
    v = check(java, ["javap"])
    assert not v.ok and v.missing == ["awk"]
    assert check(java, ["javap", "awk"]).ok  # every program listed: the whole command is fine
    assert check("bash build.sh 2>&1 | grep -v x | tail -8", ["grep", "tail", "bash build.sh"]).ok
    v = check("bash build.sh 2>&1 | tail -8", ["tail"])
    assert v.missing == ["bash build.sh"] and "bash can run any command" in v.reason
    for bad, entries in [("bash -c 'rm -rf /'", ["bash build.sh"]),
                         ("git status && rm -rf x", ["git"]),
                         ("git -c core.pager=evil log", ["git"]),
                         ("echo hi>out.txt", []),  # a write, with edits not pre-approved
                         ("cat ~/.ssh/id_rsa", ["cat"]),  # a reader outside the project
                         ("./gradlew build", ["gradlew"]),  # a script in the project: by its path only
                         ("../x/javap -c", ["javap"]),
                         ("java $(whoami)", ["java"]),
                         ("java `id`", ["java"]),
                         ('"$UNSET" x', ["javap"]),
                         ("cd /tmp && bash build.sh", ["bash build.sh"]),  # not the project's build.sh
                         ("for f in *; do rm $f; done", ["rm"]),
                         ("find . -delete", ["find"]),
                         ("cat <<EOF\nx\nEOF", ["cat"]),
                         ("xargs rm < list", ["rm"])]:
        assert not check(bad, entries).ok, bad
    for good, entries in [("echo hi>out.txt", []), ("./gradlew build", ["./gradlew"]),
                          ("mvn -q test 2>/dev/null", ["mvn"]),
                          ('export JDK=/usr/lib/jvm/jdk-17; "$JDK/bin/java" -version', ["java"]),
                          ("git log --oneline -5", ["git log"]), ("ls -la # ; rm -rf /", ["ls"])]:
        assert check(good, entries, can_edit=True).ok, good
    # Found by an independent review: each of these used to get through.
    for bad, entries in [("X='/bin/sh -c id /javap'; $X", ["javap"]),  # an unquoted variable splitting
                         ("PATH=.:$PATH javap", ["javap"]), ("export PATH=.; javap", ["javap"]),
                         ("GIT_EXTERNAL_DIFF='cmd;:' git diff", ["git diff"]),
                         ("LD_PRELOAD=/tmp/x.so javap", ["javap"]),
                         ("echo evil >& ~/.bashrc", []), ("echo x >&2file", []),
                         ("cd; echo evil >> .bashrc", []), ("cd -; cat .ssh/id_rsa", ["cat"]),
                         ("cd /etc; cat shadow", ["cat"]), ("cd /tmp; ./gradlew", ["./gradlew"]),
                         (": ~/.bashrc; echo evil >> $_", []),
                         ("X=/usr/bin/javap; printf -v X /bin/echo; $X", ["javap"]),
                         ("X=$PWD/javap; $X", ["javap"]), ("cat ''{/etc/passwd,}", ["cat"]),
                         ("cat $'\\x2fetc/passwd'", ["cat"]), ("sort --compress-prog=sh x", ["sort"]),
                         ("sort -o/etc/x a", ["sort"]), ("uniq a b", ["uniq"]),
                         ("git diff --output=/tmp/gd", ["git diff"]), ("git diff --no-index /etc/passwd x", ["git diff"]),
                         ("grep -f/etc/passwd x", ["grep"]), ("sed -n '1e id' x", ["sed"]),
                         ("find . -fprint0 /tmp/x", ["find"]), ("/tmp/evil/javap", ["javap"]),
                         ("set -k; javap X=y", ["javap"]), ("/???/b?n/r?", ["rm"]),
                         # the second review
                         (": --output=/tmp/gd; git diff $_", ["git diff"]),
                         ('X=~root/.ssh/id_rsa; cat "$X"', ["cat"]),
                         ("/c/Windows/../../tmp/javap", ["javap"]),
                         ("grep -eroot /etc/shadow", ["grep"]), ("grep --regexp=root /etc/shadow", ["grep"]),
                         ("echo '[core]' >> .git/config; git status", ["git status"]),
                         ("echo x > .claude/settings.local.json", []),
                         ("xxd data.csv out", ["xxd"]), ("git branch -D main", ["git branch"]),
                         ("export CLASSPATH=/tmp/evil.jar; javac x", ["javac"]),
                         # the third review
                         ("git blame --contents /etc/shadow -- data.csv", ["git blame"]),
                         ("grep --regex=root /etc/shadow", ["grep"]), ("git branch evil", ["git branch"]),
                         ("awk --source='{print}' /etc/shadow", ["awk"]), ("echo x > .vscode/tasks.json", [])]:
        assert not check(bad, entries, can_edit="2file" not in bad).ok, bad  # 2file: a file, not stream 2
    for entry in ["bash", "env", "sh -c", 'JP="/c/Program', "git status; rm", "bash ../x.sh"]:
        with pytest.raises(ValueError):
            S.parse_entry(entry)
    assert S.valid_entries(["bash", "javap", 'JP="/c/Program', "javap"]) == ["javap"]


def test_groups_suggestions_and_old_entries(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Mod", {"src/x/Main.java": "class Main {}\n", "build.gradle": "\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    proj = deck.room.project(pid)
    assert {"java", "gradle", "git_read"} <= set(proj["allow_suggest"])
    deck.store.set_setting(f"project:{pid}:allow", {"commands": ["bash", 'JP="/c/Program', "javap"]})  # 0.8.0
    assert deck.room.allowances(pid)["commands"] == ["javap"]
    with pytest.raises(RoomError):
        deck.room.set_allowances(pid, commands="bash")
    deck.room.set_allowances(pid, groups=["java", "text"])
    allow = deck.room.allowances(pid)
    assert sdk.preapproved("Bash", {"command": "javac -d out src/x/Main.java | sed -n 1p"}, allow, p,
                           [str(p)], [str(p)]) == "commands"
    assert "java" not in deck.room.project(pid)["allow_suggest"]


def test_shared_with_claude_code(deck: FlightDeck, tmp_path: Path):
    import json
    import subprocess

    p = make_repo(tmp_path / "Mod", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    f = p / ".claude" / "settings.local.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)", {"odd": 1}], "deny": ["Read(.env)"]},
                             "model": "x"}))
    deck.room.set_allowances(pid, read=True, commands="javap, git status", web_fetch=True, sites="python.org")
    data = json.loads(f.read_text())
    assert data["model"] == "x" and data["permissions"]["deny"] == ["Read(.env)"]
    # git isn't shared: Claude Code's own rule would allow git options Carrier holds back.
    assert data["permissions"]["allow"] == ["Bash(make:*)", {"odd": 1}, "Read(/**)", "WebFetch(domain:python.org)",
                                            "Bash(javap:*)"]
    deck.room.set_allowances(pid, edit=True)  # editing shared: .git, .claude … still ask in Claude Code too
    data = json.loads(f.read_text())
    assert "Edit(/**)" in data["permissions"]["allow"] and "Edit(**/.git/**)" in data["permissions"]["ask"]
    ignored = subprocess.run(["git", "check-ignore", "-q", ".claude/settings.local.json"], cwd=p)
    assert ignored.returncode == 0  # git won't publish it
    # Removing one removes just Carrier's rule; turning sharing off removes all of Carrier's.
    deck.room.set_allowances(pid, commands="git status")
    assert "Bash(javap:*)" not in json.loads(f.read_text())["permissions"]["allow"]
    deck.room.set_allowances(pid, export=False)
    perms = json.loads(f.read_text())["permissions"]
    assert perms["allow"] == ["Bash(make:*)", {"odd": 1}] and "ask" not in perms
    # A broken file is left alone, and the page says why.
    f.write_text("{ not json")
    deck.room.set_allowances(pid, export=True)
    assert f.read_text() == "{ not json"
    assert "isn't valid JSON" in deck.room.project(pid)["allow_export_error"]


def test_shell_cards_and_session_programs(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Mod", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    deck.room.set_allowances(pid, commands="javap")
    s = deck.start_session(pid, "job", send_task=False)
    cmd = 'JP="/c/Program Files/Java/jdk-17/bin/javap.exe"; "$JP" -c Foo | awk \'/x/\''
    deck.send(s["id"], "shell " + cmd)
    wait(deck, s["id"], "needs_you")
    req = events(deck, s["id"], "permission")[0]
    assert [(x["name"], x["listed"]) for x in req["programs"]] == [("javap", True), ("awk", False)]
    assert req["project_label"] == "Always allow `awk` in this project"
    assert req["allow_label"] == "Allow `awk` for this session"
    deck.answer(s["id"], req["req_id"], "allow_always")
    wait(deck, s["id"])
    deck.send(s["id"], "shell tail -3 x | awk '/y/'")  # tail isn't allowed: asks
    wait(deck, s["id"], "needs_you")
    deck.answer(s["id"], events(deck, s["id"], "permission")[-1]["req_id"], "deny")
    wait(deck, s["id"])
    deck.send(s["id"], "shell " + cmd.replace("Foo", "Bar"))  # awk for this session, javap for the project
    wait(deck, s["id"])
    assert len(events(deck, s["id"], "permission")) == 2
    assert ALL_CLIENTS[0].ran[-1].endswith("-c Bar | awk '/x/'")
    assert deck.room.allowances(pid)["commands"] == ["javap"]  # the project list didn't change
    # Something adding programs can't fix: no project button, and the card says why.
    deck.send(s["id"], "shell javap $(cat list)")
    wait(deck, s["id"], "needs_you")
    req = events(deck, s["id"], "permission")[-1]
    assert req["project_label"] == "" and "command substitution" in req["why"]
    assert req["allow_label"] == "Allow this exact command for this session"


def test_git_ignores_bare_repositories_claude_could_make(tmp_path: Path):
    """A folder with HEAD, config, objects and refs looks like a repository to git, and its
    config could make "git status" run a command. Claude Code's commands get
    safe.bareRepository=explicit, so git leaves such a folder alone."""
    import os
    import subprocess

    env = sdk.safe_git_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x.y", "GIT_CONFIG_VALUE_0": "z"})
    assert env == {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "safe.bareRepository",
                   "GIT_CONFIG_VALUE_1": "explicit"}  # the user's own settings are kept
    evil = tmp_path / "evil"
    for d in ("objects", "refs"):
        (evil / d).mkdir(parents=True)
    (evil / "HEAD").write_text("ref: refs/heads/main\n")
    marker = tmp_path / "ran"
    (evil / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tworktree = ..\n"
                                 f"\tfsmonitor = touch {marker.as_posix()}\n")
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "status"], cwd=evil, env=base, capture_output=True)
    if not marker.exists():
        pytest.skip("this git doesn't run fsmonitor here, so there's nothing to show")
    marker.unlink()
    subprocess.run(["git", "status"], cwd=evil, env={**base, **sdk.safe_git_env(base)}, capture_output=True)
    assert not marker.exists()
