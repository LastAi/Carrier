# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Snapshot, diff parsing and checkpoint behaviour."""

from pathlib import Path

from carrier.core import gitutil

from .conftest import run

SAMPLE = """\
diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x = 1
-y = 2
+y = 3
+z = 4
@@ -10 +11 @@ def f():
-    pass
+    return 1
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/img.png b/img.png
new file mode 100644
Binary files /dev/null and b/img.png differ
diff --git a/before.py b/after.py
similarity index 90%
rename from before.py
rename to after.py
"""


def test_parse_diff_statuses_and_ids():
    files = gitutil.parse_diff(SAMPLE)
    by = {f.path: f for f in files}
    assert [h.id for f in files for h in f.hunks] == ["h1", "h2", "h3", "h4"]
    assert by["a.py"].status == "modified" and len(by["a.py"].hunks) == 2
    assert by["a.py"].hunks[0].added == 2 and by["a.py"].hunks[0].removed == 1
    assert by["a.py"].hunks[1].old_len == 1
    assert by["new.txt"].status == "added"
    assert by["old.txt"].status == "deleted"
    assert by["img.png"].binary and not by["img.png"].hunks
    assert by["after.py"].status == "renamed" and by["after.py"].old_path == "before.py"


def test_snapshot_includes_untracked_and_leaves_worktree_alone(repo: Path):
    status_before = run(repo, "status", "--porcelain")
    head_before = run(repo, "rev-parse", "HEAD")
    snap = gitutil.snapshot_worktree(repo)
    assert run(repo, "status", "--porcelain") == status_before
    assert run(repo, "rev-parse", "HEAD") == head_before
    files = run(repo, "ls-tree", "-r", "--name-only", snap).split()
    assert "notes.txt" in files  # untracked file captured
    assert "tests/test_save.py" not in files  # deletion captured


def test_debrief_folder_is_never_reviewed(repo: Path):
    (repo / ".carrier").mkdir()
    (repo / ".carrier" / "junk.json").write_text("{}")
    snap = gitutil.snapshot_worktree(repo)
    assert ".carrier/junk.json" not in run(repo, "ls-tree", "-r", "--name-only", snap)


def test_checkpoint_roundtrip(repo: Path):
    assert gitutil.get_checkpoint(repo) is None
    sha = gitutil.resolve(repo, "HEAD")
    gitutil.set_checkpoint(repo, sha)
    assert gitutil.get_checkpoint(repo) == sha


def test_tricky_paths_and_line_content(repo: Path):
    """Paths containing ' b/', quoted paths, and form feeds inside lines."""
    (repo / "art b").mkdir()
    (repo / "art b" / "x.png").write_bytes(b"\x89PNG\x00\x01")
    (repo / "tab\tname.txt").write_text("hi\n")
    (repo / "util.py").write_bytes(b"X = 1\x0c  # page break kept\n")
    snap = gitutil.snapshot_worktree(repo)
    files = {f.path: f for f in gitutil.parse_diff(gitutil.diff_text(repo, "HEAD", snap))}
    assert "art b/x.png" in files and files["art b/x.png"].binary
    assert "tab\tname.txt" in files
    added = [ln for h in files["util.py"].hunks for ln in h.lines if ln.startswith("+")]
    assert added == ["+X = 1\x0c  # page break kept"]
