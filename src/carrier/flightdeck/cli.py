# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Flight Deck's terminal commands: ``carrier flightdeck <command>``.

Sessions themselves run in the desktop app; these commands manage the
project list and links, and show what is running. They talk to the store
directly (not a :class:`FlightDeck`), so using them while the app is open
never disturbs live sessions.

    carrier flightdeck status                       projects, links, sessions
    carrier flightdeck add <folder> [--name NAME]   register a project
    carrier flightdeck remove <project>             unregister (no active sessions)
    carrier flightdeck link <a> <b> [--note TEXT] [--a GLOB ...] [--b GLOB ...]
    carrier flightdeck unlink <link-id>

<project>, <a> and <b> accept a project id or name.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from carrier.core import gitutil
from carrier.flightdeck.coordination import ago
from carrier.core.db import ACTIVE, Store, default_path


def _store() -> Store:
    return Store(default_path())


def _project_id(store: Store, ref: str) -> int:
    for p in store.projects():
        if str(p["id"]) == ref or p["name"].lower() == ref.lower():
            return p["id"]
    sys.exit(f"No project called {ref!r}. See `carrier flightdeck status`.")


def cmd_status(store: Store, args: argparse.Namespace) -> int:
    projects = store.projects()
    if not projects:
        print("No projects yet. Add one with `carrier flightdeck add <folder>` or in the app.")
        return 0
    names = {p["id"]: p["name"] for p in projects}
    for p in projects:
        print(f"[{p['id']}] {p['name']}  {p['path']}")
        for s in store.sessions(p["id"]):
            where = f"  own copy: {s['branch']}" if s["worktree"] else ""
            print(f"      #{s['id']} {s['status']:<9} “{s['task']}”  {ago(s['last_activity'])}{where}")
    links = store.links()
    if links:
        print("\nLinks:")
        for l in links:
            print(f"  ({l['id']}) {names[l['project_a']]} ⇄ {names[l['project_b']]}"
                  + (f" — {l['note']}" if l["note"] else ""))
            if l["contracts_a"]:
                print(f"        {names[l['project_a']]} contracts: {', '.join(l['contracts_a'])}")
            if l["contracts_b"]:
                print(f"        {names[l['project_b']]} contracts: {', '.join(l['contracts_b'])}")
    active = sum(1 for s in store.sessions(active_only=True) if s["status"] in ACTIVE)
    print(f"\n{active} active session(s). Sessions run in the desktop app: `carrier app`.")
    return 0


def cmd_add(store: Store, args: argparse.Namespace) -> int:
    path = Path(args.folder).expanduser().resolve()
    if not path.is_dir():
        sys.exit(f"Folder not found: {path}")
    try:
        path = gitutil.repo_root(path)
    except gitutil.GitError:
        pass
    pid = store.add_project(args.name or path.name, str(path))
    print(f"Project [{pid}] {args.name or path.name}: {path}")
    return 0


def cmd_remove(store: Store, args: argparse.Namespace) -> int:
    pid = _project_id(store, args.project)
    if store.sessions(pid, active_only=True):
        sys.exit("End this project's sessions first (in the app).")
    store.remove_project(pid)
    print("Removed.")
    return 0


def cmd_link(store: Store, args: argparse.Namespace) -> int:
    a, b = _project_id(store, args.a), _project_id(store, args.b)
    if a == b:
        sys.exit("Pick two different projects.")
    lid = store.add_link(a, b, args.note or "", args.a_glob or [], args.b_glob or [])
    print(f"Linked ({lid}).")
    return 0


def cmd_unlink(store: Store, args: argparse.Namespace) -> int:
    store.remove_link(int(args.link))
    print("Unlinked.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="carrier flightdeck", description="Flight Deck: projects, links, sessions.")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="projects, links and sessions").set_defaults(func=cmd_status)
    sp = sub.add_parser("add", help="register a project folder")
    sp.add_argument("folder")
    sp.add_argument("--name")
    sp.set_defaults(func=cmd_add)
    sp = sub.add_parser("remove", help="unregister a project")
    sp.add_argument("project")
    sp.set_defaults(func=cmd_remove)
    sp = sub.add_parser("link", help="declare that two projects affect each other")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("--note", help="how they relate, e.g. 'the server defines the API schema the client uses'")
    sp.add_argument("--a", dest="a_glob", action="append", metavar="GLOB", help="contract files in the first project")
    sp.add_argument("--b", dest="b_glob", action="append", metavar="GLOB", help="contract files in the second project")
    sp.set_defaults(func=cmd_link)
    sp = sub.add_parser("unlink", help="remove a link")
    sp.add_argument("link")
    sp.set_defaults(func=cmd_unlink)
    args = p.parse_args(argv if argv else ["status"])
    return args.func(_store(), args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
