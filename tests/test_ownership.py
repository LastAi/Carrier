# SPDX-License-Identifier: AGPL-3.0-only
"""Folder ownership (readyroom/ownership.py): main folders are their project's alone,
shared spaces are owned part by part."""

from __future__ import annotations

from pathlib import Path

import pytest

from carrier.core.db import Store
from carrier.readyroom import ownership
from carrier.readyroom.manager import ReadyRoom, RoomError


def _room(tmp_path: Path) -> ReadyRoom:
    return ReadyRoom(Store(tmp_path / "c.db"))


def _dirs(tmp_path: Path, *names: str) -> dict[str, Path]:
    out = {}
    for n in names:
        d = tmp_path / n
        d.mkdir(parents=True, exist_ok=True)
        out[n] = d
    return out


def test_patterns():
    m = ownership.matches
    assert m("ships/a/b.json", ["ships/**"]) and m("ships/x", ["ships/"])
    assert not m("other/ships/x", ["ships/**"])           # anchored: has a slash
    assert m("deep/notes.md", ["*.md"]) and m("notes.md", ["*.md"])  # no slash: any folder
    assert m("data/ship_1.csv", ["data/ship_*.csv"]) and not m("data/sub/ship_1.csv", ["data/ship_*.csv"])
    assert m("a/b/c.txt", ["a/**/c.txt"]) and m("a/c.txt", ["a/**/c.txt"])
    assert m("ships/a.txt", ["ships"]) and m("ships", ["ships"]) and not m("shipsy", ["ships"])  # a plain name
    assert ownership.clean_patterns("ships/**, *.md\nships/**") == ["ships/**", "*.md"]
    for bad in ("../x", "/etc/**", "C:/x", "~/x", "a/../../b"):
        with pytest.raises(ValueError):
            ownership.clean_patterns(bad)


def test_main_folders_belong_to_their_project(tmp_path: Path):
    d = _dirs(tmp_path, "a", "b", "a/sub")
    room = _room(tmp_path)
    a = room.create_project("A", str(d["a"]))["id"]
    b = room.create_project("B", str(d["b"]))["id"]
    for path in (d["a"], d["a/sub"]):
        with pytest.raises(RoomError, match="A's main folder"):
            room.add_folder(b, str(path))
    p = room.add_folder(b, str(d["a/sub"]), kind="reference")
    with pytest.raises(RoomError, match="A's main folder"):
        room.set_folder_kind(b, p["folders"][0]["path"], "work")
    assert room.project(a)["main_shared_with"] == ["B"]


def test_normalize_fixes_older_setups(tmp_path: Path):
    """Projects set up before ownership: a work folder in another project's main folder
    becomes a reference folder; a work folder two projects use becomes a shared space."""
    d = _dirs(tmp_path, "a", "b", "c", "shared")
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    a = room.create_project("A", str(d["a"]))["id"]
    b = room.create_project("B", str(d["b"]))["id"]
    c = room.create_project("C", str(d["c"]))["id"]
    # Written straight to the store, as an older version could have.
    store.update_project(b, folders=[{"path": str(d["a"].resolve()), "label": "a", "kind": "work"},
                                     {"path": str(d["shared"].resolve()), "label": "shared", "kind": "work"}])
    store.update_project(c, folders=[{"path": str(d["shared"].resolve()), "label": "shared", "kind": "work",
                                      "review": "own"}])
    changes = ownership.normalize(store)
    assert any("reference folder" in x and "A's main folder" in x for x in changes)
    fb = {f["label"]: f for f in store.project(b)["folders"]}
    assert fb["a"]["kind"] == "reference" and fb["shared"]["share"] == "shared"
    fc = store.project(c)["folders"][0]
    assert fc["share"] == "shared" and fc["review"] == "all"  # "only this project's changes" → shared space
    assert ownership.normalize(store) == []  # nothing left to fix
    assert a


def test_classify(tmp_path: Path):
    d = _dirs(tmp_path, "a", "b", "shared", "shared/ships", "mine", "mine/inner")
    (d["shared"] / "readme.txt").write_text("x")
    (d["shared/ships"] / "one.json").write_text("x")
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    a = room.create_project("A", str(d["a"]))["id"]
    b = room.create_project("B", str(d["b"]))["id"]
    room.create_project("Inner", str(d["mine/inner"]))
    for pid in (a, b):
        room.add_folder(pid, str(d["shared"]))
    room.add_folder(a, str(d["mine"]))
    shared = str(d["shared"].resolve())
    room.set_folder_options(a, shared, part="ships/**")
    pa = store.project(a)

    def kind(path: Path) -> str:
        return ownership.classify(store, pa, str(path))["kind"]

    assert kind(d["a"] / "x.py") == "own"
    assert kind(d["mine"] / "x.py") == "own"                  # a work folder of its own
    assert kind(d["mine/inner"] / "x.py") == "foreign"        # another project's main folder inside it
    assert kind(d["shared/ships"] / "one.json") == "part"     # its part of the shared space
    assert kind(d["shared"] / "readme.txt") == "other"        # an existing file outside its part
    assert kind(d["shared"] / "new.txt") == "new"             # a new file: becomes its own
    ownership.claim(store, a, shared, "new.txt")
    assert kind(d["shared"] / "new.txt") == "part"
    assert kind(d["b"] / "x.py") == "outside"
    assert room.project(a)["folders"][0]["claimed"] == 1


def test_shell_writes_respect_ownership(tmp_path: Path):
    from carrier.flightdeck import shellcheck as S

    work = tmp_path / "shared"
    work.mkdir()

    def no_write(p: str) -> bool:
        return Path(p).name == "theirs.txt"

    def check(cmd: str):
        return S.check(cmd, ["echo"], work, [str(work)], [str(work)], can_edit=True, no_write=no_write)

    assert check("echo hi > mine.txt").ok
    assert not check("echo hi > theirs.txt").ok

    def check2(cmd: str, entries: list[str]):
        return S.check(cmd, entries, work, [str(work)], [str(work)], can_edit=True, no_write=no_write)

    writers = ["cp", "mv", "tee", "rm", "touch", "git", "echo", "dd", "mkdir", "sed"]
    for ok in ("cp a.txt mine.txt", "echo x | tee mine.txt", "rm mine.txt", "mkdir new", "touch new.txt",
               "git status", "git checkout -- mine.txt", "dd if=a.txt of=mine.txt", "cp theirs.txt mine.txt"):
        assert check2(ok, writers).ok, ok
    for bad in ("cp a.txt theirs.txt", "mv theirs.txt mine.txt", "echo x | tee theirs.txt", "rm theirs.txt",
                "rm -f -- theirs.txt", "touch theirs.txt", "git checkout -- theirs.txt", "dd if=a of=theirs.txt",
                "cp -t /elsewhere a.txt", "rm ../outside.txt", "rm .git/config", "rm $X", "rm *.txt"):
        assert not check2(bad, writers).ok, bad
    # Without edits pre-approved, any change asks.
    assert not S.check("touch new.txt", ["touch"], work, [str(work)], [str(work)], can_edit=False).ok


def test_new_main_folder_takes_over_work_folders(tmp_path: Path):
    """Creating a project whose main folder holds another project's work folder: that work
    folder becomes a reference folder right away (not at the next start)."""
    d = _dirs(tmp_path, "b", "ws", "ws/assets")
    room = _room(tmp_path)
    b = room.create_project("B", str(d["b"]))["id"]
    room.add_folder(b, str(d["ws/assets"]))
    room.create_project("A", str(d["ws"]))
    assert room.project(b)["folders"][0]["kind"] == "reference"


def test_claims_keep_the_files_case(tmp_path: Path):
    d = _dirs(tmp_path, "a", "b", "shared")
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    a = room.create_project("A", str(d["a"]))["id"]
    b = room.create_project("B", str(d["b"]))["id"]
    for pid in (a, b):
        room.add_folder(pid, str(d["shared"]))
    info = ownership.classify(store, store.project(a), str(d["shared"] / "Ships" / "NewShip.csv"))
    assert info["kind"] == "new" and info["rel"] == "Ships/NewShip.csv"
