# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Usage and limits: reading Claude Code's logs, limit windows, holding and continuing work."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from carrier.core import usage, usersettings
from carrier.core.db import Store
from carrier.flightdeck.manager import FlightDeck, UsageHold

from .fakeclaude import ALL_CLIENTS, FakeClaudeClient
from .test_flightdeck import events, make_repo, wait


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def reply(ts: float, mid: str, *, model: str = "claude-sonnet-5-5", session: str = "s1", cwd: str = "/w",
          inp: int = 100, out: int = 50, read: int = 1000, write: int = 0, req: str = "r1") -> str:
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "sessionId": session, "cwd": cwd,
                       "requestId": req, "uuid": mid + req,
                       "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "secret words"}],
                                   "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                                             "cache_creation_input_tokens": write}}})


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    return Path(os.environ["CARRIER_CLAUDE_LOGS"])


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "u.db")


def test_reads_logs_incrementally_and_dedupes(store: Store, logs: Path):
    now = time.time()
    f = logs / "-w" / "s1.jsonl"
    f.parent.mkdir()
    lines = [
        json.dumps({"type": "user", "message": {"content": "hello"}}),
        reply(now - 60, "m1", out=5),       # the same reply logged once per content block...
        reply(now - 60, "m1", out=50),      # ...with the full count in the last line
        reply(now - 30, "m2", model="claude-opus-5-5"),
        reply(now - 20, "m3", model="<synthetic>"),  # error placeholders don't count
        reply(now - 90 * 86400, "old"),      # older than KEEP_DAYS
    ]
    f.write_text("\n".join(lines) + "\n" + reply(now, "m4")[:40])  # last line still being written
    st = usage.scan(store, budget=5)
    assert st["done"]
    week = usage.summary(store)["week"]
    assert week["output"] == 100 and week["input"] == 200 and week["cache_read"] == 2000
    # Sonnet: 100*2 + 50*10 + 1000*0.1; Opus: 100*4 + 50*20 + 1000*0.2 (per million)
    assert week["cost"] == pytest.approx((800 + 1600) / 1e6)
    # Only token numbers are kept, never the conversation's words.
    assert "secret" not in json.dumps(store._q("SELECT * FROM usage_entries"))
    # The unfinished line is read once it is complete; nothing is counted twice.
    with open(f, "a") as fh:
        fh.write(reply(now, "m4")[40:] + "\n")
    usage.scan(store, budget=5)
    assert usage.summary(store)["week"]["output"] == 150
    usage.scan(store, budget=5)
    assert usage.summary(store)["week"]["output"] == 150


def test_logs_can_be_turned_off(store: Store, logs: Path):
    (logs / "a.jsonl").write_text(reply(time.time(), "m1") + "\n")
    usersettings.update(usage_read_logs=False)
    assert usage.scan(store).get("off")
    assert usage.summary(store)["week"]["total"] == 0


def test_limit_windows_reset_and_levels(store: Store):
    now = time.time()
    assert usage.plan(store) == "unknown"
    usage.record_account(store, "ANTHROPIC_API_KEY")
    assert usage.plan(store) == "api"
    state, prev = usage.record_limit(store, {"kind": "five_hour", "status": "allowed_warning", "utilization": 0.85,
                                             "resets_at": now + 3600})
    assert prev == "" and usage.plan(store) == "subscription"  # limit reports only come with subscriptions
    s = usage.settings()
    assert usage.level(store, s)["state"] == "near"
    assert usage.hold_reason(store, "takeoff") == ""
    usage.record_limit(store, {"kind": "five_hour", "status": "allowed_warning", "utilization": 0.93,
                               "resets_at": now + 3600})
    assert usage.level(store, s)["state"] == "hold"
    assert "5-hour limit at 93%" in usage.hold_reason(store, "takeoff")
    usersettings.update(usage_hold_takeoff=False)
    assert usage.hold_reason(store, "takeoff") == "" and usage.hold_reason(store, "routines")
    usage.record_limit(store, {"kind": "seven_day", "status": "rejected", "resets_at": now + 86400})
    assert usage.reached(store) == pytest.approx(now + 86400)
    assert usage.hold_reason(store, "takeoff").startswith("Weekly limit reached")  # always held at a limit
    # Once its reset time has passed, a window counts as reset.
    assert usage.reached(store, now + 90000) is None
    w = {x["kind"]: x for x in usage.limits(store, now + 90000)}
    assert w["seven_day"]["status"] == "allowed" and w["five_hour"]["percent"] is None
    # Extra usage: reached, but work goes on (and is billed).
    usage.record_limit(store, {"kind": "seven_day", "status": "rejected", "resets_at": now + 86400,
                               "overage_status": "allowed"})
    assert "using extra usage" in usage.level(store, s)["text"]


def test_reset_time_in_old_messages():
    assert usage.reset_from_text("Claude AI usage limit reached|1762000000") == 1762000000.0
    assert usage.reset_from_text("You've hit your limit") is None


@pytest.fixture
def deck(tmp_path: Path):
    ALL_CLIENTS.clear()
    d = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    yield d
    d.close()


def test_limit_stop_waits_and_continues(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    s = deck.start_session(pid, "big job", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "limit-warn\nhello")
    wait(deck, s["id"])
    assert any(e["kind"] == "usage" and "close to your 5-hour limit" in e["text"]
               for e in events(deck, s["id"], "notice"))
    deck.send(s["id"], "limit-hit 120")
    wait(deck, s["id"], "limited")
    row = deck.store.session(s["id"])
    assert row["limit_until"] == pytest.approx(time.time() + 120 + 60, abs=5)
    note = [e for e in events(deck, s["id"], "notice") if e["kind"] == "limit"][-1]
    assert "Carrier will continue it at" in note["text"]
    assert deck.overview()["usage"]["level"]["state"] == "reached"
    # Not yet: nothing happens.
    assert deck.usage_tick(every=0) == []
    # Starting other work while a limit is reached asks first.
    deck.room.add_task(pid, "another task")
    t = [x for x in deck.store.tasks(pid) if x["title"] == "another task"][0]
    with pytest.raises(UsageHold):
        deck.start_task(t["id"])
    # The reset passes: Carrier tells Claude to carry on.
    w = deck.store.get_setting("usage_limits")
    w["five_hour"]["resets_at"] = time.time() - 1
    deck.store.set_setting("usage_limits", w)
    deck.store.update_session(s["id"], limit_until=time.time() - 1)
    assert deck.usage_tick(every=0) == [s["id"]]
    wait(deck, s["id"])
    assert ALL_CLIENTS[0].prompts[-1].startswith("[Carrier] The limit that stopped")
    row = deck.store.session(s["id"])
    assert row["limit_until"] is None and row["limit_tries"] == 0


def test_limit_wait_survives_a_restart(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    s = deck.start_session(pid, "job", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "limit-hit 30")
    wait(deck, s["id"], "limited")
    deck.close()
    d2 = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    try:
        assert d2.store.session(s["id"])["status"] == "limited"  # not "paused": it is still waiting
        w = d2.store.get_setting("usage_limits")
        w["five_hour"]["resets_at"] = time.time() - 1
        d2.store.set_setting("usage_limits", w)
        d2.store.update_session(s["id"], limit_until=time.time() - 1)
        assert d2.usage_tick(every=0) == [s["id"]]  # resumed the conversation, then continued
        wait(d2, s["id"])
        assert ALL_CLIENTS[-1].prompts[-1].startswith("[Carrier] The limit that stopped")
    finally:
        d2.close()


def test_no_auto_resume_and_continue_button(deck: FlightDeck, tmp_path: Path):
    usersettings.update(usage_auto_resume=False)
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    s = deck.start_session(pid, "job", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "limit-hit 30")
    wait(deck, s["id"], "limited")
    assert "press Continue now then" in [e for e in events(deck, s["id"], "notice") if e["kind"] == "limit"][-1]["text"]
    deck.store.set_setting("usage_limits", {})
    deck.store.update_session(s["id"], limit_until=time.time() - 1)
    assert deck.usage_tick(every=0) == []
    row = deck.store.session(s["id"])
    assert row["status"] == "limited" and row["limit_until"] is None  # Continue now is the way on
    deck.store.update_session(s["id"], limit_until=time.time() - 1)  # untimed waits aren't picked up again
    deck.continue_session(s["id"])
    wait(deck, s["id"])
    assert ALL_CLIENTS[0].prompts[-1].startswith("[Carrier] The limit that stopped")


def test_takeoff_holds_near_a_limit(deck: FlightDeck, tmp_path: Path):
    from carrier.readyroom import routines

    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    r = routines.save(deck.room, pid, "Docs", "Update the docs.")
    routines.run(deck.room, r[0]["id"])
    deck.set_auto_takeoff(False)
    usage.record_limit(deck.store, {"kind": "five_hour", "status": "allowed_warning", "utilization": 0.95,
                                    "resets_at": time.time() + 3600})
    deck.set_auto_takeoff(True)
    assert deck.takeoff_tick(every=0) == []
    assert "5-hour limit at 95%" in deck.overview()["takeoff_hold"]
    q = deck.overview()["takeoff"][0]
    with pytest.raises(UsageHold):  # starting a routine by hand asks first, too
        deck.start_task(q["task_id"])
    started = deck.start_task(q["task_id"], confirm_usage=True)
    assert started["id"]


def test_limits_without_reset_time_and_per_model(store: Store):
    now = time.time()
    usage.record_limit(store, {"kind": "five_hour", "status": "rejected"}, now=now)
    assert usage.reached(store, now) is None  # no reset time: callers retry on their own schedule
    assert usage.level(store, usage.settings(), now)["state"] == "reached"
    assert usage.level(store, usage.settings(), now + usage.GUESS_SECONDS + 1)["state"] == "ok"  # not for ever
    # A per-model weekly limit only warns: Claude Code carries on with another model.
    usage.record_limit(store, {"kind": "seven_day_opus", "status": "rejected", "resets_at": now + 5 * 86400}, now=now)
    later = now + usage.GUESS_SECONDS + 1
    assert usage.reached(store, later) is None
    assert usage.level(store, usage.settings(), later)["state"] == "near"
    assert usage.hold_reason(store, "takeoff") in ("", usage.level(store, usage.settings())["text"])


def test_changing_effort_keeps_the_wait(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    s = deck.start_session(pid, "job", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "limit-hit 600")
    wait(deck, s["id"], "limited")
    deck.set_effort(s["id"], "high")  # reconnects the session
    assert deck.store.session(s["id"])["status"] == "limited"


def test_gives_up_continuing_after_repeated_failures(deck: FlightDeck, tmp_path: Path, monkeypatch):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    s = deck.start_session(pid, "job", mode="acceptEdits", send_task=False)
    deck.send(s["id"], "limit-hit 30")
    wait(deck, s["id"], "limited")
    deck.store.set_setting("usage_limits", {})

    def broken(sid, scheduled=False):
        raise RuntimeError("folder gone")

    monkeypatch.setattr(deck, "continue_session", broken)
    for _ in range(3):
        deck.store.update_session(s["id"], limit_until=time.time() - 1)
        deck.usage_tick(every=0)
    row = deck.store.session(s["id"])
    assert row["status"] == "paused" and row["limit_until"] is None
    assert "Stopped trying" in [e for e in events(deck, s["id"], "notice") if e["kind"] == "limit"][-1]["text"]
