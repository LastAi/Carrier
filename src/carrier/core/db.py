# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Carrier's shared store: one small SQLite database (``carrier.db``).

Ready Room writes it (projects, rules, tasks, presets) and Flight Deck
reads and extends it (sessions, edits, notices, transcripts).

Why SQLite and not a manifest file: several Claude Code sessions record
edits at the same moment. A JSON manifest would need hand-made locking and
could be corrupted by a crash mid-write; SQLite (built into Python) gives
atomic transactions and safe concurrent access for free. The file lives in
the per-user Carrier folder because projects and links span repositories.
Databases from older versions (``flightdeck.db``) are upgraded in place.

Tables
------
projects  one row per project: folder, long-term goal, rules, tags
tasks     work items: a project's queue (planned → current → done) or a
          *standalone* task (no project, its own folder)
rule_sets reusable named bundles of rules; attached globally or per project
presets   session presets: model, permission mode, overlap policy, rule set
templates starting points for new projects
settings  key/value (global rules, global rule sets)
links     a declared relationship between two projects, with optional
          *contract* globs — the files through which they affect each other
          (e.g. the API schema a server defines and a client uses)
sessions  one row per Flight Deck session (a Claude Code conversation)
edits     every file a session changed (recorded automatically by hooks)
events    coordination notices addressed to a session ("the server session
          changed schema/user.json"), delivered on its next prompt
messages  the transcript shown in the UI, so history survives a restart

All access goes through :class:`Store`. Each thread gets its own connection
(SQLite connections must not be shared across threads); WAL mode lets
readers and a writer work at the same time.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL UNIQUE,
    created     REAL NOT NULL
    -- more columns are added by _migrate() (goal, rules, tags, …)
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = standalone
    title       TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'planned',  -- planned | active | done | dropped
    position    REAL NOT NULL DEFAULT 0,          -- order in the project's queue
    cwd         TEXT,                             -- standalone tasks: their folder
    summary     TEXT NOT NULL DEFAULT '',         -- written when done
    origin      TEXT NOT NULL DEFAULT '',         -- 'standalone' when assigned from one
    origin_path TEXT,                             -- where a standalone task's files are
    created     REAL NOT NULL,
    finished    REAL
);
CREATE TABLE IF NOT EXISTS task_edits (        -- every change to a task's scope or place (audit trail)
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL,                 -- kept after the task is deleted
    project_id  INTEGER,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,                    -- created | edited | scope_added | merged_in | merged_away |
                                                  -- split_out | moved | status | deleted
    by          TEXT NOT NULL DEFAULT 'you',      -- you | claude (approved by you) | routine | debrief
    session_id  INTEGER,
    note        TEXT NOT NULL DEFAULT '',         -- why (shown in reviews)
    before      TEXT NOT NULL DEFAULT '',         -- JSON {title, details, ...} before
    after       TEXT NOT NULL DEFAULT '',         -- JSON after
    removed     TEXT NOT NULL DEFAULT ''          -- lines that left the task's details (possible descoping)
);
CREATE INDEX IF NOT EXISTS task_edits_task ON task_edits(task_id, ts);
CREATE TABLE IF NOT EXISTS project_events (    -- the project's activity log, besides task changes
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL,                 -- kept after the project is deleted
    ts          REAL NOT NULL,
    subject     TEXT NOT NULL,                    -- project | folder | group | routine | rule | skill | review | images
    action      TEXT NOT NULL,                    -- e.g. "changed goal", "added work folder lib"
    by          TEXT NOT NULL DEFAULT 'you',
    session_id  INTEGER,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS project_events_project ON project_events(project_id, ts);
CREATE TABLE IF NOT EXISTS task_groups (       -- a project's extra task queues (readyroom/manager.py)
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'normal',  -- high | normal | low | backlog
    tags        TEXT NOT NULL DEFAULT '[]',
    position    REAL NOT NULL DEFAULT 0,
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routines (          -- repeatable tasks (readyroom/routines.py)
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    trigger      TEXT NOT NULL DEFAULT 'manual',   -- manual | after_task
    gate         TEXT NOT NULL DEFAULT 'none',     -- none | reviewed
    enabled      INTEGER NOT NULL DEFAULT 1,
    created      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_sets (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_rule_sets (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rule_set_id INTEGER NOT NULL REFERENCES rule_sets(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, rule_set_id)
);
CREATE TABLE IF NOT EXISTS presets (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    model       TEXT,
    effort      TEXT,                              -- NULL = the model's default
    mode        TEXT NOT NULL DEFAULT 'default',
    policy      TEXT,                              -- NULL = the global overlap setting
    rule_set_id INTEGER REFERENCES rule_sets(id) ON DELETE SET NULL,
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    goal        TEXT NOT NULL DEFAULT '',
    rules       TEXT NOT NULL DEFAULT '',
    rule_set_ids TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',
    first_task  TEXT NOT NULL DEFAULT '',
    preset_id   INTEGER REFERENCES presets(id) ON DELETE SET NULL,
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_agents (     -- Debrief criteria managed in the Ready Room
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = every project
    name           TEXT NOT NULL,                 -- slug; same name as a built-in replaces it
    title          TEXT NOT NULL,
    prompt         TEXT NOT NULL,                 -- what to look for (markdown)
    severity       TEXT NOT NULL DEFAULT 'medium',
    model          TEXT,                          -- NULL = Debrief's default criteria model
    effort         TEXT,                          -- NULL = the model's default effort
    sees_rationale INTEGER NOT NULL DEFAULT 0,
    uses_notes     INTEGER NOT NULL DEFAULT 1,
    explore        INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY,
    project_a   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_b   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    note        TEXT NOT NULL DEFAULT '',
    contracts_a TEXT NOT NULL DEFAULT '[]',   -- JSON list of globs in project_a
    contracts_b TEXT NOT NULL DEFAULT '[]',   -- JSON list of globs in project_b
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = standalone task
    task          TEXT NOT NULL,
    cwd           TEXT NOT NULL,              -- project folder, or its worktree
    worktree      INTEGER NOT NULL DEFAULT 0, -- 1 if cwd is an isolated worktree
    branch        TEXT,                       -- worktree branch, if any
    base_ref      TEXT,                       -- snapshot at start (for Debrief)
    model         TEXT,
    mode          TEXT NOT NULL DEFAULT 'default',
    status        TEXT NOT NULL,              -- see manager.STATUSES
    sdk_session   TEXT,                       -- Claude Code session id (resume)
    cost_usd      REAL NOT NULL DEFAULT 0,
    started       REAL NOT NULL,
    last_activity REAL NOT NULL,
    last_briefed  REAL NOT NULL DEFAULT 0,
    ended         REAL,
    task_id       INTEGER,                    -- the task this session works on
    policy        TEXT,                       -- overlap policy from the preset (NULL = global)
    preset_id     INTEGER,                    -- preset chosen at start (its rule set goes in the context)
    effort        TEXT                        -- reasoning effort (NULL = the model's default)
);
CREATE TABLE IF NOT EXISTS edits (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL,
    path        TEXT NOT NULL,                -- relative to the project root, POSIX
    tool        TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS edits_by_path ON edits(project_id, path);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    target      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source      INTEGER,                      -- session that caused it
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    path        TEXT,                         -- file the notice is about (exact match for de-dup)
    ts          REAL NOT NULL,
    delivered   REAL                          -- when it went into a briefing
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts          REAL NOT NULL,
    body        TEXT NOT NULL                 -- JSON event (see sdk.py)
);
"""

# Sessions in these states hold the deck: they count for coordination.
ACTIVE = ("starting", "working", "needs_you", "idle", "paused")

# Columns added after the first release, created on open if missing.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "projects": {
        "goal": "TEXT NOT NULL DEFAULT ''",
        "rules": "TEXT NOT NULL DEFAULT ''",
        "tags": "TEXT NOT NULL DEFAULT '[]'",
        "preset_id": "INTEGER",
        # Extra folders, JSON [{"path": ..., "label": ...}]. Claude Code runs in
        # the main folder (projects.path) and may also work in these; their
        # files are named "<label>/<path>" in edits, contracts and reviews.
        "folders": "TEXT NOT NULL DEFAULT '[]'",
    },
    "sessions": {"task_id": "INTEGER", "policy": "TEXT", "preset_id": "INTEGER", "effort": "TEXT"},
    "events": {"path": "TEXT"},
    "review_agents": {"model": "TEXT", "effort": "TEXT"},
    "tasks": {"routine_id": "INTEGER", "fix_review": "TEXT", "group_id": "INTEGER"},
    "routines": {"group_id": "INTEGER", "mode": "TEXT NOT NULL DEFAULT ''"},
    "templates": {"extras": "TEXT NOT NULL DEFAULT '{}'"},
    "presets": {"effort": "TEXT"},
}


def default_path() -> Path:
    """``<user config>/carrier.db``; renames an older ``flightdeck.db``."""
    from carrier.core.usersettings import config_dir

    new = config_dir() / "carrier.db"
    old = config_dir() / "flightdeck.db"
    if not new.exists() and old.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(old) + suffix)
            if src.exists():
                src.rename(str(new) + suffix)
    return new


FOLDER_KINDS = ("work", "reference", "workspace")


def folder_kind(folder: dict[str, Any]) -> str:
    """An extra folder's kind: "work" (read and edit; reviewed), "reference" (read only), or
    "workspace" (a shared parent folder: its contents are read only, but a *new* folder the
    project creates there is added to the project as a work folder)."""
    kind = folder.get("kind")
    return kind if kind in ("reference", "workspace") else "work"


MAX_BACKUPS = 5


def backup_database(path: Path) -> Path:
    """Copy the database (consistently, even mid-write) to ``backups/`` beside it before an
    upgrade; keeps the newest MAX_BACKUPS. Returns the backup's path."""
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    target = folder / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}"
    with sqlite3.connect(path) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    for old in sorted(folder.glob(f"{path.stem}-*{path.suffix}"))[:-MAX_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()  # one writer at a time, process-wide
        self.backup: Path | None = None  # set when an upgrade made a backup first
        if path.exists() and self._needs_upgrade(path):
            self.backup = backup_database(path)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _needs_upgrade(path: Path) -> bool:
        """True if this (existing) database lacks a table or column the current version uses."""
        tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as c:
                have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not have:
                    return False  # empty file: nothing to protect
                if tables - have:
                    return True
                for table, cols in _MIGRATIONS.items():
                    present = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                    if set(cols) - present:
                        return True
        except sqlite3.Error:
            return False  # can't tell; opening it normally will report the real problem
        return False

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Bring an older database up to date (idempotent)."""
        # 0.4 made sessions.project_id NOT NULL; standalone tasks need NULL.
        # SQLite can't drop a constraint in place, so rebuild that one table.
        info = {r["name"]: r for r in c.execute("PRAGMA table_info(sessions)")}
        if info.get("project_id") is not None and info["project_id"]["notnull"]:
            Store._rebuild(c, "sessions")
        # An early 0.5 build rebuilt sessions with a rename, which made SQLite
        # repoint other tables' foreign keys at "sessions_old". Repair those.
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%sessions_old%'"
                                 ).fetchall():
            Store._rebuild(c, name)
        for table, cols in _MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    @staticmethod
    def _rebuild(c: sqlite3.Connection, table: str) -> None:
        """Recreate ``table`` from SCHEMA, keeping its rows.

        Copy into a new table, drop the old one, then rename the new one into
        place with ``legacy_alter_table`` on, so SQLite does not rewrite other
        tables' foreign keys during the rename.
        """
        start = SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        create = SCHEMA[start: SCHEMA.index(");", start) + 2]
        create = create.replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {table}_new (", 1)
        have = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("PRAGMA legacy_alter_table=ON")
        try:
            c.execute("BEGIN")
            c.execute(f"DROP TABLE IF EXISTS {table}_new")
            c.execute(create)
            new_cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table}_new)")}
            cols = ", ".join(x for x in have if x in new_cols)
            c.execute(f"INSERT INTO {table}_new({cols}) SELECT {cols} FROM {table}")
            c.execute(f"DROP TABLE {table}")
            c.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        finally:
            c.execute("PRAGMA legacy_alter_table=OFF")
            c.execute("PRAGMA foreign_keys=ON")

    # -- plumbing ------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._write_lock:
            cur = self._conn().execute(sql, args)
            return cur.lastrowid or cur.rowcount

    # -- projects ------------------------------------------------------------

    def add_project(self, name: str, path: str) -> int:
        existing = self._q("SELECT id FROM projects WHERE path=?", (path,))
        if existing:
            return existing[0]["id"]
        return self._x("INSERT INTO projects(name, path, created) VALUES (?,?,?)", (name, path, time.time()))

    @staticmethod
    def _project_row(r: dict[str, Any]) -> dict[str, Any]:
        r["tags"] = json.loads(r.get("tags") or "[]")
        r["folders"] = json.loads(r.get("folders") or "[]")
        return r

    @staticmethod
    def project_roots(project: dict[str, Any], include_reference: bool = False) -> list[tuple[str, str]]:
        """``[(label, path), ...]``: the main folder (label ``""``) first, then extras.

        Only *work* folders (where the project's work happens) unless
        ``include_reference``: reference folders are read-only context, never
        edited by the project's sessions nor reviewed with it.
        """
        return [("", project["path"])] + [(f["label"], f["path"]) for f in project.get("folders") or []
                                          if include_reference or folder_kind(f) == "work"]

    def projects(self) -> list[dict[str, Any]]:
        return [self._project_row(r) for r in self._q("SELECT * FROM projects ORDER BY name COLLATE NOCASE")]

    def project(self, pid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM projects WHERE id=?", (pid,))
        return self._project_row(rows[0]) if rows else None

    def project_by_path(self, path: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM projects WHERE path=?", (path,))
        return self._project_row(rows[0]) if rows else None

    def rename_project(self, pid: int, name: str) -> None:
        self._x("UPDATE projects SET name=? WHERE id=?", (name, pid))

    PROJECT_FIELDS = ("name", "path", "goal", "rules", "tags", "preset_id", "folders")

    def update_project(self, pid: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in self.PROJECT_FIELDS}
        for key in ("tags", "folders"):
            if key in fields:
                fields[key] = json.dumps(fields[key])
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._x(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), pid))

    # -- tasks -----------------------------------------------------------------

    TASK_FIELDS = ("project_id", "title", "details", "status", "position", "cwd", "summary", "origin",
                   "origin_path", "finished", "routine_id", "fix_review", "group_id")

    def add_task(self, title: str, project_id: int | None = None, **fields: Any) -> int:
        if "position" not in fields:
            rows = self._q("SELECT COALESCE(MAX(position), 0) AS m FROM tasks WHERE project_id IS ?", (project_id,))
            fields["position"] = rows[0]["m"] + 1
        fields = {k: v for k, v in fields.items() if k in self.TASK_FIELDS}
        fields.update(title=title, project_id=project_id, created=time.time())
        cols = ", ".join(fields)
        return self._x(f"INSERT INTO tasks({cols}) VALUES ({', '.join('?' for _ in fields)})",
                       tuple(fields.values()))

    def update_task(self, tid: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in self.TASK_FIELDS}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._x(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))

    def task(self, tid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM tasks WHERE id=?", (tid,))
        return rows[0] if rows else None

    def tasks(self, project_id: int | None, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """A project's tasks (or standalone tasks with ``project_id=None``), queue order."""
        sql = "SELECT * FROM tasks WHERE project_id IS ?"
        args: list[Any] = [project_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            args += list(statuses)
        order = " ORDER BY finished DESC, id DESC" if statuses == ("done",) else " ORDER BY position, id"
        return self._q(sql + order, tuple(args))

    def delete_task(self, tid: int) -> None:
        self._x("DELETE FROM tasks WHERE id=?", (tid,))

    # -- rule sets, presets, templates, settings ---------------------------------

    def add_rule_set(self, name: str, body: str) -> int:
        return self._x("INSERT INTO rule_sets(name, body, created) VALUES (?,?,?)", (name, body, time.time()))

    def update_rule_set(self, rid: int, name: str, body: str) -> None:
        self._x("UPDATE rule_sets SET name=?, body=? WHERE id=?", (name, body, rid))

    def delete_rule_set(self, rid: int) -> None:
        """Delete a rule set and every reference to it (templates and the
        global list keep ids in JSON, which foreign keys can't cover)."""
        self._x("DELETE FROM rule_sets WHERE id=?", (rid,))
        for t in self._q("SELECT id, rule_set_ids FROM templates"):
            ids = json.loads(t["rule_set_ids"] or "[]")
            if rid in ids:
                self._x("UPDATE templates SET rule_set_ids=? WHERE id=?",
                        (json.dumps([i for i in ids if i != rid]), t["id"]))
        glob = self.get_setting("global_rule_sets", [])
        if rid in glob:
            self.set_setting("global_rule_sets", [i for i in glob if i != rid])

    def rule_sets(self) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM rule_sets ORDER BY name COLLATE NOCASE")

    def rule_set(self, rid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM rule_sets WHERE id=?", (rid,))
        return rows[0] if rows else None

    def project_rule_set_ids(self, pid: int) -> list[int]:
        return [r["rule_set_id"] for r in self._q("SELECT rule_set_id FROM project_rule_sets WHERE project_id=?",
                                                    (pid,))]

    def set_project_rule_sets(self, pid: int, ids: list[int]) -> None:
        known = {r["id"] for r in self._q("SELECT id FROM rule_sets")}
        ids = [i for i in dict.fromkeys(ids) if i in known]  # skip deleted ids, keep order
        with self._write_lock:
            c = self._conn()
            c.execute("BEGIN")
            try:
                c.execute("DELETE FROM project_rule_sets WHERE project_id=?", (pid,))
                c.executemany("INSERT OR IGNORE INTO project_rule_sets VALUES (?,?)", [(pid, i) for i in ids])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    PRESET_FIELDS = ("name", "model", "effort", "mode", "policy", "rule_set_id")

    def add_preset(self, **fields: Any) -> int:
        fields = {k: v for k, v in fields.items() if k in self.PRESET_FIELDS}
        fields["created"] = time.time()
        return self._x(f"INSERT INTO presets({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                       tuple(fields.values()))

    def update_preset(self, pid: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in self.PRESET_FIELDS}
        if fields:
            self._x(f"UPDATE presets SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), pid))

    def delete_preset(self, pid: int) -> None:
        # projects.preset_id has no foreign key (added by migration), and ids
        # can be reused, so clear references or a new preset would inherit them.
        self._x("UPDATE projects SET preset_id=NULL WHERE preset_id=?", (pid,))
        self._x("UPDATE sessions SET preset_id=NULL WHERE preset_id=?", (pid,))
        self._x("DELETE FROM presets WHERE id=?", (pid,))

    def presets(self) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM presets ORDER BY name COLLATE NOCASE")

    def preset(self, pid: int | None) -> dict[str, Any] | None:
        if pid is None:
            return None
        rows = self._q("SELECT * FROM presets WHERE id=?", (pid,))
        return rows[0] if rows else None

    TEMPLATE_FIELDS = ("name", "goal", "rules", "rule_set_ids", "tags", "first_task", "preset_id", "extras")

    @staticmethod
    def _template_row(r: dict[str, Any]) -> dict[str, Any]:
        r["rule_set_ids"] = json.loads(r["rule_set_ids"] or "[]")
        r["tags"] = json.loads(r["tags"] or "[]")
        r["extras"] = json.loads(r.get("extras") or "{}")
        return r

    def _template_values(self, fields: dict[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in fields.items() if k in self.TEMPLATE_FIELDS}
        for k in ("rule_set_ids", "tags", "extras"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        return fields

    def add_template(self, **fields: Any) -> int:
        fields = self._template_values(fields)
        fields["created"] = time.time()
        return self._x(f"INSERT INTO templates({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                       tuple(fields.values()))

    def update_template(self, tid: int, **fields: Any) -> None:
        fields = self._template_values(fields)
        if fields:
            self._x(f"UPDATE templates SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                    (*fields.values(), tid))

    def delete_template(self, tid: int) -> None:
        self._x("DELETE FROM templates WHERE id=?", (tid,))

    def templates(self) -> list[dict[str, Any]]:
        return [self._template_row(r) for r in self._q("SELECT * FROM templates ORDER BY name COLLATE NOCASE")]

    def template(self, tid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM templates WHERE id=?", (tid,))
        return self._template_row(rows[0]) if rows else None

    # -- review agents (Debrief criteria) ----------------------------------------

    AGENT_FIELDS = ("project_id", "name", "title", "prompt", "severity", "model", "effort", "sees_rationale",
                    "uses_notes", "explore", "enabled")

    @staticmethod
    def _agent_row(r: dict[str, Any]) -> dict[str, Any]:
        for k in ("sees_rationale", "uses_notes", "explore", "enabled"):
            r[k] = bool(r[k])
        return r

    def review_agents(self, project_id: int | None, include_global: bool = False) -> list[dict[str, Any]]:
        """Agents of one project (or the global ones with ``project_id=None``)."""
        if project_id is None:
            rows = self._q("SELECT * FROM review_agents WHERE project_id IS NULL ORDER BY title COLLATE NOCASE")
        elif include_global:
            rows = self._q("SELECT * FROM review_agents WHERE project_id IS NULL OR project_id=? "
                           "ORDER BY project_id IS NOT NULL, title COLLATE NOCASE", (project_id,))
        else:
            rows = self._q("SELECT * FROM review_agents WHERE project_id=? ORDER BY title COLLATE NOCASE",
                           (project_id,))
        return [self._agent_row(r) for r in rows]

    def review_agent(self, aid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM review_agents WHERE id=?", (aid,))
        return self._agent_row(rows[0]) if rows else None

    def add_review_agent(self, **fields: Any) -> int:
        fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in self.AGENT_FIELDS}
        fields["created"] = time.time()
        return self._x(f"INSERT INTO review_agents({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                       tuple(fields.values()))

    def update_review_agent(self, aid: int, **fields: Any) -> None:
        fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items()
                  if k in self.AGENT_FIELDS and k != "project_id"}
        if fields:
            self._x(f"UPDATE review_agents SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                    (*fields.values(), aid))

    # -- routines (repeatable tasks) --------------------------------------------

    ROUTINE_FIELDS = ("name", "instructions", "trigger", "gate", "enabled", "group_id", "mode")

    def routines(self, project_id: int) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM routines WHERE project_id=? ORDER BY name COLLATE NOCASE", (project_id,))
        for r in rows:
            r["enabled"] = bool(r["enabled"])
        return rows

    def routine(self, rid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM routines WHERE id=?", (rid,))
        if rows:
            rows[0]["enabled"] = bool(rows[0]["enabled"])
        return rows[0] if rows else None

    def save_routine(self, project_id: int, rid: int | None = None, **fields: Any) -> int:
        fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in self.ROUTINE_FIELDS}
        if rid is None:
            fields.update(project_id=project_id, created=time.time())
            return self._x(f"INSERT INTO routines({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                           tuple(fields.values()))
        if fields:
            self._x(f"UPDATE routines SET {', '.join(f'{k}=?' for k in fields)} WHERE id=? AND project_id=?",
                    (*fields.values(), rid, project_id))
        return rid

    # -- task edit log ------------------------------------------------------------

    def log_task_edit(self, task_id: int, kind: str, before: dict[str, Any] | None = None,
                      after: dict[str, Any] | None = None, by: str = "you", note: str = "",
                      session_id: int | None = None, project_id: int | None = None) -> None:
        before, after = before or {}, after or {}
        old = [ln for ln in str(before.get("details", "")).splitlines() if ln.strip()]
        new = {ln.strip() for ln in str(after.get("details", "")).splitlines()}
        removed = [ln for ln in old if ln.strip() not in new] if "details" in after else []
        self._x("INSERT INTO task_edits(task_id, project_id, ts, kind, by, session_id, note, before, after, removed) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, project_id, time.time(), kind, by, session_id, note, json.dumps(before), json.dumps(after),
                 "\n".join(removed)))

    def log_project_event(self, project_id: int, subject: str, action: str, by: str = "you", detail: str = "",
                          session_id: int | None = None) -> None:
        self._x("INSERT INTO project_events(project_id, ts, subject, action, by, session_id, detail) "
                "VALUES (?,?,?,?,?,?,?)", (project_id, time.time(), subject, action, by, session_id, detail[:20000]))

    def project_log(self, project_id: int, limit: int = 500) -> list[dict[str, Any]]:
        """Everything recorded for a project — task changes and project events — newest first."""
        tasks = self._q("SELECT e.*, t.title AS task_title FROM task_edits e LEFT JOIN tasks t ON t.id = e.task_id "
                        "WHERE e.project_id=? ORDER BY e.ts DESC LIMIT ?", (project_id, limit))
        out = []
        for r in tasks:
            title = r["task_title"] or json.loads(r["after"] or "{}").get("title") \
                or json.loads(r["before"] or "{}").get("title") or f"#{r['task_id']}"
            out.append({"ts": r["ts"], "subject": "task", "task_id": r["task_id"], "kind": r["kind"],
                        "action": r["kind"], "title": title, "by": r["by"], "detail": r["note"],
                        "removed": r["removed"], "session_id": r["session_id"]})
        for r in self._q("SELECT * FROM project_events WHERE project_id=? ORDER BY ts DESC LIMIT ?",
                         (project_id, limit)):
            out.append({**r, "removed": ""})
        out.sort(key=lambda e: e["ts"], reverse=True)
        return out[:limit]

    def task_edits(self, task_id: int | None = None, project_id: int | None = None,
                   since: float = 0) -> list[dict[str, Any]]:
        if task_id is not None:
            rows = self._q("SELECT * FROM task_edits WHERE task_id=? AND ts>=? ORDER BY ts, id", (task_id, since))
        else:
            rows = self._q("SELECT * FROM task_edits WHERE project_id=? AND ts>=? ORDER BY ts, id", (project_id, since))
        for r in rows:
            r["before"] = json.loads(r["before"] or "{}")
            r["after"] = json.loads(r["after"] or "{}")
        return rows

    # -- task groups -------------------------------------------------------------

    @staticmethod
    def _group_row(r: dict[str, Any]) -> dict[str, Any]:
        r["tags"] = json.loads(r["tags"] or "[]")
        return r

    def task_groups(self, project_id: int) -> list[dict[str, Any]]:
        return [self._group_row(r) for r in
                self._q("SELECT * FROM task_groups WHERE project_id=? ORDER BY position, id", (project_id,))]

    def task_group(self, gid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM task_groups WHERE id=?", (gid,))
        return self._group_row(rows[0]) if rows else None

    def save_task_group(self, project_id: int, gid: int | None = None, **fields: Any) -> int:
        fields = {k: (json.dumps(v) if k == "tags" else v) for k, v in fields.items()
                  if k in ("name", "priority", "tags", "position")}
        if gid is None:
            rows = self._q("SELECT COALESCE(MAX(position), 0) AS m FROM task_groups WHERE project_id=?", (project_id,))
            fields.setdefault("position", rows[0]["m"] + 1)
            fields.update(project_id=project_id, created=time.time())
            return self._x(f"INSERT INTO task_groups({', '.join(fields)}) VALUES "
                           f"({', '.join('?' for _ in fields)})", tuple(fields.values()))
        if fields:
            self._x(f"UPDATE task_groups SET {', '.join(f'{k}=?' for k in fields)} WHERE id=? AND project_id=?",
                    (*fields.values(), gid, project_id))
        return gid

    def delete_task_group(self, gid: int) -> None:
        """Its tasks move to the main queue; routines tied to it lose the tie."""
        self._x("UPDATE tasks SET group_id=NULL WHERE group_id=?", (gid,))
        self._x("UPDATE routines SET group_id=NULL, trigger=CASE WHEN trigger='group_done' THEN 'manual' "
                "ELSE trigger END WHERE group_id=?", (gid,))
        self._x("DELETE FROM task_groups WHERE id=?", (gid,))

    def delete_routine(self, rid: int) -> None:
        self._x("UPDATE tasks SET routine_id=NULL WHERE routine_id=?", (rid,))
        self._x("DELETE FROM routines WHERE id=?", (rid,))

    def delete_review_agent(self, aid: int) -> None:
        self._x("DELETE FROM review_agents WHERE id=?", (aid,))

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self._x("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def remove_project(self, pid: int) -> None:
        self._x("DELETE FROM projects WHERE id=?", (pid,))
        self._x("DELETE FROM settings WHERE key LIKE ?", (f"project:{pid}:%",))

    # -- links ---------------------------------------------------------------

    def add_link(self, a: int, b: int, note: str, contracts_a: list[str], contracts_b: list[str]) -> int:
        return self._x(
            "INSERT INTO links(project_a, project_b, note, contracts_a, contracts_b, created) VALUES (?,?,?,?,?,?)",
            (a, b, note, json.dumps(contracts_a), json.dumps(contracts_b), time.time()))

    def remove_link(self, lid: int) -> None:
        self._x("DELETE FROM links WHERE id=?", (lid,))

    def links(self, project_id: int | None = None) -> list[dict[str, Any]]:
        rows = (self._q("SELECT * FROM links WHERE project_a=? OR project_b=?", (project_id, project_id))
                if project_id is not None else self._q("SELECT * FROM links"))
        for r in rows:
            r["contracts_a"] = json.loads(r["contracts_a"])
            r["contracts_b"] = json.loads(r["contracts_b"])
        return rows

    # -- sessions ------------------------------------------------------------

    def add_session(self, **fields: Any) -> int:
        now = time.time()
        fields.setdefault("status", "starting")
        fields.update(started=now, last_activity=now)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self._x(f"INSERT INTO sessions({cols}) VALUES ({marks})", tuple(fields.values()))

    def update_session(self, sid: int, **fields: Any) -> None:
        fields.setdefault("last_activity", time.time())
        sets = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), sid))

    def session(self, sid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM sessions WHERE id=?", (sid,))
        return rows[0] if rows else None

    def sessions(self, project_id: int | None = None, active_only: bool = False,
                 include_ended_hours: float = 24) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        args: list[Any] = []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if active_only:
            sql += f" AND status IN ({','.join('?' for _ in ACTIVE)})"
            args += list(ACTIVE)
        else:  # recent history only, so the sidebar stays short
            sql += " AND (ended IS NULL OR ended > ?)"
            args.append(time.time() - include_ended_hours * 3600)
        return self._q(sql + " ORDER BY started", tuple(args))

    # -- edits / events / messages ------------------------------------------

    def add_edit(self, session_id: int, project_id: int, path: str, tool: str) -> None:
        self._x("INSERT INTO edits(session_id, project_id, path, tool, ts) VALUES (?,?,?,?,?)",
                (session_id, project_id, path, tool, time.time()))

    def edits(self, project_id: int, path: str | None = None, since: float = 0,
              exclude_session: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM edits WHERE project_id=? AND ts>=?"
        args: list[Any] = [project_id, since]
        if path is not None:
            sql += " AND path=?"
            args.append(path)
        if exclude_session is not None:
            sql += " AND session_id<>?"
            args.append(exclude_session)
        return self._q(sql + " ORDER BY ts DESC", tuple(args))

    def edits_by_session(self, session_id: int, since: float = 0) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM edits WHERE session_id=? AND ts>=? ORDER BY ts", (session_id, since))

    def session_files(self, session_id: int) -> list[str]:
        rows = self._q("SELECT path, MAX(ts) AS t FROM edits WHERE session_id=? GROUP BY path ORDER BY t DESC",
                       (session_id,))
        return [r["path"] for r in rows]

    def add_event(self, target: int, source: int | None, kind: str, text: str, path: str | None = None) -> int:
        return self._x("INSERT INTO events(target, source, kind, text, path, ts) VALUES (?,?,?,?,?,?)",
                       (target, source, kind, text, path, time.time()))

    def pending_events(self, target: int) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM events WHERE target=? AND delivered IS NULL ORDER BY ts", (target,))

    def recent_events(self, target: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
        if target is None:
            return self._q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        return self._q("SELECT * FROM events WHERE target=? ORDER BY ts DESC LIMIT ?", (target, limit))

    def mark_delivered(self, event_ids: list[int]) -> None:
        if event_ids:
            marks = ",".join("?" for _ in event_ids)
            self._x(f"UPDATE events SET delivered=? WHERE id IN ({marks})", (time.time(), *event_ids))

    def add_message(self, session_id: int, body: dict[str, Any]) -> None:
        self._x("INSERT INTO messages(session_id, ts, body) VALUES (?,?,?)",
                (session_id, time.time(), json.dumps(body)))

    def messages(self, session_id: int, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._q("SELECT body FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit))
        return [json.loads(r["body"]) for r in reversed(rows)]
