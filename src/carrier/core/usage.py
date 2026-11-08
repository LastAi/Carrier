# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Usage: tokens, cost and Claude's usage limits. Everything here is local and read only.

Two sources, no network:

* **Usage limits** (subscription plans). While an agent runs, Claude Code reports the
  state of each limit window (5-hour, weekly, per model): ok / near / reached, how much
  is used when it says so, and when it resets. Carrier keeps the latest report per
  window (:func:`record_limit`). Nothing can be asked while no agent runs, so the
  figures are "as of" the last report, and a window whose reset time has passed is
  shown as reset.
* **Tokens.** Claude Code writes every conversation, with each reply's token counts,
  to log files under ``~/.claude/projects``. Carrier reads them, read only and a little
  at a time (:func:`scan`), and keeps just the numbers (model, tokens, time, folder,
  conversation id) for :data:`KEEP_DAYS` days. That covers Carrier's own sessions and
  plain Claude Code use alike. Turned off with the ``usage_read_logs`` setting.

Cost is an estimate at API list prices (:data:`PRICES`); it means real money only for
API-key use. On a subscription, the limit windows are what matter.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

KEEP_DAYS = 60
READ_CHUNK = 8 * 1024 * 1024

GUESS_SECONDS = 600  # a reached limit with no reset time given: assume this long, then let work try again
MODEL_WINDOWS = ("seven_day_opus", "seven_day_sonnet")  # Claude Code switches model; they don't stop work

WINDOWS = {  # Claude Code's rate-limit kinds -> label, length in seconds
    "five_hour": ("5-hour limit", 5 * 3600),
    "seven_day": ("Weekly limit", 7 * 86400),
    "seven_day_opus": ("Weekly Opus limit", 7 * 86400),
    "seven_day_sonnet": ("Weekly Sonnet limit", 7 * 86400),
    "overage": ("Extra usage", 0),
}

# API list prices in USD per million tokens (input, output, cache read), checked November 2026.
# Cache writes cost 1.25x input (5-minute) or 2x input (1-hour). Older models: no estimate.
PRICES = {
    "fable": (10.0, 50.0, 1.0),
    "opus": (4.0, 20.0, 0.20),
    "sonnet": (2.0, 10.0, 0.10),
    "haiku": (0.10, 0.50, 0.01),  # prompts up to 100K tokens
}
_PRICED = re.compile(r"claude-(fable|opus|sonnet|haiku)-5")

SETTINGS = {  # per-user settings (core/usersettings.py) and their defaults
    "usage_plan": "auto",  # "auto" | "subscription" | "api"
    "usage_read_logs": True,
    "usage_warn_at": 80,  # % of a limit window: warn in the app
    "usage_hold_at": 90,  # % of a limit window: hold automatic starts
    "usage_hold_takeoff": True,
    "usage_hold_routines": True,
    "usage_auto_resume": True,
}


def settings() -> dict[str, Any]:
    from carrier.core import usersettings

    s = usersettings.load()
    out = {k: s.get(k, v) for k, v in SETTINGS.items()}
    if out["usage_plan"] not in ("auto", "subscription", "api"):
        out["usage_plan"] = "auto"
    for k in ("usage_warn_at", "usage_hold_at"):
        try:
            out[k] = max(1, min(100, int(out[k])))
        except (TypeError, ValueError):
            out[k] = SETTINGS[k]
    for k in ("usage_read_logs", "usage_hold_takeoff", "usage_hold_routines", "usage_auto_resume"):
        out[k] = bool(out[k])
    return out


# -- limit windows ------------------------------------------------------------------


def record_limit(store: Any, info: dict[str, Any], now: float | None = None) -> tuple[dict[str, Any], str]:
    """Keep Claude Code's latest report for one limit window.

    Returns the window's new state and its previous status ("" if unknown or reset).
    """
    now = time.time() if now is None else now
    kind = str(info.get("kind") or "five_hour")
    windows = store.get_setting("usage_limits", {}) or {}
    old = windows.get(kind) or {}
    prev = old.get("status", "") if not _expired(old, now) else ""
    util = info.get("utilization")
    state = {
        "status": str(info.get("status") or "allowed"),
        "resets_at": float(info["resets_at"]) if info.get("resets_at") else None,
        "utilization": float(util) if isinstance(util, (int, float)) else None,
        "overage_status": info.get("overage_status") or None,
        "seen": now,
    }
    if state["status"] == "rejected" and not state["resets_at"]:
        state["guess_until"] = now + GUESS_SECONDS  # so it can't stay "reached" for ever
    if state["utilization"] is None and state["resets_at"] == old.get("resets_at") and not _expired(old, now):
        state["utilization"] = old.get("utilization")  # same window, no new figure: keep the last one
    windows[kind] = state
    store.set_setting("usage_limits", windows)
    return state, prev


def _expired(w: dict[str, Any], now: float) -> bool:
    until = w.get("resets_at") or w.get("guess_until")
    return bool(until) and float(until) <= now


def record_account(store: Any, api_key_source: str | None) -> None:
    """From Claude Code's start-up message: "none" means a claude.ai login (a subscription)."""
    if api_key_source:
        store.set_setting("usage_key_source", str(api_key_source))


def plan(store: Any, s: dict[str, Any] | None = None) -> str:
    """"subscription", "api" or "unknown" (setting first, then what Claude Code reported)."""
    s = s or settings()
    if s["usage_plan"] != "auto":
        return s["usage_plan"]
    if store.get_setting("usage_limits"):
        return "subscription"  # only subscriptions get limit-window reports
    src = store.get_setting("usage_key_source")
    if src is None:
        return "unknown"
    return "subscription" if src == "none" else "api"


def limits(store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """The known limit windows, newest state, with reset windows shown as reset."""
    now = time.time() if now is None else now
    out = []
    for kind, w in (store.get_setting("usage_limits", {}) or {}).items():
        label, length = WINDOWS.get(kind, (kind.replace("_", " ").capitalize(), 0))
        reset = _expired(w, now)
        util = None if reset else w.get("utilization")
        pct = None if util is None else round(util * 100, 1)  # Claude Code reports a fraction (above 1 with extra usage)
        out.append({"kind": kind, "label": label, "status": "allowed" if reset else w.get("status", "allowed"),
                    "percent": pct, "resets_at": None if reset else w.get("resets_at"),
                    "reset_at": w.get("resets_at") if reset else None, "seen": w.get("seen"),
                    "overage_status": None if reset else w.get("overage_status"),
                    "start": (w["resets_at"] - length) if w.get("resets_at") and length and not reset else None})
    order = list(WINDOWS)
    out.sort(key=lambda x: order.index(x["kind"]) if x["kind"] in order else 99)
    return out


def reached(store: Any, now: float | None = None) -> float | None:
    """If a limit that stops work is reached right now and Claude Code said when it resets:
    when work can go on (the latest such reset). Per-model weekly limits don't count: Claude
    Code moves to another model. A reached limit without a reset time gives None (callers
    retry on their own schedule) but still shows in :func:`level`."""
    now = time.time() if now is None else now
    times = [w["resets_at"] for w in limits(store, now)
             if w["status"] == "rejected" and w["kind"] not in ("overage", *MODEL_WINDOWS) and w["resets_at"]]
    return max(times) if times else None


def level(store: Any, s: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """How close to the limits: {"state": "ok" | "near" | "hold" | "reached", "text", "window"}."""
    now = time.time() if now is None else now
    worst = {"state": "ok", "text": "", "window": None}
    rank = {"ok": 0, "near": 1, "hold": 2, "reached": 3}
    for w in limits(store, now):
        if w["kind"] == "overage":
            continue
        model_only = w["kind"] in MODEL_WINDOWS  # at most a warning: work goes on with another model
        pct = w["percent"]
        when = f" (resets {_clock(w['resets_at'], now)})" if w["resets_at"] else ""
        if w["status"] == "rejected":
            extra = " — using extra usage" if w["overage_status"] in ("allowed", "allowed_warning") else ""
            st, text = "reached", f"{w['label']} reached{extra}{when}."
        elif pct is not None and pct >= s["usage_hold_at"]:
            st, text = "hold", f"{w['label']} at {pct:g}%{when}."
        elif (pct is not None and pct >= s["usage_warn_at"]) or w["status"] == "allowed_warning":
            st = "near"
            text = f"{w['label']} at {pct:g}%{when}." if pct is not None else f"Close to the {w['label'].lower()}{when}."
        else:
            continue
        if model_only:
            st = "near"
        if rank[st] > rank[worst["state"]]:
            worst = {"state": st, "text": text, "window": w["kind"]}
    return worst


def hold_reason(store: Any, what: str, s: dict[str, Any] | None = None) -> str:
    """Why automatic starts of ``what`` ("takeoff" | "routines") should wait, or ""."""
    s = s or settings()
    lv = level(store, s)
    if lv["state"] == "reached":
        return lv["text"]  # nothing starts on its own while a limit is reached
    if lv["state"] == "hold" and s[f"usage_hold_{what}"]:
        return lv["text"]
    return ""


def _clock(ts: float | None, now: float) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    if ts - now < 20 * 3600:
        return d.strftime("%H:%M")
    return d.strftime("%a %H:%M")


_RESET_IN_TEXT = re.compile(r"\|(\d{10})\b")


def reset_from_text(text: str) -> float | None:
    """Older Claude Code versions end the limit message with "|<unix time>"."""
    m = _RESET_IN_TEXT.search(text or "")
    return float(m.group(1)) if m else None


# -- token logs -----------------------------------------------------------------------


def log_dirs() -> list[Path]:
    """Folders where Claude Code keeps conversation logs (``<config>/projects``)."""
    override = os.environ.get("CARRIER_CLAUDE_LOGS")
    if override is not None:  # tests, or an unusual setup
        return [Path(p) for p in override.split(os.pathsep) if p and Path(p).is_dir()]
    roots: list[Path] = []
    for p in (os.environ.get("CLAUDE_CONFIG_DIR") or "").split(","):
        if p.strip():
            roots.append(Path(p.strip()).expanduser())
    roots += [Path.home() / ".claude", Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude"]
    out, seen = [], set()
    for r in roots:
        d = r / "projects"
        try:
            key = str(d.resolve())
        except OSError:
            continue
        if d.is_dir() and key not in seen:
            seen.add(key)
            out.append(d)
    return out


_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {"running": False, "done": False, "files": 0, "read": 0, "last": 0.0, "db": ""}


def scan_state(store: Any = None) -> dict[str, Any]:
    """Progress of reading the logs into ``store`` (a fresh state for a different database)."""
    if store is not None and _scan_state["db"] != str(getattr(store, "path", "")) and not _scan_state["running"]:
        return {"running": False, "done": False, "files": 0, "read": 0, "last": 0.0, "db": ""}
    return dict(_scan_state)


def find_logs() -> list[Path]:
    """Claude Code's log files, newest first."""
    files = []
    for d in log_dirs():
        files += [p for p in d.rglob("*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: -_mtime(p))


def scan(store: Any, budget: float = 2.0, now: float | None = None, files: list[Path] | None = None) -> dict[str, Any]:
    """Read new lines from Claude Code's logs, for at most ``budget`` seconds.

    Picks up where it stopped (per file: size, time and read position). Returns the
    scan state; ``done`` is False if there is more to read. ``files`` (from
    :func:`find_logs`) saves looking for them again on each call of a long first scan.
    """
    if not settings()["usage_read_logs"]:
        return {**scan_state(), "off": True}
    if not _scan_lock.acquire(blocking=False):
        return scan_state()
    now = time.time() if now is None else now
    cutoff = now - KEEP_DAYS * 86400
    try:
        _scan_state.update(running=True, db=str(getattr(store, "path", "")))
        if files is None:
            files = find_logs()
        started = time.time()  # the budget is for reading, not for finding the files
        known = {r["path"]: r for r in store.usage_files()}
        _scan_state.update(files=len(files), read=0)
        done = True
        for i, f in enumerate(files):
            _scan_state["read"] = i
            try:
                st = f.stat()
            except OSError:
                continue
            k = known.get(str(f))
            if k and k["size"] == st.st_size and k["mtime"] == st.st_mtime:
                continue
            if st.st_mtime < cutoff:
                store.usage_file(str(f), st.st_size, st.st_mtime, st.st_size)  # too old to matter
                continue
            offset = int(k["offset"]) if k and st.st_size >= k["offset"] else 0  # shrank: start over
            offset = _read_file(store, f, offset, cutoff, started + budget)
            store.usage_file(str(f), st.st_size if offset >= st.st_size else -1, st.st_mtime, offset)
            if time.time() > started + budget:
                done = False
                break
        if done:
            store.prune_usage(cutoff)
        _scan_state.update(done=done, last=time.time(), read=_scan_state["files"] if done else _scan_state["read"])
        return scan_state()
    finally:
        _scan_state["running"] = False
        _scan_lock.release()


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _read_file(store: Any, f: Path, offset: int, cutoff: float, deadline: float) -> int:
    """Read complete lines from ``offset``; returns the new position."""
    try:
        fh = open(f, "rb")
    except OSError:
        return offset
    with fh:
        fh.seek(offset)
        rest = b""  # an unfinished line; it starts at file position ``offset``
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            data = rest + chunk
            end = data.rfind(b"\n")
            if end < 0:
                rest = data
                continue
            rows = [r for r in (parse_line(ln) for ln in data[:end].split(b"\n")) if r and r["ts"] >= cutoff]
            if rows:
                store.add_usage(rows)
            offset += end + 1
            rest = data[end + 1:]
            if time.time() > deadline:
                break
        return offset


def parse_line(line: bytes) -> dict[str, Any] | None:
    """One log line -> a usage row, or None (not an assistant reply with token counts)."""
    if b'"usage"' not in line or b'"assistant"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    m = d.get("message") or {}
    u = m.get("usage") if isinstance(m, dict) else None
    model = str(m.get("model") or "") if isinstance(m, dict) else ""
    if not isinstance(u, dict) or not model or model.startswith("<"):
        return None
    try:
        ts = datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

    def n(v: Any) -> int:
        return int(v) if isinstance(v, (int, float)) else 0

    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    key = f"{m.get('id')}:{d.get('requestId') or ''}" if m.get("id") else str(d.get("uuid") or "")
    if not key:
        return None
    return {"key": key, "ts": ts, "model": model, "session": str(d.get("sessionId") or ""),
            "cwd": str(d.get("cwd") or ""), "input": n(u.get("input_tokens")), "output": n(u.get("output_tokens")),
            "cache_write": n(u.get("cache_creation_input_tokens")), "cache_write_1h": n(cc.get("ephemeral_1h_input_tokens")),
            "cache_read": n(u.get("cache_read_input_tokens"))}


def cost(row: dict[str, Any]) -> float | None:
    """Estimated API-price cost of a row (or a sum of rows of one model)."""
    m = _PRICED.search(row.get("model") or "")
    if not m:
        return None
    pin, pout, pread = PRICES[m.group(1)]
    w1h = min(row.get("cache_write_1h", 0), row.get("cache_write", 0))
    w5m = row.get("cache_write", 0) - w1h
    return (row.get("input", 0) * pin + row.get("output", 0) * pout + row.get("cache_read", 0) * pread
            + w5m * pin * 1.25 + w1h * pin * 2.0) / 1_000_000


# -- totals -----------------------------------------------------------------------------

TOKEN_KEYS = ("input", "output", "cache_write", "cache_read")


def _blank() -> dict[str, Any]:
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0, "cost": 0.0, "unpriced": 0}


def _add(acc: dict[str, Any], r: dict[str, Any]) -> None:
    for k in TOKEN_KEYS:
        acc[k] += r.get(k, 0)
    acc["total"] += sum(r.get(k, 0) for k in TOKEN_KEYS)
    c = cost(r)
    if c is None:
        acc["unpriced"] += sum(r.get(k, 0) for k in TOKEN_KEYS)
    else:
        acc["cost"] += c


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


def summary(store: Any, now: float | None = None, days: int = 14) -> dict[str, Any]:
    """Everything the Usage panel shows."""
    now = time.time() if now is None else now
    s = settings()
    keys = [datetime.fromtimestamp(now - i * 86400).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    week_keys = set(keys[-7:])  # the last 7 days, today included
    first = datetime.strptime(keys[0], "%Y-%m-%d").timestamp() - 3600  # local midnight (with DST slack)
    wins = limits(store, now)
    rows = store.usage_rows(first)  # grouped by day, model, session, cwd (and a 'ts' minimum)

    # Which Flight Deck session (and so task and project) a Claude Code conversation belongs to.
    by_sdk: dict[str, dict[str, Any]] = {}
    for x in store.usage_sessions():
        if x["sdk_session"]:
            by_sdk[x["sdk_session"]] = x
    projects = store.projects()
    roots: list[tuple[str, int]] = []
    for p in projects:
        roots.append((_norm(p["path"]), p["id"]))
        for f in p.get("folders") or []:
            if isinstance(f, dict) and f.get("path"):
                roots.append((_norm(f["path"]), p["id"]))
    roots.sort(key=lambda r: -len(r[0]))
    names = {p["id"]: p["name"] for p in projects}

    def project_of(cwd: str, sess: dict[str, Any] | None) -> int | None:
        if sess and sess.get("project_id"):
            return sess["project_id"]
        c = _norm(cwd)
        for root, pid in roots:
            if c == root or c.startswith(root + os.sep):
                return pid
        return None

    week, carrier_week, other_week = _blank(), _blank(), _blank()
    per_day: dict[str, dict[str, Any]] = {}
    per_model: dict[str, dict[str, Any]] = {}
    per_project: dict[Any, dict[str, Any]] = {}
    per_task: dict[int, dict[str, Any]] = {}
    for r in rows:
        sess = by_sdk.get(r["session"])
        day = per_day.setdefault(r["day"], {"day": r["day"], **_blank(), "carrier": 0})
        _add(day, r)
        if sess:
            day["carrier"] += sum(r[k] for k in TOKEN_KEYS)
        if r["day"] not in week_keys:
            continue
        _add(week, r)
        _add(carrier_week if sess else other_week, r)
        _add(per_model.setdefault(r["model"], {"model": r["model"], **_blank()}), r)
        pid = project_of(r["cwd"], sess)
        _add(per_project.setdefault(pid, {"project_id": pid, "name": names.get(pid, "Outside your projects"),
                                          **_blank()}), r)
        if sess and sess.get("task_id"):
            _add(per_task.setdefault(sess["task_id"], {"task_id": sess["task_id"], "title": sess["task"],
                                                     **_blank()}), r)
    today = {k: v for k, v in per_day.get(keys[-1], _blank()).items() if k not in ("day", "carrier")}
    for w in wins:
        w["tokens"] = store.usage_total(w["start"]) if w.get("start") else None
    day_list = []
    for k in keys:
        d = per_day.get(k, {"day": k, **_blank(), "carrier": 0})
        day_list.append({"day": k, "total": d["total"], "carrier": d["carrier"], "cost": round(d["cost"], 4)})

    def top(d: dict[Any, dict[str, Any]], n: int) -> list[dict[str, Any]]:
        return [{**v, "cost": round(v["cost"], 4)} for v in sorted(d.values(), key=lambda v: -v["total"])[:n]]

    return {
        "plan": plan(store, s), "settings": s, "limits": wins, "level": level(store, s, now),
        "reached_until": reached(store, now),
        "today": {**today, "cost": round(today["cost"], 4)}, "week": {**week, "cost": round(week["cost"], 4)},
        "carrier_week": {**carrier_week, "cost": round(carrier_week["cost"], 4)},
        "other_week": {**other_week, "cost": round(other_week["cost"], 4)},
        "days": day_list, "models": top(per_model, 8), "projects": top(per_project, 12), "tasks": top(per_task, 10),
        "scan": {**scan_state(store), "dirs": [str(d) for d in log_dirs()] if s["usage_read_logs"] else []},
        "hold": {"takeoff": hold_reason(store, "takeoff", s), "routines": hold_reason(store, "routines", s)},
    }


def brief(store: Any) -> dict[str, Any]:
    """The small version for the always-on header and alerts (no log reading)."""
    s = settings()
    return {"plan": plan(store, s), "level": level(store, s), "reached_until": reached(store),
            "limits": [{k: w[k] for k in ("kind", "label", "status", "percent", "resets_at", "seen")}
                       for w in limits(store) if w["kind"] != "overage"]}
