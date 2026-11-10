# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Debrief's command line: ``carrier debrief [command]`` (or just ``debrief``).

Debrief is Carrier's review tool. Running plain ``debrief`` is all most
people need — it prepares (or resumes) a review and opens it in the
terminal. Everything else is optional::

    debrief                  start or resume a review   (same as `debrief start`)
    debrief init             set up this repo (config + optional Stop hook)
    debrief start --from REF review from a specific commit/tag/branch
    debrief prepare          build a review without opening it (used by the hook)
    debrief list             print the prepared items and flags (no UI)
    debrief status           checkpoint and pending changes
    debrief fixes [--run]    show the fix list, or hand it to Claude Code
    debrief criteria         list active criteria
    debrief checkpoint [REF] show or move the checkpoint
    debrief doctor           check git, Claude Code and the Agent SDK
    debrief app              open the Carrier desktop app
    debrief uninstall-hook   remove the Stop hook

Only argparse is used, to keep dependencies minimal.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from carrier import __version__
from carrier.core import gitutil
from carrier.debrief import hooks
from carrier.core.config import CONFIG_TEMPLATE, load_config
from carrier.debrief.criteria import load_criteria
from carrier.core.llm import AgentSDKBackend, Backend, BackendError
from carrier.debrief.projects import project_kwargs, review_matches
from carrier.debrief.pipeline import NothingToReview, prepare
from carrier.debrief.store import all_reviews, is_stale, open_review, review_dir, supersede

WAIT_FOR_BACKGROUND_SECONDS = 600


def _say(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _repo() -> Path:
    return gitutil.repo_root(Path.cwd())


def _backend(no_ai: bool, repo: Path) -> Backend | None:
    if no_ai:
        return None
    return AgentSDKBackend(concurrency=int(load_config(repo).get("agents", "concurrency")))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load_config(repo)
    cfg.data_dir.mkdir(exist_ok=True)
    conf = cfg.data_dir / "config.toml"
    if not conf.exists():
        conf.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        _say(f"Created {conf.relative_to(repo)} (all settings optional).")
    crit = cfg.data_dir / "criteria"
    if not crit.exists():
        crit.mkdir()
        (crit / "README.md").write_text(
            "Put your own Debrief review criteria here as markdown files.\n"
            "See docs/writing-criteria.md in the Carrier repository.\n", encoding="utf-8")
    want_hook = args.hook
    if want_hook is None and sys.stdin.isatty():
        answer = input("Prepare reviews automatically when Claude Code finishes? [Y/n] ").strip().lower()
        want_hook = answer in ("", "y", "yes")
    if want_hook:
        path, changed = hooks.install_stop_hook(repo, shared=args.shared)
        _say(f"{'Installed' if changed else 'Already installed'} Stop hook in {path.relative_to(repo)}.")
    _say("Done. Run `debrief` (or open the Carrier app) whenever you want to review.")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    if os.environ.get("CARRIER_INNER"):
        return 0  # called from inside one of our own agent sessions — never recurse
    repo = _repo()
    cfg = load_config(repo)
    if args.background:
        if not hooks.prepare_running(cfg):
            hooks.spawn_background_prepare(cfg)
        return 0
    with hooks.held_lock(cfg) as ok:
        if not ok:
            _say("A prepare is already running.", args.quiet)
            return 0
        try:
            review = asyncio.run(prepare(repo, _backend(args.no_ai, repo), args.from_ref,
                                         progress=lambda m: _say(m, args.quiet), **project_kwargs(repo)))
        except NothingToReview as exc:
            _say(str(exc), args.quiet)
            return 0
    _say(f"Prepared review {review.id}. Run `debrief` to open it.", args.quiet)
    return 0


def _wait_for_background(cfg) -> None:
    if not hooks.prepare_running(cfg):
        return
    _say("Waiting for the background prepare to finish…")
    deadline = time.time() + WAIT_FOR_BACKGROUND_SECONDS
    while hooks.prepare_running(cfg) and time.time() < deadline:
        time.sleep(1)


def cmd_start(args: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load_config(repo)
    _wait_for_background(cfg)

    review = None
    if not args.fresh and not args.from_ref:
        review = open_review(cfg, status="in_progress")
        pk = project_kwargs(repo)
        if review and not review_matches(review, (pk.get("project") or {}).get("id"), pk.get("extras", [])):
            review = None  # prepared for other folders (or another project): prepare a new one
        if review and is_stale(cfg, review):
            _say(f"Review {review.id} is out of date (another review finished since). "
                 "Preparing a new one.")
            supersede(cfg, keep_id="", statuses=("in_progress",))
            review = None
        if review:
            _say(f"Resuming review {review.id} ({review.decided_count}/{len(review.items)} decided). "
                 "Use --fresh to start over with the latest changes.")
    backend = _backend(args.no_ai, repo)
    if review is None:
        with hooks.held_lock(cfg) as ok:
            if not ok:
                _say("A prepare is already running; try again in a moment.")
                return 1
            _say("(Press Ctrl+C to cancel — nothing is saved until preparing finishes.)")
            try:
                review = asyncio.run(prepare(repo, backend, args.from_ref, reuse=not args.fresh,
                                             progress=_say, **project_kwargs(repo)))
            except NothingToReview as exc:
                _say(str(exc) + " Nothing to review.")
                return 0

    # Imported here so non-UI commands start fast and work without a terminal.
    from carrier.core.notes import collect_notes
    from carrier.debrief.record import finish
    from carrier.debrief.tui.app import DebriefApp

    notes, _ = collect_notes(cfg)
    finished = DebriefApp(review, cfg, notes, backend).run()
    if not finished:
        _say(f"Review saved ({review.decided_count}/{len(review.items)} decided). Run `debrief` to continue.")
        return 0
    result = finish(cfg, review)
    _say(f"Review finished. Record: {result.record.relative_to(repo)}")
    _say(result.message)
    if result.fixes:
        _say(f"Fix list: {result.fixes.relative_to(repo)}\n"
             "Hand it to Claude Code with:  debrief fixes --run")
    else:
        _say("No fixes requested.")
    from carrier.debrief.record import render_later

    later = render_later(review)
    if later:
        _say("Approved with follow-ups for later (add them to your task list):\n" + later)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load_config(repo)
    _wait_for_background(cfg)
    review = open_review(cfg)
    if review is None or args.fresh or args.from_ref:
        try:
            review = asyncio.run(prepare(repo, _backend(args.no_ai, repo), args.from_ref,
                                         reuse=not args.fresh, progress=_say, **project_kwargs(repo)))
        except NothingToReview as exc:
            _say(str(exc))
            return 0
    print(f"Review {review.id}: {review.base[:10]} → {review.target[:10]}  ({review.status})")
    for w in review.warnings:
        print(f"  ⚠ {w}")
    for item in review.items:
        sev = (item.top_severity or "").upper()
        decision = f"[{item.decision.action}]" if item.decision else ""
        print(f"\n{item.id:>4}  {sev:<6} {item.title} {decision}")
        print(f"      {', '.join(item.paths)}")
        for f in item.flags:
            print(f"      - {f.severity}: {f.criterion}: {f.reason}")
    return 0


def _ns(repo: Path) -> str | None:
    """A Carrier project's checkpoint namespace when ``repo`` is its main folder."""
    project = project_kwargs(repo).get("project")
    return str(project["id"]) if project else None


def cmd_status(args: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load_config(repo)
    cp = gitutil.get_checkpoint(repo, _ns(repo))
    if cp:
        when = gitutil.git(repo, "show", "-s", "--format=%ci", cp).strip()
        print(f"Checkpoint: {cp[:10]} ({when})")
    else:
        print("Checkpoint: none yet (first review will start from "
              f"{cfg.get('review', 'first_review_from')})")
    base = cp or gitutil.resolve(repo, cfg.get("review", "first_review_from")) or "HEAD"
    stat = gitutil.git(repo, "diff", "--shortstat", base, "--", ".", ":(exclude).carrier", check=False).strip()
    untracked = [p for p in gitutil.git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
                 if not p.startswith(".carrier/")]
    print(f"Changes since: {stat or 'none in tracked files'}"
          + (f"; {len(untracked)} untracked file(s)" if untracked else ""))
    if hooks.prepare_running(cfg):
        print("Background prepare: running")
    r = open_review(cfg)
    if r:
        print(f"Open review: {r.id} ({r.status}, {r.decided_count}/{len(r.items)} decided)")
    hook_on = any(hooks.is_our_hook(p.read_text(encoding="utf-8"))
                  for p in (hooks.settings_path(repo, False), hooks.settings_path(repo, True)) if p.exists())
    print(f"Stop hook: {'installed' if hook_on else 'not installed (debrief init)'}")
    return 0


def cmd_fixes(args: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load_config(repo)
    done = [r for r in all_reviews(cfg) if r.status == "done"]
    path = next((review_dir(cfg, r.id) / "fixes.md" for r in reversed(done)
                 if (review_dir(cfg, r.id) / "fixes.md").exists()), None)
    if path is None:
        _say("No fix list yet — finish a review with at least one Fix or Revert.")
        return 1
    text = path.read_text(encoding="utf-8")
    if not args.run:
        print(text)
        _say(f"\n({path.relative_to(repo)}) — run `debrief fixes --run` to start Claude Code with it.")
        return 0
    claude = shutil.which("claude")
    if not claude:
        _say("The `claude` command was not found on PATH. Paste the fix list into Claude Code instead.")
        return 1
    from carrier.core.terminal import fix_prompt

    # Run Claude Code right here in this terminal, pointing it at the file
    # (avoids pasting a long, multi-line prompt onto the command line).
    return subprocess.call([claude, fix_prompt(repo, path)], cwd=repo)


def cmd_criteria(args: argparse.Namespace) -> int:
    cfg = load_config(_repo())
    disabled = set(cfg.get("criteria", "disabled"))
    for c in load_criteria(cfg, include_disabled=True):
        state = "off" if (not c.enabled or c.name in disabled) else "on "
        extra = " sees-rationale" if c.sees_rationale else ""
        print(f"[{state}] {c.name:<16} {c.severity:<6} {c.title}  ({c.source}{extra})")
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    repo = _repo()
    if args.ref:
        sha = gitutil.resolve(repo, args.ref)
        if not sha:
            _say(f"Unknown ref: {args.ref}")
            return 1
        gitutil.set_checkpoint(repo, sha, _ns(repo))
        print(f"Checkpoint moved to {sha[:10]}.")
    else:
        cp = gitutil.get_checkpoint(repo, _ns(repo))
        print(cp or "No checkpoint yet.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"Carrier {__version__}, Python {sys.version.split()[0]}")
    try:
        print(f"git repo: {_repo()}")
    except gitutil.GitError as exc:
        print(f"✗ {exc}")
        ok = False
    print(f"claude CLI on PATH: {shutil.which('claude') or 'no (the Agent SDK may bundle its own)'}")
    import importlib.util

    if importlib.util.find_spec("claude_agent_sdk") is None:
        print("✗ claude-agent-sdk not installed (pip install claude-agent-sdk)")
        return 1
    print("claude-agent-sdk: installed")
    print("Asking Claude a one-word question…")
    try:
        reply = asyncio.run(AgentSDKBackend().ask(system="Reply with one word.", prompt="Say OK.",
                                                  model="haiku"))
        print(f"✓ Claude replied: {reply[:40]!r}")
    except BackendError as exc:
        print(f"✗ {exc}\n  Make sure Claude Code is installed and you are logged in (`claude`).")
        ok = False
    return 0 if ok else 1


def cmd_app(args: argparse.Namespace) -> int:
    from carrier.desktop.main import main as app_main

    return app_main()


def cmd_uninstall_hook(args: argparse.Namespace) -> int:
    changed = hooks.remove_stop_hook(_repo())
    _say("Removed hook from: " + ", ".join(map(str, changed)) if changed else "No Debrief hook found.")
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="debrief",
                                description="Debrief (part of Carrier): human-led, agent-guided review of "
                                            "Claude Code's work.")
    p.add_argument("--version", action="version", version=f"Carrier {__version__} — Debrief")
    sub = p.add_subparsers(dest="command")

    def review_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--from", dest="from_ref", metavar="REF",
                        help="start from this commit/tag/branch instead of the checkpoint")
        sp.add_argument("--no-ai", action="store_true", help="no model calls: group by file, no flags or guide")
        sp.add_argument("--fresh", action="store_true", help="ignore any saved/open review and rebuild")

    sp = sub.add_parser("start", help="start or resume a review (default)")
    review_opts(sp)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("init", help="set up Debrief in this repository")
    sp.add_argument("--hook", dest="hook", action="store_true", default=None, help="install the Stop hook")
    sp.add_argument("--no-hook", dest="hook", action="store_false", help="do not install the Stop hook")
    sp.add_argument("--shared", action="store_true", help="put the hook in .claude/settings.json (committed)")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("prepare", help="build a review without opening it")
    sp.add_argument("--from", dest="from_ref", metavar="REF")
    sp.add_argument("--no-ai", action="store_true")
    sp.add_argument("--background", action="store_true", help="run detached and return immediately")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("list", help="print items and flags without the UI")
    review_opts(sp)
    sp.set_defaults(func=cmd_list)

    sub.add_parser("status", help="checkpoint and pending changes").set_defaults(func=cmd_status)

    sp = sub.add_parser("fixes", help="show the latest fix list")
    sp.add_argument("--run", action="store_true", help="start Claude Code with the fix list as its task")
    sp.set_defaults(func=cmd_fixes)

    sub.add_parser("criteria", help="list criteria").set_defaults(func=cmd_criteria)

    sp = sub.add_parser("checkpoint", help="show or move the checkpoint")
    sp.add_argument("ref", nargs="?")
    sp.set_defaults(func=cmd_checkpoint)

    sub.add_parser("doctor", help="check the setup").set_defaults(func=cmd_doctor)
    sub.add_parser("app", help="open the desktop app").set_defaults(func=cmd_app)
    sub.add_parser("uninstall-hook", help="remove the Stop hook").set_defaults(func=cmd_uninstall_hook)
    return p


COMMANDS = {"start", "init", "prepare", "list", "status", "fixes", "criteria",
            "checkpoint", "doctor", "app", "uninstall-hook"}


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``debrief ...`` and ``carrier debrief ...``."""
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Plain `debrief` (optionally with start's flags, e.g. `debrief --from v1`)
    # means `debrief start`.
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv = ["start", *argv]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except gitutil.GitError as exc:
        _say(f"debrief: {exc}")
        return 2
    except KeyboardInterrupt:
        _say("Cancelled.")
        return 130
    except BrokenPipeError:  # e.g. `debrief list | head`
        return 0
